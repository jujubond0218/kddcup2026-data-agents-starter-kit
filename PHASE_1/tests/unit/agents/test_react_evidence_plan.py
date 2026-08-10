import json

from pydantic import BaseModel, ConfigDict

from data_agent_baseline.agents.model import (
    ModelResponse,
    ModelToolCall,
    ScriptedModelAdapter,
)
from data_agent_baseline.agents.react import ReActAgent, ReActAgentConfig
from data_agent_baseline.benchmark.schema import PublicTask, TaskAssets, TaskRecord
from data_agent_baseline.tools.registry import (
    ToolExecutionResult,
    ToolRegistry,
    ToolSpec,
    create_default_tool_registry,
)


class _EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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


def _tool_response(
    name: str,
    arguments: dict[str, object] | str,
    *,
    call_id: str,
    content: str = "",
) -> ModelResponse:
    rendered = (
        arguments
        if isinstance(arguments, str)
        else json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    )
    call = ModelToolCall(id=call_id, name=name, arguments=rendered)
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


def _no_call_response() -> ModelResponse:
    return ModelResponse(
        content="No call.",
        tool_calls=(),
        raw_response='{"content":"No call.","tool_calls":[]}',
        finish_reason="stop",
    )


def _multiple_calls_response() -> ModelResponse:
    first_call = ModelToolCall(id="call_a", name="list_context", arguments="{}")
    second_call = ModelToolCall(id="call_b", name="list_context", arguments="{}")
    return ModelResponse(
        content="",
        tool_calls=(first_call, second_call),
        raw_response='{"tool_calls":["call_a","call_b"]}',
        finish_reason="tool_calls",
    )


def _invalid_call_response() -> ModelResponse:
    call = ModelToolCall(id="", name="", arguments="{}")
    return ModelResponse(
        content="",
        tool_calls=(call,),
        raw_response='{"tool_calls":[{"id":"","name":"","arguments":"{}"}]}',
        finish_reason="tool_calls",
    )


def _base_report() -> dict:
    return {
        "task_interpretation": "test",
        "task_requirements": [
            {
                "id": "req_total",
                "kind": "measure",
                "status": "unresolved",
                "description": "resolve the total",
            }
        ],
        "answer_projection": {"columns": [], "helper_fields": [], "enforceable": False},
        "recommended_sources": [],
        "files": [
            {"path": "data.csv", "kind": "tabular", "summary": {"columns": ["value"]}},
        ],
        "schema_map": {"data.csv": {"columns": ["value"]}},
        "knowledge": {"applicable_rules": []},
        "etl_candidates": [],
        "join_paths": [],
        "value_samples": {},
        "uncertainties": [
            {
                "requirement_id": "req_total",
                "issue": "ambiguous total",
                "candidates": [],
                "evidence_refs": [],
                "verification_hint": "check",
            }
        ],
        "warnings": [],
    }


def _resolved_report() -> dict:
    report = _base_report()
    report["task_requirements"][0]["status"] = "resolved"
    report["uncertainties"] = []
    return report


def _fallback_report() -> dict:
    return {
        "task_interpretation": "Explorer did not complete.",
        "task_requirements": [
            {
                "id": "task_goal",
                "kind": "other",
                "status": "unresolved",
                "description": "complete the task",
            }
        ],
        "answer_projection": {"columns": [], "helper_fields": [], "enforceable": False},
        "recommended_sources": [],
        "files": [
            {"path": "data.csv", "kind": "tabular", "summary": {"columns": ["value"]}},
        ],
        "schema_map": {"data.csv": {"columns": ["value"]}},
        "knowledge": {"applicable_rules": []},
        "etl_candidates": [],
        "join_paths": [],
        "value_samples": {},
        "uncertainties": [
            {
                "requirement_id": "task_goal",
                "issue": "synthesis failed",
                "candidates": ["data.csv"],
                "evidence_refs": [],
                "verification_hint": "check data.csv",
            }
        ],
        "warnings": [{"code": "EXPLORER_FALLBACK"}],
    }


