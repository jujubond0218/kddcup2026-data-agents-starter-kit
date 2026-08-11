import json

from data_agent_baseline.agents.model import (
    ModelResponse,
    ModelToolCall,
    ScriptedModelAdapter,
)
from data_agent_baseline.agents.react import ReActAgent, ReActAgentConfig
from data_agent_baseline.benchmark.schema import PublicTask, TaskAssets, TaskRecord
from data_agent_baseline.config import (
    AgentConfig,
    AppConfig,
    DatasetConfig,
    ExplorerConfig,
    RunConfig,
)
from data_agent_baseline.run.runner import run_benchmark
from data_agent_baseline.tools.python_exec import PYTHON_CAPTURE_STREAM_MAX_BYTES
from data_agent_baseline.tools.registry import (
    ToolRegistry,
    ToolSpec,
    create_default_tool_registry,
)


def _task(tmp_path) -> PublicTask:
    task_dir = tmp_path / "task_1"
    context_dir = task_dir / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "data.csv").write_text("value\none\n", encoding="utf-8")
    return PublicTask(
        record=TaskRecord(
            task_id="task_1",
            difficulty="easy",
            question="Return the value.",
        ),
        assets=TaskAssets(task_dir=task_dir, context_dir=context_dir),
    )


def _write_task_json(task: PublicTask) -> None:
    (task.task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": task.task_id,
                "difficulty": task.difficulty,
                "question": task.question,
            }
        ),
        encoding="utf-8",
    )


def _tool_response(
    name: str,
    arguments: dict[str, object] | str,
    *,
    call_id: str,
    content: str = "",
) -> ModelResponse:
    rendered_arguments = (
        arguments
        if isinstance(arguments, str)
        else json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    )
    call = ModelToolCall(id=call_id, name=name, arguments=rendered_arguments)
    return ModelResponse(
        content=content,
        tool_calls=(call,),
        raw_response=json.dumps({"tool_calls": [call.to_openai_dict()]}),
        finish_reason="tool_calls",
    )


def _answer_response(value: str = "one") -> ModelResponse:
    return _tool_response(
        "answer",
        {"columns": ["value"], "rows": [[value]]},
        call_id=f"call_answer_{value}",
    )


def test_runs_native_tool_loop_and_replays_matching_call_id(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("list_context", {"max_depth": 2}, call_id="call_list"),
            _answer_response(),
        ]
    )
    events = []
    agent = ReActAgent(
        model=model,
        tools=create_default_tool_registry(),
        event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == ["list_context", "answer"]
    assert result.steps[0].tool_call_id == "call_list"
    second_request = model.requests[1]
    assert [message.role for message in second_request] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert second_request[2].tool_calls[0].id == "call_list"
    assert second_request[3].tool_call_id == "call_list"
    tool_started = next(payload for kind, payload in events if kind == "tool_started")
    assert tool_started["tool_call_id"] == "call_list"
    verification_passed = next(
        payload for kind, payload in events if kind == "answer_verification_passed"
    )
    assert verification_passed["tool_call_id"] == "call_answer_one"


def test_next_model_request_receives_bounded_python_observation(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response(
                "execute_python",
                {"code": "print('HEAD-' + ('x' * 200_000) + '-TAIL')"},
                call_id="call_python_large",
            ),
            _answer_response(),
        ]
    )
    agent = ReActAgent(model=model, tools=create_default_tool_registry())

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    observation = json.loads(model.requests[1][-1].content)
    content = observation["content"]
    assert content["truncated"] is True
    assert content["output"].startswith("HEAD-")
    assert content["output"].endswith("-TAIL\n")
    returned_bytes = content["capture"]["output"]["returned_bytes"]
    assert returned_bytes <= PYTHON_CAPTURE_STREAM_MAX_BYTES


