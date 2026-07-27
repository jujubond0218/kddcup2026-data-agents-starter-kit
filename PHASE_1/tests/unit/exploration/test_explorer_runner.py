import json

import pytest
from pydantic import ValidationError

from data_agent_baseline.agents.model import ModelResponse, ModelToolCall, ScriptedModelAdapter
from data_agent_baseline.agents.react import ReActAgent
from data_agent_baseline.benchmark.schema import PublicTask, TaskAssets, TaskRecord
from data_agent_baseline.config import (
    AgentConfig,
    AppConfig,
    DatasetConfig,
    ExplorerConfig as AppExplorerConfig,
    RunConfig,
)
from data_agent_baseline.exploration.inventory import inspect_context
from data_agent_baseline.exploration.runner import (
    ExploreInput,
    ExplorerConfig,
    ExplorerReportInput,
    ExplorerRunner,
    PreviewFileInput,
    _ExplorerTools,
    create_explorer_tool_spec,
)
from data_agent_baseline.run.runner import run_benchmark
from data_agent_baseline.tools.registry import ToolRegistry, create_default_tool_registry


def _task(tmp_path) -> PublicTask:
    task_dir = tmp_path / "task_1"
    context_dir = task_dir / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "sales.csv").write_text(
        "customer_id,amount\nC1,10\nC2,20\nC3,30\n",
        encoding="utf-8",
    )
    return PublicTask(
        record=TaskRecord(task_id="task_1", difficulty="easy", question="Find sales."),
        assets=TaskAssets(task_dir=task_dir, context_dir=context_dir),
    )


def _response(name: str, arguments: dict, call_id: str) -> ModelResponse:
    call = ModelToolCall(id=call_id, name=name, arguments=json.dumps(arguments))
    return ModelResponse(
        content="",
        tool_calls=(call,),
        raw_response=json.dumps({"tool_calls": [call.to_openai_dict()]}),
        finish_reason="tool_calls",
    )


def _request() -> ExploreInput:
    return ExploreInput(
        focus="Confirm the fields needed for the sales question.",
        candidate_paths=["sales.csv"],
    )


def _report(*, evidence_ref: str = "preview:1") -> dict:
    return {
        "selected_sources": [
            {
                "path": "sales.csv",
                "reason": "Contains the requested sales fields.",
                "evidence_refs": [evidence_ref],
            }
        ],
        "evidence_refs": [evidence_ref],
        "key_fields": [
            {
                "path": "sales.csv",
                "field": "amount",
                "table": None,
                "reason": "Requested measure.",
                "evidence_refs": [evidence_ref],
            }
        ],
        "join_candidates": [],
        "etl_candidates": [],
        "warnings": [],
        "uncertainties": [],
    }


def _inventory(task: PublicTask, config: ExplorerConfig | None = None) -> dict:
    effective_config = config or ExplorerConfig()
    return inspect_context(task, effective_config.inventory_limits())


def test_explorer_returns_evidence_first_report_without_inventory_model_step(tmp_path):
    task = _task(tmp_path)
    config = ExplorerConfig(max_steps=2, max_preview_calls=1)
    model = ScriptedModelAdapter(
        [
            _response("preview_file", {"path": "sales.csv"}, "explore_preview"),
            _response("report", _report(), "explore_report"),
        ]
    )
    events = []

    result = ExplorerRunner(
        model=model,
        config=config,
        inventory=_inventory(task, config),
        event_sink=lambda kind, payload: events.append((kind, payload)),
    ).run(task, _request())

    assert result.success is True
    assert result.fallback_used is False
    assert result.report == _report()
    assert result.evidence[0]["evidence_id"] == "preview:1"
    assert result.steps_used == 2
    assert "candidate_evidence" in model.requests[0][1].content
    assert model.requests[1][-1].tool_call_id == "explore_preview"
    assert any(kind == "explorer_completed" for kind, _ in events)


def test_explorer_finishes_with_two_previews_and_report_within_three_requests(tmp_path):
    task = _task(tmp_path)
    (task.context_dir / "notes.md").write_text(
        "# Sales notes\namount is numeric\n", encoding="utf-8"
    )
    config = ExplorerConfig(max_steps=3, max_preview_calls=2)
    inventory = _inventory(task, config)
    request = ExploreInput(
        focus="Confirm sales fields and supporting notes.",
        candidate_paths=["sales.csv", "notes.md"],
    )
    report = _report()
    report["selected_sources"].append(
        {
            "path": "notes.md",
            "reason": "Defines the amount field.",
            "evidence_refs": ["preview:2"],
        }
    )
    report["evidence_refs"] = ["preview:1", "preview:2"]
    model = ScriptedModelAdapter(
        [
            _response("preview_file", {"path": "sales.csv"}, "preview_one"),
            _response("preview_file", {"path": "notes.md"}, "preview_two"),
            _response("report", report, "report"),
        ]
    )

    result = ExplorerRunner(
        model=model,
        config=config,
        inventory=inventory,
    ).run(task, request)

    assert result.fallback_used is False
    assert result.steps_used == 3
    assert len(model.requests) == 3
    assert {item["evidence_id"] for item in result.evidence} == {
        "preview:1",
        "preview:2",
    }