def _explore_spec(report: dict) -> ToolSpec:
    def handler(task, action_input):
        del task, action_input
        return ToolExecutionResult(
            ok=True,
            content={"report": report, "evidence": []},
        )

    return ToolSpec(
        name="explore",
        description="Synthetic exploration tool.",
        input_model=_EmptyInput,
        handler=handler,
        is_terminal=True,
    )


def _tools(report: dict) -> ToolRegistry:
    base = create_default_tool_registry()
    specs = dict(base.specs)
    specs["explore"] = _explore_spec(report)
    return ToolRegistry(specs=specs)


def _valid_plan_arguments(requirement_id: str = "req_total") -> dict:
    return {
        "items": [
            {
                "requirement_id": requirement_id,
                "candidates": [
                    {
                        "claim": "sum the value",
                        "source_fields": [{"path": "data.csv", "field": "value"}],
                        "operation": "measure",
                    }
                ],
                "verifications": [
                    {"tool": "read_csv", "path": "data.csv", "purpose": "check values"},
                ],
            }
        ]
    }


def _invalid_plan_arguments() -> dict:
    return {
        "items": [
            {
                "requirement_id": "not_pending",
                "candidates": [
                    {
                        "claim": "sum the value",
                        "source_fields": [{"path": "data.csv", "field": "value"}],
                        "operation": "measure",
                    }
                ],
                "verifications": [
                    {"tool": "read_csv", "path": "data.csv", "purpose": "check values"},
                ],
            }
        ]
    }


def _valid_strict_plan_arguments(requirement_id: str = "req_total") -> dict:
    return {
        "items": {
            requirement_id: {
                "candidates": [
                    {
                        "claim": "sum the value",
                        "source_fields": [{"path": "data.csv", "field": "value"}],
                        "operation": "measure",
                    }
                ],
                "verifications": [
                    {"tool": "read_csv", "path": "data.csv", "purpose": "check values"},
                ],
            }
        }
    }


def _two_file_report() -> dict:
    """Single unresolved requirement; two tabular files usable as verification targets."""
    report = _base_report()
    report["files"] = [
        {"path": "data.csv", "kind": "tabular", "summary": {"columns": ["value"]}},
        {"path": "data2.csv", "kind": "tabular", "summary": {"columns": ["value"]}},
    ]
    report["schema_map"] = {
        "data.csv": {"columns": ["value"]},
        "data2.csv": {"columns": ["value"]},
    }
    return report


def _two_verification_plan_arguments() -> dict:
    """One item declaring two distinct (read_csv, path) verification actions."""
    return {
        "items": [
            {
                "requirement_id": "req_total",
                "candidates": [
                    {
                        "claim": "sum the value",
                        "source_fields": [
                            {"path": "data.csv", "field": "value"},
                            {"path": "data2.csv", "field": "value"},
                        ],
                        "operation": "measure",
                    }
                ],
                "verifications": [
                    {"tool": "read_csv", "path": "data.csv", "purpose": "check values"},
                    {"tool": "read_csv", "path": "data2.csv", "purpose": "check values"},
                ],
            }
        ]
    }


def _two_requirement_report() -> dict:
    """Two unresolved requirements over the same data.csv file."""
    report = _base_report()
    report["task_requirements"] = [
        {
            "id": "req_total",
            "kind": "measure",
            "status": "unresolved",
            "description": "resolve the total",
        },
        {
            "id": "req_rate",
            "kind": "measure",
            "status": "unresolved",
            "description": "resolve the rate",
        },
    ]
    report["uncertainties"] = [
        {
            "requirement_id": "req_total",
            "issue": "ambiguous total",
            "candidates": [],
            "evidence_refs": [],
            "verification_hint": "check",
        },
        {
            "requirement_id": "req_rate",
            "issue": "ambiguous rate",
            "candidates": [],
            "evidence_refs": [],
            "verification_hint": "check",
        },
    ]
    return report