def test_artifact_rejection_can_be_rewritten_and_resubmitted(tmp_path):
    artifact_root = tmp_path / "artifact"
    artifact_root.mkdir()
    model = ScriptedModelAdapter(
        [
            _tool_response(
                "execute_python",
                {
                    "code": (
                        "answer_csv_path.write_text('value,value\\none,two\\n', encoding='utf-8')"
                    )
                },
                call_id="call_write_invalid",
            ),
            _tool_response(
                "answer",
                {"from_csv": "answer.csv"},
                call_id="call_submit_invalid",
            ),
            _tool_response(
                "execute_python",
                {"code": ("answer_csv_path.write_text('value\\none\\n', encoding='utf-8')")},
                call_id="call_rewrite",
            ),
            _tool_response(
                "answer",
                {"from_csv": "answer.csv"},
                call_id="call_submit_valid",
            ),
        ]
    )
    events = []
    agent = ReActAgent(
        model=model,
        tools=create_default_tool_registry(artifact_root=artifact_root),
        event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert result.answer is not None
    assert result.answer.columns == ["value"]
    assert result.answer.rows == [["one"]]
    rejected_observation = model.requests[2][-1]
    assert rejected_observation.tool_call_id == "call_submit_invalid"
    assert json.loads(rejected_observation.content)["content"]["error"]["code"] == (
        "ANSWER_VERIFICATION_ERROR"
    )
    assert [kind for kind, _ in events].count("answer_artifact_detected") == 2
    rejected = next(payload for kind, payload in events if kind == "answer_artifact_rejected")
    submitted = next(payload for kind, payload in events if kind == "answer_artifact_submitted")
    assert rejected["tool_call_id"] == "call_submit_invalid"
    assert rejected["error_code"] == "ANSWER_VERIFICATION_ERROR"
    assert submitted["tool_call_id"] == "call_submit_valid"
    assert submitted["row_count"] == 1
    assert submitted["column_count"] == 1
    assert len(submitted["sha256"]) == 64


def test_python_artifact_observation_contains_metadata_not_rows(tmp_path):
    artifact_root = tmp_path / "artifact"
    artifact_root.mkdir()
    model = ScriptedModelAdapter(
        [
            _tool_response(
                "execute_python",
                {
                    "code": (
                        "answer_csv_path.write_text('value\\n' + "
                        "'\\n'.join(str(i) for i in range(10000)) + '\\n', "
                        "encoding='utf-8')"
                    )
                },
                call_id="call_write_large",
            ),
            _tool_response(
                "answer",
                {"from_csv": "answer.csv"},
                call_id="call_submit_large",
            ),
        ]
    )
    agent = ReActAgent(
        model=model,
        tools=create_default_tool_registry(artifact_root=artifact_root),
    )

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert result.answer is not None
    assert len(result.answer.rows) == 10_000
    observation = json.loads(model.requests[1][-1].content)["content"]
    assert observation["output"] == ""
    assert observation["answer_artifact"]["handle"] == "answer.csv"
    assert observation["answer_artifact"]["byte_count"] > 0
    assert "rows" not in observation["answer_artifact"]
    assert len(json.dumps(result.steps[1].action_input).encode("utf-8")) < 100


def test_blocks_third_consecutive_identical_tool_call_before_handler(tmp_path):
    base_tools = create_default_tool_registry()
    read_spec = base_tools.specs["read_csv"]
    handler_calls = 0

    def counting_handler(task, action_input):
        nonlocal handler_calls
        handler_calls += 1
        return read_spec.handler(task, action_input)

    specs = dict(base_tools.specs)
    specs["read_csv"] = ToolSpec(
        name=read_spec.name,
        description=read_spec.description,
        input_model=read_spec.input_model,
        handler=counting_handler,
        is_terminal=read_spec.is_terminal,
    )
    model = ScriptedModelAdapter(
        [
            _tool_response(
                "read_csv",
                '{"path":"data.csv","max_rows":1}',
                call_id="call_read_first",
            ),
            _tool_response(
                "read_csv",
                '{"max_rows":1,"path":"data.csv"}',
                call_id="call_read_second",
            ),
            _tool_response(
                "read_csv",
                {"path": "data.csv", "max_rows": 1},
                call_id="call_read_blocked",
            ),
            _answer_response(),
        ]
    )
    events = []
    agent = ReActAgent(
        model=model,
        tools=ToolRegistry(specs=specs),
        event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert handler_calls == 2
    blocked = result.steps[2]
    assert blocked.ok is False
    assert blocked.tool_call_id == "call_read_blocked"
    assert blocked.observation["content"]["error"] == {
        "code": "REPEATED_IDENTICAL_TOOL_CALL",
        "message": (
            "Identical call blocked after two consecutive attempts. Reuse the previous "
            "observations instead of retrying unchanged. Review the available evidence, "
            "Explorer report, or current plan, then change the arguments, source, or "
            "approach—or submit the best supported answer."
        ),
        "recoverable": True,
        "do_not_retry_same_call": True,
    }
    assert blocked.observation["content"]["repeat_count"] == 3
    assert blocked.observation["content"]["first_step"] == 1
    assert blocked.observation["content"]["previous_step"] == 2
    assert model.requests[3][-1].tool_call_id == "call_read_blocked"
    assert not any(
        kind == "tool_started" and payload["tool_call_id"] == "call_read_blocked"
        for kind, payload in events
    )
    guard = next(
        payload for kind, payload in events if kind == "repeated_identical_tool_call_blocked"
    )
    assert guard["blocked_tool"] == "read_csv"
    assert guard["repeat_count"] == 3


def test_different_tool_arguments_reset_consecutive_call_count(tmp_path):
    base_tools = create_default_tool_registry()
    list_spec = base_tools.specs["list_context"]
    handler_calls = 0

    def counting_handler(task, action_input):
        nonlocal handler_calls
        handler_calls += 1
        return list_spec.handler(task, action_input)

    specs = dict(base_tools.specs)
    specs["list_context"] = ToolSpec(
        name=list_spec.name,
        description=list_spec.description,
        input_model=list_spec.input_model,
        handler=counting_handler,
        is_terminal=list_spec.is_terminal,
    )
    model = ScriptedModelAdapter(
        [
            _tool_response("list_context", {"max_depth": 2}, call_id="call_list_1"),
            _tool_response("list_context", {"max_depth": 2}, call_id="call_list_2"),
            _tool_response("list_context", {"max_depth": 1}, call_id="call_list_3"),
            _tool_response("list_context", {"max_depth": 2}, call_id="call_list_4"),
            _answer_response(),
        ]
    )
    events = []
    agent = ReActAgent(
        model=model,
        tools=ToolRegistry(specs=specs),
        event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert handler_calls == 4
    assert all(step.ok for step in result.steps)
    assert not any(kind == "repeated_identical_tool_call_blocked" for kind, _ in events)


def test_budget_reminders_are_injected_once_before_steps_15_and_19(tmp_path):
    responses = [
        _tool_response("list_context", {"max_depth": 2}, call_id=f"call_list_{index}")
        for index in range(1, 20)
    ]
    responses.append(_answer_response())
    model = ScriptedModelAdapter(responses)
    events = []
    agent = ReActAgent(
        model=model,
        tools=create_default_tool_registry(),
        config=ReActAgentConfig(max_steps=20),
        event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    warning_text = "STEP BUDGET WARNING"
    critical_text = "STEP BUDGET CRITICAL"
    assert not any(warning_text in (message.content or "") for message in model.requests[13])
    assert sum(warning_text in (message.content or "") for message in model.requests[14]) == 1
    assert not any(critical_text in (message.content or "") for message in model.requests[17])
    assert sum(critical_text in (message.content or "") for message in model.requests[18]) == 1
    assert sum(warning_text in (message.content or "") for message in model.requests[19]) == 1
    assert sum(critical_text in (message.content or "") for message in model.requests[19]) == 1
    warnings = [payload for kind, payload in events if kind == "step_budget_warning"]
    assert [(payload["level"], payload["step_index"]) for payload in warnings] == [
        ("warning", 15),
        ("critical", 19),
    ]
    assert not any(kind == "final_step_guard_triggered" for kind, _ in events)


def test_final_step_guard_blocks_handler_and_allows_one_answer_only_retry(tmp_path):
    base_tools = create_default_tool_registry()
    list_spec = base_tools.specs["list_context"]
    handler_calls = 0

    def counting_handler(task, action_input):
        nonlocal handler_calls
        handler_calls += 1
        return list_spec.handler(task, action_input)

    specs = dict(base_tools.specs)
    specs["list_context"] = ToolSpec(
        name=list_spec.name,
        description=list_spec.description,
        input_model=list_spec.input_model,
        handler=counting_handler,
        is_terminal=list_spec.is_terminal,
    )
    model = ScriptedModelAdapter(
        [
            _tool_response("list_context", {"max_depth": 2}, call_id="call_executed"),
            _tool_response("list_context", {"max_depth": 1}, call_id="call_blocked"),
            _answer_response(),
        ]
    )
    events = []
    agent = ReActAgent(
        model=model,
        tools=ToolRegistry(specs=specs),
        config=ReActAgentConfig(max_steps=2),
        event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert handler_calls == 1
    assert [step.step_index for step in result.steps] == [1, 2, 3]
    assert [step.action for step in result.steps] == ["list_context", "list_context", "answer"]
    blocked = result.steps[1]
    assert blocked.ok is False
    assert blocked.tool_call_id == "call_blocked"
    assert blocked.observation["content"]["error"]["code"] == "FINAL_STEP_REQUIRES_ANSWER"
    assert model.requested_tool_names[2] == ("answer",)
    assert model.requests[2][-1].role == "tool"
    assert model.requests[2][-1].tool_call_id == "call_blocked"
    guard_event = next(payload for kind, payload in events if kind == "final_step_guard_triggered")
    assert guard_event["blocked_tool"] == "list_context"
    assert guard_event["retry_step_index"] == 3
    assert not any(
        kind == "tool_started" and payload["tool_call_id"] == "call_blocked"
        for kind, payload in events
    )
    retry_started = next(
        payload for kind, payload in events if kind == "step_started" and payload["step_index"] == 3
    )
    assert retry_started["finalization_retry"] is True


def test_finalization_retry_failure_modes_do_not_create_another_retry(tmp_path):
    no_call = ModelResponse(
        content="No call.",
        tool_calls=(),
        raw_response='{"content":"No call.","tool_calls":[]}',
        finish_reason="stop",
    )
    cases = [
        (
            _tool_response("list_context", {"max_depth": 1}, call_id="retry_non_answer"),
            "UNKNOWN_TOOL",
        ),
        (no_call, "NO_TOOL_CALL"),
        (_tool_response("answer", {}, call_id="retry_invalid"), "ARGUMENT_VALIDATION_ERROR"),
        (
            _tool_response(
                "answer",
                {"columns": ["value"], "rows": [[None]]},
                call_id="retry_rejected",
            ),
            "ANSWER_VERIFICATION_ERROR",
        ),
    ]

    for index, (retry_response, expected_code) in enumerate(cases):
        model = ScriptedModelAdapter(
            [
                _tool_response("list_context", {"max_depth": 1}, call_id=f"blocked_{index}"),
                retry_response,
            ]
        )
        agent = ReActAgent(
            model=model,
            tools=create_default_tool_registry(),
            config=ReActAgentConfig(max_steps=1),
        )

        result = agent.run(_task(tmp_path / f"case_{index}"))

        assert result.succeeded is False
        assert result.failure_reason == "Agent did not submit an answer within max_steps."
        assert len(model.requests) == 2
        assert model.requested_tool_names[1] == ("answer",)
        assert [step.step_index for step in result.steps] == [1, 2]
        assert result.steps[1].observation["content"]["error"]["code"] == expected_code


def test_exploration_pending_on_final_step_is_not_bypassed(tmp_path):
    base_tools = create_default_tool_registry()
    list_spec = base_tools.specs["list_context"]
    explore_spec = ToolSpec(
        name="explore",
        description="Synthetic exploration tool.",
        input_model=list_spec.input_model,
        handler=list_spec.handler,
    )
    tools = ToolRegistry(
        specs={
            "answer": base_tools.specs["answer"],
            "explore": explore_spec,
        }
    )
    model = ScriptedModelAdapter(
        [_tool_response("explore", {"max_depth": 1}, call_id="call_explore")]
    )
    events = []
    agent = ReActAgent(
        model=model,
        tools=tools,
        config=ReActAgentConfig(max_steps=1),
        event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    result = agent.run(_task(tmp_path))

    assert result.succeeded is False
    assert [step.action for step in result.steps] == ["explore"]
    assert len(model.requests) == 1
    assert model.requested_tool_names[0] == ("explore",)
    assert not any(kind == "final_step_guard_triggered" for kind, _ in events)


def test_returns_validation_error_to_model_and_allows_correction(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("read_csv", {}, call_id="call_invalid"),
            _answer_response(),
        ]
    )
    events = []
    agent = ReActAgent(
        model=model,
        tools=create_default_tool_registry(),
        event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert result.steps[0].ok is False
    assert result.steps[0].observation["content"]["error"]["code"] == ("ARGUMENT_VALIDATION_ERROR")
    tool_message = model.requests[1][-1]
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == "call_invalid"
    assert "ARGUMENT_VALIDATION_ERROR" in tool_message.content
    tool_failure = next(payload for kind, payload in events if kind == "tool_failed")
    assert tool_failure["error_code"] == "ARGUMENT_VALIDATION_ERROR"
    assert tool_failure["tool_call_id"] == "call_invalid"


def test_non_sqlite_observation_reaches_next_turn_with_correction_guidance(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response(
                "execute_context_sql",
                {"path": "data.csv", "sql": "SELECT * FROM data"},
                call_id="call_wrong_sql",
            ),
            _tool_response(
                "read_csv",
                {"path": "data.csv"},
                call_id="call_correct_reader",
            ),
            _answer_response(),
        ]
    )
    agent = ReActAgent(
        model=model,
        tools=create_default_tool_registry(),
    )

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "execute_context_sql",
        "read_csv",
        "answer",
    ]
    assert result.steps[0].observation["content"]["error"]["code"] == "NOT_SQLITE"
    second_request_observation = json.loads(model.requests[1][-1].content)
    error = second_request_observation["content"]["error"]
    assert error["do_not_retry_same_call"] is True
    assert error["suggested_tools"] == ["read_csv"]
    assert "explore report" in error["guidance"]


def test_empty_tool_call_is_recorded_and_corrected_with_user_message(tmp_path):
    empty_response = ModelResponse(
        content="I will answer in text.",
        tool_calls=(),
        raw_response='{"content":"I will answer in text.","tool_calls":[]}',
        finish_reason="stop",
    )
    model = ScriptedModelAdapter([empty_response, _answer_response()])
    agent = ReActAgent(model=model, tools=create_default_tool_registry())

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert result.steps[0].action == "__error__"
    assert result.steps[0].observation["content"]["error"]["code"] == "NO_TOOL_CALL"
    assert [message.role for message in model.requests[1][-2:]] == ["assistant", "user"]
    assert "native tool interface" in model.requests[1][-1].content


def test_multiple_tool_calls_are_rejected_with_one_tool_message_per_id(tmp_path):
    first_call = ModelToolCall(id="call_a", name="list_context", arguments="{}")
    second_call = ModelToolCall(id="call_b", name="list_context", arguments="{}")
    multiple_response = ModelResponse(
        content="",
        tool_calls=(first_call, second_call),
        raw_response='{"tool_calls":["call_a","call_b"]}',
        finish_reason="tool_calls",
    )
    model = ScriptedModelAdapter([multiple_response, _answer_response()])
    agent = ReActAgent(model=model, tools=create_default_tool_registry())

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert result.steps[0].observation["content"]["error"]["code"] == ("MULTIPLE_TOOL_CALLS")
    replay = model.requests[1][-3:]
    assert [message.role for message in replay] == ["assistant", "tool", "tool"]
    assert [message.tool_call_id for message in replay[1:]] == ["call_a", "call_b"]


def test_max_steps_failure_preserves_protocol_error_trace(tmp_path):
    response = ModelResponse(
        content="No call.",
        tool_calls=(),
        raw_response='{"content":"No call.","tool_calls":[]}',
        finish_reason="stop",
    )
    agent = ReActAgent(
        model=ScriptedModelAdapter([response]),
        tools=create_default_tool_registry(),
        config=ReActAgentConfig(max_steps=1),
    )

    result = agent.run(_task(tmp_path))

    assert result.succeeded is False
    assert result.failure_reason == "Agent did not submit an answer within max_steps."
    assert result.steps[0].finish_reason == "stop"


def test_rejects_invalid_answer_then_allows_native_correction(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response(
                "answer",
                {"columns": ["name", " name "], "rows": [["Alice", "Alice"]]},
                call_id="call_rejected_answer",
            ),
            _answer_response(),
        ]
    )
    events = []
    agent = ReActAgent(
        model=model,
        tools=create_default_tool_registry(),
        event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.ok for step in result.steps] == [False, True]
    rejected = result.steps[0]
    assert rejected.tool_call_id == "call_rejected_answer"
    error = rejected.observation["content"]["error"]
    assert error["code"] == "ANSWER_VERIFICATION_ERROR"
    assert error["verification_code"] == "DUPLICATE_COLUMN_NAME"
    correction_observation = model.requests[1][-1]
    assert correction_observation.role == "tool"
    assert correction_observation.tool_call_id == "call_rejected_answer"
    assert "ANSWER_VERIFICATION_ERROR" in correction_observation.content
    rejected_event = next(
        payload for kind, payload in events if kind == "answer_verification_rejected"
    )
    assert rejected_event["verification_code"] == "DUPLICATE_COLUMN_NAME"
    tool_failed = next(payload for kind, payload in events if kind == "tool_failed")
    assert tool_failed["error_code"] == "ANSWER_VERIFICATION_ERROR"


def test_rejected_answer_exhausts_steps_without_becoming_terminal(tmp_path):
    invalid_answer = _tool_response(
        "answer",
        {"columns": ["value"], "rows": [[None]]},
        call_id="call_only_answer",
    )
    agent = ReActAgent(
        model=ScriptedModelAdapter([invalid_answer]),
        tools=create_default_tool_registry(),
        config=ReActAgentConfig(max_steps=1),
    )

    result = agent.run(_task(tmp_path))

    assert result.succeeded is False
    assert result.answer is None
    assert result.failure_reason == "Agent did not submit an answer within max_steps."
    assert result.steps[0].observation["content"]["error"]["code"] == ("ANSWER_VERIFICATION_ERROR")


def test_runner_persists_guard_event_step_21_and_prediction(tmp_path):
    task = _task(tmp_path)
    _write_task_json(task)
    model = ScriptedModelAdapter(
        [
            *[
                _tool_response(
                    "list_context",
                    {"max_depth": 1},
                    call_id=f"runner_list_{index}",
                )
                for index in range(1, 20)
            ],
            _tool_response("list_context", {"max_depth": 1}, call_id="runner_blocked"),
            _answer_response(),
        ],
    )
    config = AppConfig(
        dataset=DatasetConfig(root_path=tmp_path),
        agent=AgentConfig(max_steps=20),
        run=RunConfig(output_dir=tmp_path / "runs", run_id="stop-guard", max_workers=1),
        explorer=ExplorerConfig(enabled=False),
    )

    run_output_dir, artifacts = run_benchmark(
        config=config,
        model=model,
        tools=create_default_tool_registry(),
        task_ids=["task_1"],
    )

    assert artifacts[0].succeeded is True
    task_output_dir = run_output_dir / "task_1"
    trace = json.loads((task_output_dir / "trace.json").read_text(encoding="utf-8"))
    assert [step["step_index"] for step in trace["steps"]] == list(range(1, 22))
    assert trace["steps"][19]["observation"]["content"]["error"]["code"] == (
        "FINAL_STEP_REQUIRES_ANSWER"
    )
    events = [
        json.loads(line)
        for line in (task_output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert sum(event["event_type"] == "final_step_guard_triggered" for event in events) == 1
    assert any(
        event["event_type"] == "step_started"
        and event["step_index"] == 21
        and event["finalization_retry"] is True
        for event in events
    )
    assert (task_output_dir / "prediction.csv").read_text(encoding="utf-8").splitlines() == [
        "value",
        "one",
    ]