def test_explorer_final_step_requires_report_and_fails_open(tmp_path):
    task = _task(tmp_path)
    config = ExplorerConfig(max_steps=1)
    result = ExplorerRunner(
        model=ScriptedModelAdapter(
            [_response("preview_file", {"path": "sales.csv"}, "late_preview")]
        ),
        config=config,
        inventory=_inventory(task, config),
    ).run(task, _request())

    assert result.success is True
    assert result.fallback_used is True
    assert result.steps_used == 1
    assert result.report["selected_sources"][0]["path"] == "sales.csv"
    assert result.evidence[0]["evidence_id"] == "inventory:1"


def test_explorer_model_failure_returns_inventory_evidence_fallback(tmp_path):
    task = _task(tmp_path)
    config = ExplorerConfig()
    result = ExplorerRunner(
        model=ScriptedModelAdapter([]),
        config=config,
        inventory=_inventory(task, config),
    ).run(task, _request())

    assert result.success is True
    assert result.fallback_used is True
    assert result.evidence[0]["source_tool"] == "context_inventory"
    assert "RuntimeError" in result.failure_reason


def test_explore_input_enforces_focus_and_candidate_limits():
    with pytest.raises(ValidationError):
        ExploreInput(focus=" ", candidate_paths=["sales.csv"])
    with pytest.raises(ValidationError):
        ExploreInput(focus="x" * 501, candidate_paths=["sales.csv"])
    with pytest.raises(ValidationError):
        ExploreInput(focus="x", candidate_paths=[f"{index}.csv" for index in range(9)])
    with pytest.raises(ValidationError):
        ExploreInput(focus="x", candidate_paths=["sales.csv", "sales.csv"])


def test_explorer_restricts_preview_paths_and_budget(tmp_path):
    task = _task(tmp_path)
    config = ExplorerConfig(max_preview_calls=1)
    tools = _ExplorerTools(
        config=config,
        inventory=_inventory(task, config),
        candidate_paths=["sales.csv"],
    )

    first = tools.preview(task, PreviewFileInput(path="sales.csv"))
    second = tools.preview(task, PreviewFileInput(path="sales.csv"))
    outside = tools.preview(task, PreviewFileInput(path="../outside.csv"))

    assert first.ok is True
    assert first.content["observation"]["summary"]["sample_row_count"] == 3
    assert second.error_code == "PREVIEW_BUDGET_EXHAUSTED"
    assert outside.error_code == "PATH_NOT_SELECTED"


def test_report_rejects_unknown_evidence_and_unselected_paths(tmp_path):
    task = _task(tmp_path)
    config = ExplorerConfig()
    tools = _ExplorerTools(
        config=config,
        inventory=_inventory(task, config),
        candidate_paths=["sales.csv"],
    )

    unknown_evidence = tools.report(
        task,
        ExplorerReportInput.model_validate(_report(evidence_ref="preview:99")),
    )
    unselected_path = _report(evidence_ref="inventory:1")
    unselected_path["selected_sources"][0]["path"] = "other.csv"
    invalid_path = tools.report(
        task,
        ExplorerReportInput.model_validate(unselected_path),
    )

    assert unknown_evidence.error_code == "UNKNOWN_EVIDENCE_REF"
    assert invalid_path.error_code == "REPORT_PATH_NOT_SELECTED"


def test_report_schema_cannot_overwrite_inventory():
    payload = _report(evidence_ref="inventory:1")
    payload["inventory"] = {"files": []}

    with pytest.raises(ValidationError):
        ExplorerReportInput.model_validate(payload)


def test_explore_tool_rejects_paths_missing_from_inventory(tmp_path):
    task = _task(tmp_path)
    config = ExplorerConfig()
    model = ScriptedModelAdapter([])
    registry = ToolRegistry(
        specs={
            "explore": create_explorer_tool_spec(
                model=model,
                config=config,
                inventory=_inventory(task, config),
            )
        }
    )
    result = registry.execute(
        task,
        ModelToolCall(
            id="invalid_path",
            name="explore",
            arguments=json.dumps(
                {
                    "focus": "Inspect another file.",
                    "candidate_paths": ["../outside.csv"],
                }
            ),
        ),
    )

    assert result.error_code == "INVALID_CANDIDATE_PATH"
    assert model.requests == []


def test_main_agent_can_skip_explore_when_inventory_is_sufficient(tmp_path):
    task = _task(tmp_path)
    inventory = _inventory(task)
    model = ScriptedModelAdapter(
        [_response("answer", {"columns": ["amount"], "rows": [["10"]]}, "main_answer")]
    )
    agent = ReActAgent(
        model=model,
        tools=create_default_tool_registry(),
        context_inventory=inventory,
    )

    result = agent.run(task)

    assert result.succeeded is True
    assert [step.action for step in result.steps] == ["answer"]
    assert "Context Inventory" in model.requests[0][1].content
    assert "sales.csv" in model.requests[0][1].content