def _duplicate_verification_plan_arguments() -> dict:
    """Two items each verifying the exact same (read_csv, data.csv) action."""
    return {
        "items": [
            {
                "requirement_id": "req_total",
                "candidates": [
                    {
                        "claim": "sum the value",
                        "source_fields": [{"path": "data.csv", "field": "value"}],
                        "operation": "measure",
                    }
                ],
                "verifications": [
                    {"tool": "read_csv", "path": "data.csv", "purpose": "check values"},
                ],
            },
            {
                "requirement_id": "req_rate",
                "candidates": [
                    {
                        "claim": "compute the rate",
                        "source_fields": [{"path": "data.csv", "field": "value"}],
                        "operation": "measure",
                    }
                ],
                "verifications": [
                    {"tool": "read_csv", "path": "data.csv", "purpose": "check values"},
                ],
            },
        ]
    }


def _agent(
    report: dict,
    *,
    max_steps: int = 10,
    enabled: bool = True,
    attempts: int = 2,
    strict_keys: bool = False,
    verification_gate: bool = True,
):
    events: list[tuple[str, dict]] = []
    agent = ReActAgent(
        model=ScriptedModelAdapter([]),
        tools=_tools(report),
        config=ReActAgentConfig(
            max_steps=max_steps,
            evidence_plan_enabled=enabled,
            evidence_plan_max_commit_attempts=attempts,
            evidence_plan_strict_keys=strict_keys,
            evidence_plan_verification_gate=verification_gate,
        ),
        event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )
    return agent, events


def test_plan_not_exposed_when_disabled(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), enabled=False)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert model.requested_tool_names[1] != ("commit_evidence_plan",)
    assert not any(kind == "evidence_plan_required" for kind, _ in events)


def test_plan_not_exposed_without_unresolved_or_uncertainty(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_resolved_report(), enabled=True)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert model.requested_tool_names[1] != ("commit_evidence_plan",)
    assert not any(kind == "evidence_plan_required" for kind, _ in events)


def test_commit_success_restores_tools_and_never_reexposes_explore(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report())
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "read_csv",
        "answer",
    ]
    assert model.requested_tool_names[0] == ("explore",)
    assert model.requested_tool_names[1] == ("commit_evidence_plan",)
    assert "explore" not in model.requested_tool_names[2]
    assert "commit_evidence_plan" not in model.requested_tool_names[2]
    required = next(payload for kind, payload in events if kind == "evidence_plan_required")
    assert required["requirement_count"] == 1
    committed = next(payload for kind, payload in events if kind == "evidence_plan_committed")
    assert committed["verification_count"] == 1
    assert committed["requirement_ids"] == ["req_total"]
    observed = next(
        payload for kind, payload in events if kind == "evidence_plan_verification_observed"
    )
    assert observed["tool"] == "read_csv"
    assert observed["path"] == "data.csv"
    answered = next(payload for kind, payload in events if kind == "evidence_plan_answered")
    assert answered["verification_observed_count"] == 1
    assert answered["all_verifications_observed"] is True


def test_invalid_plan_is_recoverable_and_retries_only_plan_tool(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _invalid_plan_arguments(),
                call_id="call_plan_bad",
            ),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan_ok",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), attempts=2)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "commit_evidence_plan",
        "read_csv",
        "answer",
    ]
    rejected = result.steps[1]
    assert rejected.ok is False
    assert rejected.tool_call_id == "call_plan_bad"
    assert rejected.observation["content"]["error"]["code"] == (
        "EVIDENCE_PLAN_REQUIREMENT_COVERAGE"
    )
    assert model.requested_tool_names[1] == ("commit_evidence_plan",)
    assert model.requested_tool_names[2] == ("commit_evidence_plan",)
    reject_event = next(
        payload for kind, payload in events if kind == "evidence_plan_commit_rejected"
    )
    assert reject_event["error_code"] == "EVIDENCE_PLAN_REQUIREMENT_COVERAGE"
    assert reject_event["attempt"] == 1
    assert reject_event["fail_open"] is False
    assert reject_event["tool_call_id"] == "call_plan_bad"


