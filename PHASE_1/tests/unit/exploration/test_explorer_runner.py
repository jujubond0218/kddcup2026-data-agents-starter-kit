import json
import sqlite3

from data_agent_baseline.agents.model import (
    ModelResponse,
    ModelToolCall,
    ScriptedModelAdapter,
)
from data_agent_baseline.agents.react import ReActAgent
from data_agent_baseline.benchmark.schema import PublicTask, TaskAssets, TaskRecord
from data_agent_baseline.config import (
    AgentConfig,
    AppConfig,
    DatasetConfig,
    ExplorerConfig as AppExplorerConfig,
    RunConfig,
)
from data_agent_baseline.exploration.runner import (
    ExploreInput,
    ExplorerConfig,
    ExplorerReportInput,
    ExplorerRunner,
    _ExplorerTools,
    create_explorer_tool_spec,
)
from data_agent_baseline.run.runner import run_benchmark
from data_agent_baseline.tools.registry import ToolRegistry, create_default_tool_registry


def _task(tmp_path, *, sqlite: bool = False) -> PublicTask:
    task_dir = tmp_path / "task_1"
    context_dir = task_dir / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "sales.csv").write_text(
        "customer_id,amount\nC1,10\nC2,20\nC3,30\n",
        encoding="utf-8",
    )
    (context_dir / "knowledge.md").write_text(
        "# General notes\nUnrelated background.\n\n"
        "## Find revenue.\nRevenue maps to the `amount` field.\n",
        encoding="utf-8",
    )
    if sqlite:
        with sqlite3.connect(context_dir / "facts.db") as connection:
            connection.execute("CREATE TABLE facts (customer_id TEXT, region TEXT)")
            connection.execute("INSERT INTO facts VALUES ('C1', 'north')")
    return PublicTask(
        record=TaskRecord(
            task_id="task_1",
            difficulty="easy",
            question="Find revenue.",
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


def _call(name: str, arguments: dict, call_id: str) -> ModelToolCall:
    return ModelToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _response(name: str, arguments: dict, call_id: str) -> ModelResponse:
    call = _call(name, arguments, call_id)
    return ModelResponse(
        content="",
        tool_calls=(call,),
        raw_response=json.dumps({"tool_calls": [call.to_openai_dict()]}),
        finish_reason="tool_calls",
    )


def _report_payload(**overrides) -> dict:
    report = {
        "task_interpretation": "Locate the revenue field and return the requested values.",
        "task_requirements": [
            {
                "id": "measure_revenue",
                "kind": "measure",
                "description": "Identify the field representing revenue.",
                "status": "resolved",
            },
            {
                "id": "output_revenue",
                "kind": "output",
                "description": "Return the requested revenue values.",
                "status": "resolved",
            },
        ],
        "recommended_sources": [
            {
                "path": "sales.csv",
                "table": None,
                "fields": ["amount"],
                "purpose": "Read the requested revenue values.",
                "reason": "Inventory exposes the amount field and knowledge maps revenue to it.",
                "requirement_ids": ["measure_revenue", "output_revenue"],
                "status": "confirmed",
            }
        ],
        "knowledge": {
            "applicable_rules": [
                {
                    "source_path": "knowledge.md",
                    "kind": "field_mapping",
                    "rule": "Revenue maps to the amount field.",
                    "evidence_refs": ["knowledge:1"],
                    "requirement_ids": ["measure_revenue"],
                }
            ]
        },
        "etl_candidates": [],
        "join_paths": [],
        "value_samples": {"sales.csv.amount": [10, 20, 30]},
        "uncertainties": [],
        "warnings": [],
    }
    report.update(overrides)
    return report


def _allowed_schema_values(schema: dict) -> list[str]:
    if "enum" in schema:
        return schema["enum"]
    return [schema["const"]]


def test_direct_report_uses_one_request_and_preserves_knowledge_evidence(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter([_response("report", _report_payload(), "report")])
    events = []

    result = ExplorerRunner(
        model=model,
        config=ExplorerConfig(),
        event_sink=lambda kind, payload: events.append((kind, payload)),
    ).run(task, ExploreInput())

    assert result.success is True
    assert result.fallback_used is False
    assert result.steps_used == 1
    assert len(model.requests) == 1
    assert model.requested_tool_names[0] == ("grep_context", "preview_file", "report")
    assert {item["path"] for item in result.report["files"]} == {
        "knowledge.md",
        "sales.csv",
    }
    evidence = result.report["knowledge"]["source_evidence"]
    assert evidence[0]["evidence_id"] == "knowledge:1"
    assert evidence[0]["path"] == "knowledge.md"
    assert "Revenue maps to the `amount` field." in evidence[0]["excerpt"]
    assert result.report["knowledge"]["applicable_rules"][0]["evidence_refs"] == ["knowledge:1"]
    completed = next(payload for kind, payload in events if kind == "explorer_completed")
    assert completed["knowledge_evidence_count"] == 1
    assert completed["requirement_count"] == 2


def test_report_schema_describes_nested_guide_items_and_normalizes_common_aliases():
    schema = ExplorerReportInput.model_json_schema()

    assert schema["properties"]["task_requirements"]["items"]["$ref"].endswith("/TaskRequirement")
    assert schema["properties"]["recommended_sources"]["items"]["$ref"].endswith(
        "/RecommendedSource"
    )
    assert schema["$defs"]["TaskRequirement"]["properties"]["kind"]["enum"] == [
        "entity",
        "measure",
        "filter",
        "time_scope",
        "output",
        "knowledge",
        "join",
        "other",
    ]

    report = ExplorerReportInput.model_validate(
        {
            "task_requirements": [
                {
                    "requirement_id": "measure_revenue",
                    "type": "measure",
                    "meaning": "Locate revenue.",
                    "status": "confirmed",
                }
            ],
            "recommended_sources": [
                {
                    "source_path": "sales.csv",
                    "candidate_fields": [{"field": "amount"}],
                    "role": "Revenue source.",
                    "supports": ["measure_revenue"],
                    "status": "confirmed",
                }
            ],
            "knowledge": {
                "applicable_rules": [
                    {
                        "source": "knowledge.md",
                        "type": "field_mapping",
                        "text": "Revenue maps to amount.",
                        "evidence_ids": ["knowledge:1"],
                        "supports": ["measure_revenue"],
                    }
                ]
            },
        }
    )

    assert report.task_requirements[0].id == "measure_revenue"
    assert report.task_requirements[0].status == "resolved"
    assert report.recommended_sources[0].fields == ["amount"]
    assert report.knowledge.applicable_rules[0].evidence_refs == ["knowledge:1"]


def test_inventory_is_injected_before_first_model_request(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter([_response("report", _report_payload(), "report")])

    ExplorerRunner(model=model, config=ExplorerConfig()).run(task, ExploreInput())

    bundle = json.loads(model.requests[0][1].content)
    assert bundle["question"] == "Find revenue."
    assert {item["path"] for item in bundle["inventory"]["files"]} == {
        "knowledge.md",
        "sales.csv",
    }
    assert bundle["knowledge_evidence"][0]["path"] == "knowledge.md"
    assert bundle["constraints"]["maximum_targeted_followups"] == 1


def test_knowledge_falls_back_to_question_overlap_when_text_is_not_exact(tmp_path):
    original = _task(tmp_path)
    task = PublicTask(
        record=TaskRecord(
            task_id=original.task_id,
            difficulty=original.difficulty,
            question="Return the customer revenue amount.",
        ),
        assets=original.assets,
    )
    model = ScriptedModelAdapter([_response("report", _report_payload(), "report")])

    result = ExplorerRunner(model=model, config=ExplorerConfig()).run(task, ExploreInput())

    excerpt = result.report["knowledge"]["source_evidence"][0]["excerpt"]
    assert "Revenue maps to the `amount` field." in excerpt
    assert "Unrelated background." not in excerpt


def test_sql_is_hidden_without_sqlite_and_uses_exact_path_enum_when_present(tmp_path):
    without_sqlite = _task(tmp_path / "without")
    tools = _ExplorerTools(config=ExplorerConfig())
    tools.prepare(without_sqlite)
    assert "execute_context_sql" not in tools.registry(report_only=False).specs

    with_sqlite = _task(tmp_path / "with", sqlite=True)
    sqlite_tools = _ExplorerTools(config=ExplorerConfig())
    sqlite_tools.prepare(with_sqlite)
    registry = sqlite_tools.registry(report_only=False)
    assert "execute_context_sql" in registry.specs
    sql_schema = registry.specs["execute_context_sql"].input_model.model_json_schema()
    assert _allowed_schema_values(sql_schema["properties"]["path"]) == ["facts.db"]


def test_preview_and_grep_paths_are_inventory_enums_and_knowledge_is_reserved(tmp_path):
    task = _task(tmp_path)
    tools = _ExplorerTools(config=ExplorerConfig())
    tools.prepare(task)
    registry = tools.registry(report_only=False)

    preview_schema = registry.specs["preview_file"].input_model.model_json_schema()
    grep_schema = registry.specs["grep_context"].input_model.model_json_schema()

    assert _allowed_schema_values(preview_schema["properties"]["path"]) == ["sales.csv"]
    assert _allowed_schema_values(grep_schema["properties"]["path"]) == [
        "knowledge.md",
        "sales.csv",
    ]


def test_one_targeted_followup_is_followed_by_report_only(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter(
        [
            _response("preview_file", {"path": "sales.csv"}, "preview"),
            _response("report", _report_payload(), "report"),
        ]
    )

    result = ExplorerRunner(model=model, config=ExplorerConfig()).run(task, ExploreInput())

    assert result.success is True
    assert result.steps_used == 2
    assert model.requested_tool_names[1] == ("report",)
    assert [item["source_tool"] for item in result.evidence] == [
        "inspect_files",
        "knowledge_preview",
        "preview_file",
    ]
    assert result.report["uncertainties"] == []
    assert result.report["recommended_sources"][0]["status"] == "confirmed"


def test_invalid_dynamic_path_consumes_followup_without_entering_handler(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter(
        [
            _response("preview_file", {"path": "missing.csv"}, "bad_preview"),
            _response("report", _report_payload(), "report"),
        ]
    )
    events = []

    result = ExplorerRunner(
        model=model,
        config=ExplorerConfig(),
        event_sink=lambda kind, payload: events.append((kind, payload)),
    ).run(task, ExploreInput())

    assert result.success is True
    failed = next(
        payload
        for kind, payload in events
        if kind == "explorer_step_completed" and payload["ok"] is False
    )
    assert failed["error_code"] == "ARGUMENT_VALIDATION_ERROR"
    assert not any(
        payload.get("error_code") == "PATH_NOT_INSPECTED"
        for kind, payload in events
        if kind == "explorer_step_completed"
    )
    assert all(item["source_tool"] != "preview_file" for item in result.evidence)
    guard = next(
        item
        for item in result.report["uncertainties"]
        if item["requirement_id"].startswith("followup_verification")
    )
    assert "ARGUMENT_VALIDATION_ERROR" in guard["issue"]
    assert guard["candidates"] == []
    assert any(item.get("code") == "EXPLORER_FOLLOWUP_FAILED" for item in result.report["warnings"])
    assert any(
        item["id"] == guard["requirement_id"] and item["status"] == "unresolved"
        for item in result.report["task_requirements"]
    )


def test_empty_followup_evidence_injects_uncertainty_and_downgrades_source(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter(
        [
            _response(
                "grep_context",
                {"path": "sales.csv", "pattern": "never-present-value"},
                "grep",
            ),
            _response("report", _report_payload(), "report"),
        ]
    )

    result = ExplorerRunner(model=model, config=ExplorerConfig()).run(task, ExploreInput())

    assert result.success is True
    assert result.report["recommended_sources"][0]["status"] == "candidate"
    guard = next(
        item
        for item in result.report["uncertainties"]
        if item["requirement_id"].startswith("followup_verification")
    )
    assert guard["candidates"] == ["sales.csv"]
    assert guard["evidence_refs"] == ["grep:1"]
    assert "no material evidence" in guard["issue"]
    assert any(
        item.get("code") == "EXPLORER_FOLLOWUP_INCONCLUSIVE" for item in result.report["warnings"]
    )


def test_failed_followup_guard_survives_model_fallback(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter(
        [_response("preview_file", {"path": "missing.csv"}, "bad_preview")]
    )

    result = ExplorerRunner(model=model, config=ExplorerConfig()).run(task, ExploreInput())

    assert result.fallback_used is True
    assert any(
        item["requirement_id"].startswith("followup_verification")
        for item in result.report["uncertainties"]
    )
    assert any(item.get("code") == "EXPLORER_FOLLOWUP_FAILED" for item in result.report["warnings"])


def test_report_projection_drops_invalid_semantic_items_without_rejecting_guide(tmp_path):
    task = _task(tmp_path)
    payload = _report_payload(
        task_requirements=[
            {
                "id": "measure_revenue",
                "kind": "measure",
                "description": "Identify revenue.",
                "status": "resolved",
            },
            {
                "id": "measure_revenue",
                "kind": "output",
                "description": "Duplicate ID.",
                "status": "resolved",
            },
        ],
        recommended_sources=[
            {
                "path": "sales.csv",
                "table": None,
                "fields": ["missing_field"],
                "purpose": "Locate revenue.",
                "reason": "Candidate source.",
                "requirement_ids": ["measure_revenue"],
                "status": "confirmed",
            },
            {
                "path": "missing.csv",
                "table": None,
                "fields": [],
                "purpose": "Invalid source.",
                "reason": "Path was invented.",
                "requirement_ids": ["measure_revenue"],
                "status": "candidate",
            },
        ],
        knowledge={
            "applicable_rules": [
                {
                    "source_path": "knowledge.md",
                    "kind": "field_mapping",
                    "rule": "Unanchored rule.",
                    "evidence_refs": ["knowledge:missing"],
                    "requirement_ids": ["measure_revenue"],
                }
            ]
        },
    )
    model = ScriptedModelAdapter([_response("report", payload, "report")])

    result = ExplorerRunner(model=model, config=ExplorerConfig()).run(task, ExploreInput())

    assert result.success is True
    assert len(result.report["task_requirements"]) == 1
    assert result.report["recommended_sources"] == [
        {
            "path": "sales.csv",
            "table": None,
            "fields": [],
            "purpose": "Locate revenue.",
            "reason": "Candidate source.",
            "requirement_ids": ["measure_revenue"],
            "status": "candidate",
        }
    ]
    assert result.report["knowledge"]["applicable_rules"] == []
    codes = {item["code"] for item in result.report["warnings"]}
    assert {
        "DUPLICATE_REQUIREMENT_ID",
        "UNKNOWN_SOURCE_FIELD_DROPPED",
        "UNKNOWN_SOURCE_PATH",
        "UNANCHORED_KNOWLEDGE_RULE",
    } <= codes


def test_model_failure_fallback_keeps_inventory_and_knowledge(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter([])
    events = []

    result = ExplorerRunner(
        model=model,
        config=ExplorerConfig(),
        event_sink=lambda kind, payload: events.append((kind, payload)),
    ).run(task, ExploreInput())

    assert result.success is True
    assert result.fallback_used is True
    assert result.steps_used == 1
    assert result.report["task_requirements"][0]["status"] == "unresolved"
    assert result.report["knowledge"]["source_evidence"][0]["path"] == "knowledge.md"
    assert result.report["uncertainties"][0]["requirement_id"] == "task_goal"
    fallback = next(payload for kind, payload in events if kind == "explorer_fallback_used")
    assert fallback["reason_code"] == "MODEL_FAILURE"


def test_max_steps_one_never_opens_an_unbounded_followup_loop(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter([_response("preview_file", {"path": "sales.csv"}, "preview")])

    result = ExplorerRunner(
        model=model,
        config=ExplorerConfig(max_steps=1),
    ).run(task, ExploreInput())

    assert result.fallback_used is True
    assert result.steps_used == 1
    assert len(model.requests) == 1


def test_zero_soft_deadline_fails_open_without_scan_or_model_request(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter([])

    result = ExplorerRunner(
        model=model,
        config=ExplorerConfig(max_duration_seconds=0),
    ).run(task, ExploreInput())

    assert result.fallback_used is True
    assert result.success is False
    assert model.requests == []


def test_main_agent_calls_explore_once_then_restores_normal_tools(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter(
        [
            _response("explore", {"{}": {}}, "main_explore"),
            _response("report", _report_payload(), "report"),
            _response("explore", {}, "repeated_explore"),
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
        config=ExplorerConfig(),
    )
    agent = ReActAgent(model=model, tools=ToolRegistry(specs=specs))

    result = agent.run(task)

    assert result.succeeded is True
    assert [step.action for step in result.steps] == ["explore", "explore", "answer"]
    assert result.steps[0].tool_call_id == "main_explore"
    assert result.steps[1].observation["content"]["error"]["code"] == "UNKNOWN_TOOL"
    assert model.requested_tool_names[0] == ("explore",)
    assert "explore" not in model.requested_tool_names[2]
    assert model.requests[2][-1].tool_call_id == "main_explore"


def test_runner_forces_first_explore_and_records_deterministic_inventory(tmp_path):
    task = _task(tmp_path)
    _write_task_json(task)
    model = ScriptedModelAdapter(
        [
            _response("explore", {}, "main_explore"),
            _response("report", _report_payload(), "report"),
            _response(
                "answer",
                {"columns": ["amount"], "rows": [["10"]]},
                "main_answer",
            ),
        ]
    )
    config = AppConfig(
        dataset=DatasetConfig(root_path=tmp_path),
        agent=AgentConfig(api_key="test-key"),
        run=RunConfig(output_dir=tmp_path / "runs", run_id="explorer-run", max_workers=1),
        explorer=AppExplorerConfig(),
    )

    run_output_dir, artifacts = run_benchmark(
        config=config,
        model=model,
        task_ids=["task_1"],
    )

    assert artifacts[0].succeeded is True
    assert model.requested_tool_names[0] == ("explore",)
    events = [
        json.loads(line)
        for line in (run_output_dir / "task_1" / "events.jsonl").read_text().splitlines()
    ]
    assert sum(event["event_type"] == "explorer_started" for event in events) == 1
    assert sum(event["event_type"] == "explorer_inspection_created" for event in events) == 1
    assert sum(event["event_type"] == "explorer_knowledge_reviewed" for event in events) == 1
    completed = next(event for event in events if event["event_type"] == "explorer_completed")
    assert completed["steps_used"] == 1
    assert completed["knowledge_evidence_count"] == 1


def test_disabled_explorer_restores_master_prompt_and_tools(tmp_path):
    task = _task(tmp_path)
    _write_task_json(task)
    model = ScriptedModelAdapter(
        [_response("answer", {"columns": ["amount"], "rows": [["10"]]}, "answer")]
    )
    config = AppConfig(
        dataset=DatasetConfig(root_path=tmp_path),
        agent=AgentConfig(api_key="test-key"),
        run=RunConfig(output_dir=tmp_path / "runs", run_id="disabled", max_workers=1),
        explorer=AppExplorerConfig(enabled=False),
    )

    _, artifacts = run_benchmark(config=config, model=model, task_ids=["task_1"])

    assert artifacts[0].succeeded is True
    assert "explore" not in model.requested_tool_names[0]