def test_main_agent_can_call_focused_explore_then_answer_with_matching_ids(tmp_path):
    task = _task(tmp_path)
    config = ExplorerConfig(max_steps=2, max_preview_calls=1)
    inventory = _inventory(task, config)
    model = ScriptedModelAdapter(
        [
            _response(
                "explore",
                {
                    "focus": "Confirm the sales amount field.",
                    "candidate_paths": ["sales.csv"],
                },
                "main_explore",
            ),
            _response("preview_file", {"path": "sales.csv"}, "explore_preview"),
            _response("report", _report(), "explore_report"),
            _response(
                "answer",
                {"columns": ["amount"], "rows": [["10"]]},
                "main_answer",
            ),
        ]
    )
    base_registry = create_default_tool_registry()
    specs = dict(base_registry.specs)
    specs["explore"] = create_explorer_tool_spec(
        model=model,
        config=config,
        inventory=inventory,
    )
    agent = ReActAgent(
        model=model,
        tools=ToolRegistry(specs=specs),
        context_inventory=inventory,
    )

    result = agent.run(task)

    assert result.succeeded is True
    assert [step.action for step in result.steps] == ["explore", "answer"]
    assert result.steps[0].tool_call_id == "main_explore"
    assert model.requests[-1][-1].tool_call_id == "main_explore"


def test_runner_injects_inventory_without_forcing_explorer(tmp_path):
    task = _task(tmp_path)
    (task.task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": "task_1",
                "difficulty": "easy",
                "question": "Find sales.",
            }
        ),
        encoding="utf-8",
    )
    config = AppConfig(
        dataset=DatasetConfig(root_path=tmp_path),
        agent=AgentConfig(api_key="test-key"),
        run=RunConfig(output_dir=tmp_path / "runs", run_id="explorer-run", max_workers=1),
    )
    model = ScriptedModelAdapter(
        [_response("answer", {"columns": ["amount"], "rows": [["10"]]}, "main_answer")]
    )

    run_output_dir, artifacts = run_benchmark(
        config=config,
        model=model,
        task_ids=["task_1"],
    )

    assert artifacts[0].succeeded is True
    assert "Context Inventory" in model.requests[0][1].content
    assert "Call `explore` only when" in model.requests[0][0].content
    events = [
        json.loads(line)
        for line in (run_output_dir / "task_1" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    created = next(event for event in events if event["event_type"] == "context_inventory_created")
    assert created["file_count"] == 1
    assert created["prompt_chars"] <= config.explorer.max_prompt_inventory_chars
    assert not any(event["event_type"] == "explorer_started" for event in events)


def test_runner_inventory_failure_is_recorded_and_main_agent_continues(
    tmp_path,
    monkeypatch,
):
    task = _task(tmp_path)
    (task.task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": "task_1",
                "difficulty": "easy",
                "question": "Find sales.",
            }
        ),
        encoding="utf-8",
    )

    def fail_inventory(*_args, **_kwargs):
        raise OSError("inventory unavailable")

    monkeypatch.setattr(
        "data_agent_baseline.run.runner.inspect_context",
        fail_inventory,
    )
    config = AppConfig(
        dataset=DatasetConfig(root_path=tmp_path),
        agent=AgentConfig(api_key="test-key"),
        run=RunConfig(output_dir=tmp_path / "runs", run_id="failed-inventory", max_workers=1),
    )
    model = ScriptedModelAdapter(
        [_response("answer", {"columns": ["amount"], "rows": [["10"]]}, "main_answer")]
    )

    run_output_dir, artifacts = run_benchmark(
        config=config,
        model=model,
        task_ids=["task_1"],
    )

    assert artifacts[0].succeeded is True
    assert "CONTEXT_INVENTORY_FAILED" in model.requests[0][1].content
    events = [
        json.loads(line)
        for line in (run_output_dir / "task_1" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failed = next(event for event in events if event["event_type"] == "context_inventory_failed")
    assert failed["error_type"] == "OSError"
    assert "inventory unavailable" not in json.dumps(failed)


def test_disabled_explorer_restores_prompt_without_inventory(tmp_path):
    task = _task(tmp_path)
    (task.task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": "task_1",
                "difficulty": "easy",
                "question": "Find sales.",
            }
        ),
        encoding="utf-8",
    )
    config = AppConfig(
        dataset=DatasetConfig(root_path=tmp_path),
        agent=AgentConfig(api_key="test-key"),
        run=RunConfig(output_dir=tmp_path / "runs", run_id="no-explorer", max_workers=1),
        explorer=AppExplorerConfig(enabled=False),
    )
    model = ScriptedModelAdapter(
        [_response("answer", {"columns": ["amount"], "rows": [["10"]]}, "main_answer")]
    )

    run_benchmark(config=config, model=model, task_ids=["task_1"])

    assert "Context Inventory" not in model.requests[0][1].content