def test_two_invalid_commits_fail_open_keep_answer_chance(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _invalid_plan_arguments(),
                call_id="call_plan_bad_1",
            ),
            _tool_response(
                "commit_evidence_plan",
                _invalid_plan_arguments(),
                call_id="call_plan_bad_2",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), attempts=2)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "commit_evidence_plan",
        "read_csv",
        "answer",
    ]
    assert model.requested_tool_names[3] != ("commit_evidence_plan",)
    reject_events = [payload for kind, payload in events if kind == "evidence_plan_commit_rejected"]
    assert [payload["attempt"] for payload in reject_events] == [1, 2]
    assert reject_events[-1]["fail_open"] is True
    assert not any(kind == "evidence_plan_committed" for kind, _ in events)


def test_small_step_budget_skips_plan(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), max_steps=2)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    skipped = next(payload for kind, payload in events if kind == "evidence_plan_skipped")
    assert skipped["reason"] == "INSUFFICIENT_STEP_BUDGET"
    assert model.requested_tool_names[1] != ("commit_evidence_plan",)
    assert not any(kind == "evidence_plan_required" for kind, _ in events)


def test_explorer_fallback_task_goal_triggers_and_commits(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(requirement_id="task_goal"),
                call_id="call_plan_goal",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_fallback_report())
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    required = next(payload for kind, payload in events if kind == "evidence_plan_required")
    assert required["requirement_count"] == 1
    committed = next(payload for kind, payload in events if kind == "evidence_plan_committed")
    assert committed["requirement_ids"] == ["task_goal"]


def test_unverified_answer_blocked_once_then_second_answer_fails_open(tmp_path):
    # Option C supersedes the old "incomplete verification never blocks the answer"
    # contract: after a successful commit, the first answer is rejected while a
    # declared verification is still unobserved; the second answer must fail open so
    # the correction can never loop forever.
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _answer_response(value="one"),
            _answer_response(value="one"),
        ]
    )
    agent, events = _agent(_base_report())
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "answer",
        "answer",
    ]
    blocked = result.steps[2]
    assert blocked.ok is False
    assert blocked.tool_call_id == "call_answer_one"
    assert blocked.observation["content"]["error"]["code"] == "EVIDENCE_PLAN_VERIFICATION_PENDING"
    assert result.steps[3].ok is True
    assert len([p for k, p in events if k == "evidence_plan_answer_blocked"]) == 1
    skipped = next(
        payload for kind, payload in events if kind == "evidence_plan_verification_gate_skipped"
    )
    assert skipped["reason"] == "MAX_ANSWER_BLOCKS"


def test_first_answer_rejected_then_passes_after_verification(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _answer_response(value="one"),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(value="one"),
        ]
    )
    agent, events = _agent(_base_report())
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "answer",
        "read_csv",
        "answer",
    ]
    blocked = result.steps[2]
    assert blocked.ok is False
    assert blocked.tool_call_id == "call_answer_one"
    assert blocked.observation["content"]["error"]["code"] == "EVIDENCE_PLAN_VERIFICATION_PENDING"
    blocked_event = next(
        payload for kind, payload in events if kind == "evidence_plan_answer_blocked"
    )
    assert blocked_event["step_index"] == 3
    assert blocked_event["tool_call_id"] == "call_answer_one"
    assert blocked_event["missing_count"] == 1
    assert blocked_event["missing_verifications"] == [{"tool": "read_csv", "path": "data.csv"}]
    observed = next(
        payload for kind, payload in events if kind == "evidence_plan_verification_observed"
    )
    assert (observed["tool"], observed["path"]) == ("read_csv", "data.csv")
    answered = next(payload for kind, payload in events if kind == "evidence_plan_answered")
    assert answered["all_verifications_observed"] is True


def test_answer_accepted_when_all_verifications_observed(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report())
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "read_csv",
        "answer",
    ]
    assert not any(kind == "evidence_plan_answer_blocked" for kind, _ in events)
    answered = next(payload for kind, payload in events if kind == "evidence_plan_answered")
    assert answered["verification_observed_count"] == 1
    assert answered["all_verifications_observed"] is True


def test_wrong_path_failed_call_and_wrong_tool_do_not_satisfy_verification(tmp_path):
    task = _task(tmp_path)
    (task.assets.context_dir / "data2.csv").write_text("value\ntwo\n", encoding="utf-8")
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _tool_response("read_csv", {"path": "data2.csv"}, call_id="call_wrong_path"),
            _tool_response("read_csv", {"path": "missing.csv"}, call_id="call_failed"),
            _tool_response("read_json", {"path": "data.csv"}, call_id="call_wrong_tool"),
            _answer_response(value="one"),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(value="one"),
        ]
    )
    agent, events = _agent(_base_report())
    agent.model = model

    result = agent.run(task)

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "read_csv",
        "read_csv",
        "read_json",
        "answer",
        "read_csv",
        "answer",
    ]
    blocked = result.steps[5]
    assert blocked.action == "answer"
    assert blocked.observation["content"]["error"]["code"] == "EVIDENCE_PLAN_VERIFICATION_PENDING"
    observed_events = [p for k, p in events if k == "evidence_plan_verification_observed"]
    assert [(p["tool"], p["path"]) for p in observed_events] == [("read_csv", "data.csv")]
    answered = next(payload for kind, payload in events if kind == "evidence_plan_answered")
    assert answered["verification_observed_count"] == 1
    assert answered["all_verifications_observed"] is True


def test_gate_inactive_without_trigger_or_commit_or_verification_gap(tmp_path):
    task = _task(tmp_path)
    # Plan not triggered (all requirements resolved): answer passes unchanged.
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_resolved_report())
    agent.model = model
    result = agent.run(task)
    assert result.succeeded is True
    assert not any(
        kind in {"evidence_plan_answer_blocked", "evidence_plan_verification_gate_skipped"}
        for kind, _ in events
    )

    # Plan triggered but never committed (bounded fail-open): answer passes unchanged.
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _invalid_plan_arguments(),
                call_id="call_bad_1",
            ),
            _tool_response(
                "commit_evidence_plan",
                _invalid_plan_arguments(),
                call_id="call_bad_2",
            ),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), attempts=2)
    agent.model = model
    result = agent.run(task)
    assert result.succeeded is True
    assert not any(kind == "evidence_plan_answer_blocked" for kind, _ in events)

    # Plan committed and all verifications observed: answer passes unchanged.
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report())
    agent.model = model
    result = agent.run(task)
    assert result.succeeded is True
    assert not any(kind == "evidence_plan_answer_blocked" for kind, _ in events)


def test_gate_fails_open_on_insufficient_budget(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), max_steps=4)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "answer",
    ]
    assert result.steps[2].ok is True
    assert not any(kind == "evidence_plan_answer_blocked" for kind, _ in events)
    skipped = next(
        payload for kind, payload in events if kind == "evidence_plan_verification_gate_skipped"
    )
    assert skipped["reason"] == "INSUFFICIENT_STEP_BUDGET"


def test_gate_fails_open_when_budget_cannot_cover_two_missing_verifications(tmp_path):
    task = _task(tmp_path)
    (task.assets.context_dir / "data2.csv").write_text("value\ntwo\n", encoding="utf-8")
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _two_verification_plan_arguments(),
                call_id="call_plan",
            ),
            _answer_response(),
        ]
    )
    # Two missing checks need two read steps plus one answer step (required_steps=3),
    # but answer at step 3 with max_steps=5 leaves only 2 remaining steps.
    agent, events = _agent(_two_file_report(), max_steps=5)
    agent.model = model

    result = agent.run(task)

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "answer",
    ]
    assert result.steps[2].ok is True
    assert not any(kind == "evidence_plan_answer_blocked" for kind, _ in events)
    skipped = next(
        payload for kind, payload in events if kind == "evidence_plan_verification_gate_skipped"
    )
    assert skipped["reason"] == "INSUFFICIENT_STEP_BUDGET"


def test_gate_blocks_when_budget_exactly_covers_two_missing_verifications(tmp_path):
    task = _task(tmp_path)
    (task.assets.context_dir / "data2.csv").write_text("value\ntwo\n", encoding="utf-8")
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _two_verification_plan_arguments(),
                call_id="call_plan",
            ),
            _answer_response(value="one"),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read_1"),
            _tool_response("read_csv", {"path": "data2.csv"}, call_id="call_read_2"),
            _answer_response(value="one"),
        ]
    )
    # Answer at step 3 with max_steps=6 leaves exactly the required 3 steps
    # (two read steps + one answer step), so the first answer is blocked.
    agent, events = _agent(_two_file_report(), max_steps=6)
    agent.model = model

    result = agent.run(task)

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "answer",
        "read_csv",
        "read_csv",
        "answer",
    ]
    blocked = result.steps[2]
    assert blocked.ok is False
    assert blocked.tool_call_id == "call_answer_one"
    blocked_event = next(
        payload for kind, payload in events if kind == "evidence_plan_answer_blocked"
    )
    assert blocked_event["step_index"] == 3
    assert blocked_event["missing_count"] == 2
    assert blocked_event["missing_verifications"] == [
        {"tool": "read_csv", "path": "data.csv"},
        {"tool": "read_csv", "path": "data2.csv"},
    ]
    assert not any(kind == "evidence_plan_verification_gate_skipped" for kind, _ in events)
    answered = next(payload for kind, payload in events if kind == "evidence_plan_answered")
    assert answered["verification_observed_count"] == 2
    assert answered["all_verifications_observed"] is True


def test_duplicate_verification_tool_path_counted_once_in_gate_budget(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _duplicate_verification_plan_arguments(),
                call_id="call_plan",
            ),
            _answer_response(value="one"),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(value="one"),
        ]
    )
    # Both items declare the same (read_csv, data.csv) check. Deduplication makes
    # missing_count 1 and required_steps 2, so answer at step 3 with max_steps=5
    # (remaining=2) is still blocked. Without dedupe, required_steps would be 3 and
    # the gate would fail-open instead.
    agent, events = _agent(_two_requirement_report(), max_steps=5)
    agent.model = model

    result = agent.run(task)

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "answer",
        "read_csv",
        "answer",
    ]
    blocked = result.steps[2]
    assert blocked.ok is False
    assert blocked.observation["content"]["error"]["code"] == "EVIDENCE_PLAN_VERIFICATION_PENDING"
    blocked_event = next(
        payload for kind, payload in events if kind == "evidence_plan_answer_blocked"
    )
    assert blocked_event["missing_count"] == 1
    assert blocked_event["missing_verifications"] == [{"tool": "read_csv", "path": "data.csv"}]
    answered = next(payload for kind, payload in events if kind == "evidence_plan_answered")
    assert answered["all_verifications_observed"] is True


def test_gate_disabled_leaves_unverified_answer_unblocked(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _answer_response(),
        ]
    )
    # verification_gate=False restores the pre-Option-C behavior: the first answer
    # after commit is not blocked even with an unobserved verification.
    agent, events = _agent(_base_report(), verification_gate=False)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "answer",
    ]
    assert result.steps[2].ok is True
    assert not any(kind == "evidence_plan_answer_blocked" for kind, _ in events)
    assert not any(kind == "evidence_plan_verification_gate_skipped" for kind, _ in events)
    answered = next(payload for kind, payload in events if kind == "evidence_plan_answered")
    assert answered["all_verifications_observed"] is False


def test_gate_events_exclude_observation_content(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _answer_response(value="one"),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(value="one"),
        ]
    )
    agent, events = _agent(_base_report())
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    blocked_event = next(
        payload for kind, payload in events if kind == "evidence_plan_answer_blocked"
    )
    assert set(blocked_event) == {
        "step_index",
        "tool_call_id",
        "missing_count",
        "missing_verifications",
    }
    assert all(set(item) == {"tool", "path"} for item in blocked_event["missing_verifications"])
    for kind, payload in events:
        if kind in {"evidence_plan_answer_blocked", "evidence_plan_verification_gate_skipped"}:
            rendered = json.dumps(payload, ensure_ascii=False)
            assert "rows" not in rendered
            assert "observation" not in rendered


def test_no_tool_call_during_pending_is_bounded_and_recovers(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _no_call_response(),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report())
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert result.steps[1].action == "__error__"
    assert result.steps[1].observation["content"]["error"]["code"] == "NO_TOOL_CALL"
    assert model.requested_tool_names[2] == ("commit_evidence_plan",)
    assert any(kind == "evidence_plan_committed" for kind, _ in events)


def test_wrong_tool_during_pending_gets_correction_then_commits(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_wrong"),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report())
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "read_csv",
        "commit_evidence_plan",
        "read_csv",
        "answer",
    ]
    assert result.steps[1].ok is False
    assert result.steps[1].observation["content"]["error"]["code"] == "EVIDENCE_PLAN_PENDING"
    # Runtime enforcement: a wrong tool is not a rejected plan, so it must not
    # emit evidence_plan_commit_rejected or consume a bounded commit attempt.
    assert not any(kind == "evidence_plan_commit_rejected" for kind, _ in events)
    assert model.requested_tool_names[1] == ("commit_evidence_plan",)
    assert model.requested_tool_names[2] == ("commit_evidence_plan",)
    assert any(kind == "evidence_plan_committed" for kind, _ in events)


def test_wrong_tools_do_not_consume_commit_attempts(tmp_path):
    # Three wrong tools would have exhausted max_commit_attempts=2 under the old
    # bounded-failure semantics; runtime enforcement instead keeps the plan pending
    # and lets a later valid commit succeed.
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_wrong_1"),
            _tool_response("read_json", {"path": "data.json"}, call_id="call_wrong_2"),
            _tool_response("execute_python", {"code": "x=1"}, call_id="call_wrong_3"),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), attempts=2)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "read_csv",
        "read_json",
        "execute_python",
        "commit_evidence_plan",
        "read_csv",
        "answer",
    ]
    for step in result.steps[1:4]:
        assert step.observation["content"]["error"]["code"] == "EVIDENCE_PLAN_PENDING"
    assert not any(kind == "evidence_plan_commit_rejected" for kind, _ in events)
    assert any(kind == "evidence_plan_committed" for kind, _ in events)


def test_wrong_tool_loop_fails_open_on_step_budget(tmp_path):
    # With only four steps, the correction loop can hold the plan pending through
    # step 2 (two steps remain), fails open at step 3 (fewer than two remain), and
    # the final normal step still answers.
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_wrong_1"),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_wrong_2"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), max_steps=4)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "read_csv",
        "read_csv",
        "answer",
    ]
    assert result.steps[1].observation["content"]["error"]["code"] == "EVIDENCE_PLAN_PENDING"
    assert result.steps[2].observation["content"]["error"]["code"] == "UNKNOWN_TOOL"
    assert model.requested_tool_names[1] == ("commit_evidence_plan",)
    assert model.requested_tool_names[2] == ("commit_evidence_plan",)
    assert model.requested_tool_names[3] != ("commit_evidence_plan",)
    assert not any(kind == "evidence_plan_committed" for kind, _ in events)


def test_two_no_tool_calls_during_pending_fail_open(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _no_call_response(),
            _no_call_response(),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), attempts=2)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "__error__",
        "__error__",
        "read_csv",
        "answer",
    ]
    assert model.requested_tool_names[1] == ("commit_evidence_plan",)
    assert model.requested_tool_names[2] == ("commit_evidence_plan",)
    assert model.requested_tool_names[3] != ("commit_evidence_plan",)
    reject_events = [payload for kind, payload in events if kind == "evidence_plan_commit_rejected"]
    assert [payload["error_code"] for payload in reject_events] == ["NO_TOOL_CALL", "NO_TOOL_CALL"]
    assert [payload["attempt"] for payload in reject_events] == [1, 2]
    assert reject_events[-1]["fail_open"] is True


def test_multiple_tool_calls_during_pending_counts_as_failure(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _multiple_calls_response(),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), attempts=2)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert model.requested_tool_names[2] == ("commit_evidence_plan",)
    reject_events = [payload for kind, payload in events if kind == "evidence_plan_commit_rejected"]
    assert [payload["error_code"] for payload in reject_events] == ["MULTIPLE_TOOL_CALLS"]
    assert reject_events[0]["attempt"] == 1
    assert reject_events[0]["fail_open"] is False
    assert any(kind == "evidence_plan_committed" for kind, _ in events)


def test_invalid_tool_call_during_pending_counts_as_failure(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _invalid_call_response(),
            _tool_response(
                "commit_evidence_plan",
                _valid_plan_arguments(),
                call_id="call_plan",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), attempts=2)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert model.requested_tool_names[2] == ("commit_evidence_plan",)
    reject_events = [payload for kind, payload in events if kind == "evidence_plan_commit_rejected"]
    assert [payload["error_code"] for payload in reject_events] == ["INVALID_TOOL_CALL"]
    assert reject_events[0]["attempt"] == 1
    assert reject_events[0]["fail_open"] is False
    assert any(kind == "evidence_plan_committed" for kind, _ in events)


def test_strict_commit_success_restores_tools_and_never_reexposes_explore(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                _valid_strict_plan_arguments(),
                call_id="call_plan",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), strict_keys=True)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "read_csv",
        "answer",
    ]
    assert model.requested_tool_names[1] == ("commit_evidence_plan",)
    assert "explore" not in model.requested_tool_names[2]
    assert "commit_evidence_plan" not in model.requested_tool_names[2]
    committed = next(payload for kind, payload in events if kind == "evidence_plan_committed")
    assert committed["requirement_ids"] == ["req_total"]
    assert committed["verification_count"] == 1


def test_strict_wrong_tool_correction_then_commits(tmp_path):
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_wrong"),
            _tool_response(
                "commit_evidence_plan",
                _valid_strict_plan_arguments(),
                call_id="call_plan",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), strict_keys=True)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert result.steps[1].ok is False
    assert result.steps[1].observation["content"]["error"]["code"] == "EVIDENCE_PLAN_PENDING"
    # Option A runtime enforcement still applies: a wrong tool is not a rejected plan.
    assert not any(kind == "evidence_plan_commit_rejected" for kind, _ in events)
    assert any(kind == "evidence_plan_committed" for kind, _ in events)


def test_strict_missing_key_commit_is_bounded_and_fails_open(tmp_path):
    missing_key_args = {"items": {}}
    model = ScriptedModelAdapter(
        [
            _tool_response("explore", {}, call_id="call_explore"),
            _tool_response(
                "commit_evidence_plan",
                missing_key_args,
                call_id="call_plan_bad_1",
            ),
            _tool_response(
                "commit_evidence_plan",
                missing_key_args,
                call_id="call_plan_bad_2",
            ),
            _tool_response("read_csv", {"path": "data.csv"}, call_id="call_read"),
            _answer_response(),
        ]
    )
    agent, events = _agent(_base_report(), attempts=2, strict_keys=True)
    agent.model = model

    result = agent.run(_task(tmp_path))

    assert result.succeeded is True
    assert [step.action for step in result.steps] == [
        "explore",
        "commit_evidence_plan",
        "commit_evidence_plan",
        "read_csv",
        "answer",
    ]
    assert model.requested_tool_names[3] != ("commit_evidence_plan",)
    reject_events = [payload for kind, payload in events if kind == "evidence_plan_commit_rejected"]
    assert [payload["error_code"] for payload in reject_events] == [
        "ARGUMENT_VALIDATION_ERROR",
        "ARGUMENT_VALIDATION_ERROR",
    ]
    assert [payload["attempt"] for payload in reject_events] == [1, 2]
    assert reject_events[-1]["fail_open"] is True
    assert not any(kind == "evidence_plan_committed" for kind, _ in events)


def test_system_prompt_includes_evidence_plan_clause_when_enabled(tmp_path):
    agent, _ = _agent(_base_report(), enabled=True)
    system = agent._initial_messages(_task(tmp_path))[0].content

    assert "commit_evidence_plan" in system
    assert "only tool available" in system
    assert "retry until it is accepted or the runtime fails open" in system
    assert (
        "Complete the declared verification actions before submitting the answer when the step budget permits"
        in system
    )
    assert "exactly once" not in system


def test_system_prompt_omits_evidence_plan_clause_when_disabled(tmp_path):
    agent, _ = _agent(_base_report(), enabled=False)
    system = agent._initial_messages(_task(tmp_path))[0].content

    assert "commit_evidence_plan" not in system
