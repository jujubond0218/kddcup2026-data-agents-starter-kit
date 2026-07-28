import json
import sqlite3

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
from data_agent_baseline.exploration.runner import (
    ExploreInput,
    ExplorerConfig,
    ExplorerReportInput,
    ExplorerRunner,
    ExplorerSqlInput,
    GrepContextInput,
    InspectFilesInput,
    PreviewFileInput,
    _ExplorerTools,
    create_explorer_tool_spec,
)
from data_agent_baseline.run.runner import run_benchmark
from data_agent_baseline.tools.registry import ToolRegistry, create_default_tool_registry


def _task(tmp_path, *, knowledge: bool = False) -> PublicTask:
    task_dir = tmp_path / "task_1"
    context_dir = task_dir / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "sales.csv").write_text(
        "customer_id,amount\nC1,10\nC2,20\nC3,30\n",
        encoding="utf-8",
    )
    if knowledge:
        (context_dir / "knowledge.md").write_text(
            "# Sales rules\nRevenue maps to `amount`.\n",
            encoding="utf-8",
        )
    return PublicTask(
        record=TaskRecord(task_id="task_1", difficulty="easy", question="Find sales."),
        assets=TaskAssets(task_dir=task_dir, context_dir=context_dir),
    )


def _call(name: str, arguments: dict, call_id: str) -> ModelToolCall:
    return ModelToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _response(*calls: ModelToolCall) -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=tuple(calls),
        raw_response=json.dumps({"tool_calls": [call.to_openai_dict() for call in calls]}),
        finish_reason="tool_calls",
    )


def _single_response(name: str, arguments: dict, call_id: str) -> ModelResponse:
    return _response(_call(name, arguments, call_id))


def _report(
    *,
    inspect_ref: str = "inspect:1",
    sales_ref: str | None = None,
    knowledge_ref: str | None = None,
    knowledge_path: str = "knowledge.md",
) -> dict:
    schema_ref = sales_ref or inspect_ref
    report = {
        "files": [
            {
                "path": "sales.csv",
                "format": "tabular",
                "row_count": 3,
                "evidence_refs": [inspect_ref],
            }
        ],
        "schema_map": {
            "sales.csv": {
                "path": "sales.csv",
                "table": None,
                "columns": ["customer_id", "amount"],
                "semantics": {},
                "evidence_refs": [schema_ref],
            }
        },
        "knowledge": [],
        "etl_candidates": [],
        "join_paths": [],
        "value_samples": {},
        "warnings": [],
    }
    if knowledge_ref is not None:
        report["files"].append(
            {
                "path": knowledge_path,
                "format": "markdown",
                "row_count": None,
                "evidence_refs": [inspect_ref],
            }
        )
        report["knowledge"].append(
            {
                "path": knowledge_path,
                "kind": "field_mapping",
                "text": "Revenue maps to amount.",
                "evidence_refs": [knowledge_ref],
            }
        )
    return report


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


def test_explorer_inspects_then_reads_knowledge_and_supports_two_calls_per_turn(tmp_path):
    task = _task(tmp_path, knowledge=True)
    config = ExplorerConfig(max_steps=3, max_preview_calls=2)
    model = ScriptedModelAdapter(
        [
            _single_response("inspect_files", {}, "inspect_call"),
            _response(
                _call("preview_file", {"path": "knowledge.md"}, "knowledge_preview"),
                _call("preview_file", {"path": "sales.csv"}, "sales_preview"),
            ),
            _single_response(
                "report",
                _report(sales_ref="preview:2", knowledge_ref="preview:1"),
                "report_call",
            ),
        ]
    )
    events = []

    result = ExplorerRunner(
        model=model,
        config=config,
        event_sink=lambda kind, payload: events.append((kind, payload)),
    ).run(task, ExploreInput())

    assert result.success is True
    assert result.fallback_used is False
    assert result.steps_used == 3
    assert {item["evidence_id"] for item in result.evidence} == {
        "inspect:1",
        "preview:1",
        "preview:2",
    }
    assert model.requested_tool_names[0] == ("inspect_files",)
    assert "execute_context_sql" not in model.requested_tool_names[1]
    assert "inspect_files" not in model.requested_tool_names[1]
    assert model.requests[2][-2].tool_call_id == "knowledge_preview"
    assert model.requests[2][-1].tool_call_id == "sales_preview"
    assert any(kind == "explorer_knowledge_reviewed" for kind, _ in events)
    assert any(kind == "explorer_completed" for kind, _ in events)


def test_explorer_requires_inspect_as_first_successful_turn(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter(
        [
            _single_response("preview_file", {"path": "sales.csv"}, "premature"),
            _single_response("inspect_files", {}, "inspect"),
            _single_response("report", _report(), "report"),
        ]
    )

    result = ExplorerRunner(
        model=model,
        config=ExplorerConfig(max_steps=3),
    ).run(task, ExploreInput())

    assert result.success is True
    assert result.steps_used == 3
    protocol_observation = json.loads(model.requests[1][-1].content)
    assert protocol_observation["content"]["error"]["code"] == "INSPECT_REQUIRED"


def test_explorer_rejects_more_than_two_calls_and_mixed_report(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter(
        [
            _response(
                _call("inspect_files", {}, "one"),
                _call("inspect_files", {}, "two"),
                _call("inspect_files", {}, "three"),
            ),
            _single_response("inspect_files", {}, "inspect"),
            _response(
                _call("preview_file", {"path": "sales.csv"}, "preview"),
                _call("report", _report(), "mixed_report"),
            ),
            _single_response("report", _report(), "report"),
        ]
    )

    result = ExplorerRunner(
        model=model,
        config=ExplorerConfig(max_steps=4),
    ).run(task, ExploreInput())

    assert result.success is True
    assert result.steps_used == 4
    first_error = json.loads(model.requests[1][-1].content)
    assert first_error["content"]["error"]["code"] == "TOO_MANY_TOOL_CALLS"
    mixed_error = json.loads(model.requests[3][-1].content)
    assert mixed_error["content"]["error"]["code"] == "REPORT_MUST_BE_EXCLUSIVE"


def test_knowledge_must_be_attempted_before_report(tmp_path):
    task = _task(tmp_path, knowledge=True)
    tools = _ExplorerTools(config=ExplorerConfig())
    inspected = tools.inspect(task, InspectFilesInput())
    rejected = tools.report(task, ExplorerReportInput.model_validate(_report()))
    previewed = tools.preview(task, PreviewFileInput(path="knowledge.md"))
    accepted = tools.report(
        task,
        ExplorerReportInput.model_validate(_report(knowledge_ref=previewed.content["evidence_id"])),
    )

    assert inspected.ok is True
    knowledge_summary = next(
        item["summary"]
        for item in inspected.content["observation"]["files"]
        if item["path"] == "knowledge.md"
    )
    assert "preview" not in knowledge_summary
    assert knowledge_summary["requires_explicit_preview"] is True
    assert rejected.error_code == "KNOWLEDGE_NOT_REVIEWED"
    assert previewed.ok is True
    assert accepted.ok is True


def test_knowledge_detection_is_case_insensitive(tmp_path):
    task = _task(tmp_path, knowledge=True)
    (task.context_dir / "knowledge.md").rename(task.context_dir / "Knowledge.MD")
    tools = _ExplorerTools(config=ExplorerConfig())
    inspected = tools.inspect(task, InspectFilesInput())
    previewed = tools.preview(task, PreviewFileInput(path="Knowledge.MD"))
    accepted = tools.report(
        task,
        ExplorerReportInput.model_validate(
            _report(
                inspect_ref=inspected.content["evidence_id"],
                knowledge_ref=previewed.content["evidence_id"],
                knowledge_path="Knowledge.MD",
            )
        ),
    )

    assert previewed.ok is True
    assert accepted.ok is True


def test_knowledge_preview_failure_can_be_reported_as_evidence_warning(tmp_path, monkeypatch):
    task = _task(tmp_path, knowledge=True)
    tools = _ExplorerTools(config=ExplorerConfig())
    inspected = tools.inspect(task, InspectFilesInput())

    def fail_preview(*_args, **_kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(
        "data_agent_baseline.exploration.runner.preview_context_file",
        fail_preview,
    )
    failed = tools.preview(task, PreviewFileInput(path="knowledge.md"))
    report = _report(inspect_ref=inspected.content["evidence_id"])
    report["files"].append(
        {
            "path": "knowledge.md",
            "format": "markdown",
            "row_count": None,
            "evidence_refs": [inspected.content["evidence_id"]],
        }
    )
    report["warnings"].append(
        {
            "path": "knowledge.md",
            "message": "knowledge.md could not be read.",
            "evidence_refs": [failed.content["evidence"]["evidence_id"]],
        }
    )
    accepted = tools.report(task, ExplorerReportInput.model_validate(report))

    assert failed.error_code == "PREVIEW_FAILED"
    assert accepted.ok is True


def test_preview_requires_inspection_known_path_and_budget(tmp_path):
    task = _task(tmp_path)
    tools = _ExplorerTools(config=ExplorerConfig(max_preview_calls=1))

    before_inspect = tools.preview(task, PreviewFileInput(path="sales.csv"))
    tools.inspect(task, InspectFilesInput())
    first = tools.preview(task, PreviewFileInput(path="sales.csv"))
    second = tools.preview(task, PreviewFileInput(path="sales.csv"))
    outside = tools.preview(task, PreviewFileInput(path="../outside.csv"))

    assert before_inspect.error_code == "INSPECT_REQUIRED"
    assert first.ok is True
    assert first.content["observation"]["summary"]["sample_row_count"] == 3
    assert second.error_code == "PREVIEW_BUDGET_EXHAUSTED"
    assert outside.error_code == "PATH_NOT_INSPECTED"


def test_grep_context_searches_text_and_rejects_invalid_regex(tmp_path):
    task = _task(tmp_path)
    tools = _ExplorerTools(config=ExplorerConfig())
    tools.inspect(task, InspectFilesInput())

    found = tools.grep(task, GrepContextInput(pattern="C[12]"))
    invalid = tools.grep(task, GrepContextInput(pattern="["))
    unsafe_filter = tools.grep(task, GrepContextInput(pattern="C1", path="../outside"))

    assert found.ok is True
    assert found.content["observation"]["match_count"] == 2
    assert invalid.error_code == "INVALID_GREP_PATTERN"
    assert unsafe_filter.error_code == "INVALID_PATH_FILTER"


def test_grep_context_searches_sqlite_and_obeys_file_byte_budget(tmp_path):
    task = _task(tmp_path)
    database_path = task.context_dir / "0facts.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE facts (id INTEGER, value TEXT)")
        connection.executemany("INSERT INTO facts VALUES (?, ?)", [(1, "Alpha"), (2, "Beta")])
    context_bytes = sum(path.stat().st_size for path in task.context_dir.iterdir())
    tools = _ExplorerTools(
        config=ExplorerConfig(
            max_single_file_bytes=database_path.stat().st_size,
            max_total_read_bytes=context_bytes,
        )
    )
    tools.inspect(task, InspectFilesInput())

    found = tools.grep(task, GrepContextInput(pattern="alpha", path="0facts.db"))
    bounded_tools = _ExplorerTools(
        config=ExplorerConfig(max_single_file_bytes=1, max_total_read_bytes=1)
    )
    bounded_tools.inspect(task, InspectFilesInput())
    bounded = bounded_tools.grep(task, GrepContextInput(pattern="alpha", path="0facts.db"))

    assert found.ok is True
    assert found.content["observation"]["matches"][0]["table"] == "facts"
    assert found.content["observation"]["read_bytes"] == database_path.stat().st_size
    assert bounded.ok is True
    assert bounded.content["observation"]["matches"] == []
    assert bounded.content["observation"]["warnings"][0]["code"] == "GREP_FILE_TOO_LARGE"


def test_grep_context_output_is_character_bounded(tmp_path):
    task = _task(tmp_path)
    (task.context_dir / "long.txt").write_text(
        "\n".join(f"match-{index}-{'x' * 500}" for index in range(30)),
        encoding="utf-8",
    )
    tools = _ExplorerTools(config=ExplorerConfig(max_inventory_chars=700))
    tools.inspect(task, InspectFilesInput())

    result = tools.grep(task, GrepContextInput(pattern="match"))

    assert result.ok is True
    observation = result.content["observation"]
    assert len(json.dumps(observation, ensure_ascii=False, separators=(",", ":"))) <= 700
    assert observation["truncated"] is True


def test_explorer_sql_is_read_only_and_evidence_backed(tmp_path):
    task = _task(tmp_path)
    database_path = task.context_dir / "facts.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE facts (id INTEGER, value TEXT)")
        connection.executemany("INSERT INTO facts VALUES (?, ?)", [(1, "a"), (2, "b")])
    tools = _ExplorerTools(config=ExplorerConfig())
    tools.inspect(task, InspectFilesInput())

    selected = tools.sql(
        task,
        ExplorerSqlInput(path="facts.db", sql="SELECT id, value FROM facts", limit=2),
    )
    explained = tools.sql(
        task,
        ExplorerSqlInput(
            path="facts.db",
            sql="EXPLAIN QUERY PLAN SELECT * FROM facts WHERE id = 1",
        ),
    )
    rejected = tools.sql(
        task,
        ExplorerSqlInput(path="facts.db", sql="DELETE FROM facts"),
    )
    pragma = tools.sql(
        task,
        ExplorerSqlInput(path="facts.db", sql="PRAGMA table_info(facts)"),
    )
    multi_statement = tools.sql(
        task,
        ExplorerSqlInput(path="facts.db", sql="SELECT 1; SELECT 2"),
    )
    non_sqlite = tools.sql(
        task,
        ExplorerSqlInput(path="sales.csv", sql="SELECT 1"),
    )

    assert selected.ok is True
    assert selected.content["evidence_id"] == "sql:1"
    assert explained.ok is True
    assert pragma.ok is True
    assert rejected.error_code == "EXPLORATION_SQL_ERROR"
    assert multi_statement.error_code == "EXPLORATION_SQL_ERROR"
    assert non_sqlite.error_code == "NOT_SQLITE"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2


def test_explorer_sql_output_is_character_bounded(tmp_path):
    task = _task(tmp_path)
    database_path = task.context_dir / "large.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE facts (value TEXT)")
        connection.execute("INSERT INTO facts VALUES (?)", ("x" * 20_000,))
    tools = _ExplorerTools(config=ExplorerConfig(max_inventory_chars=500))
    tools.inspect(task, InspectFilesInput())

    result = tools.sql(
        task,
        ExplorerSqlInput(path="large.db", sql="SELECT value FROM facts"),
    )

    assert result.ok is True
    observation = result.content["observation"]
    assert len(json.dumps(observation, ensure_ascii=False, separators=(",", ":"))) <= 500
    assert observation["truncated"] is True


def test_report_rejects_unknown_path_evidence_and_join_field(tmp_path):
    task = _task(tmp_path)
    tools = _ExplorerTools(config=ExplorerConfig())
    inspected = tools.inspect(task, InspectFilesInput())
    inspect_ref = inspected.content["evidence_id"]

    unknown_path = _report(inspect_ref=inspect_ref)
    unknown_path["files"][0]["path"] = "missing.csv"
    path_result = tools.report(task, ExplorerReportInput.model_validate(unknown_path))

    unknown_evidence = _report(inspect_ref="inspect:99")
    evidence_result = tools.report(task, ExplorerReportInput.model_validate(unknown_evidence))

    unknown_field = _report(inspect_ref=inspect_ref)
    unknown_field["join_paths"] = [
        {
            "status": "candidate",
            "left": {"path": "sales.csv", "field": "missing", "table": None},
            "right": {"path": "sales.csv", "field": "amount", "table": None},
            "evidence_refs": [inspect_ref],
        }
    ]
    field_result = tools.report(task, ExplorerReportInput.model_validate(unknown_field))

    assert path_result.error_code == "UNKNOWN_REPORT_PATH"
    assert evidence_result.error_code == "UNKNOWN_EVIDENCE_REF"
    assert field_result.error_code == "UNKNOWN_FIELD_REF"


def test_final_turn_without_report_and_model_failure_use_inspection_fallback(tmp_path):
    task = _task(tmp_path)
    final_missing = ExplorerRunner(
        model=ScriptedModelAdapter(
            [
                _single_response("inspect_files", {}, "inspect"),
                _single_response("preview_file", {"path": "sales.csv"}, "late"),
            ]
        ),
        config=ExplorerConfig(max_steps=2),
    ).run(task, ExploreInput())

    model_failure = ExplorerRunner(
        model=ScriptedModelAdapter([_single_response("inspect_files", {}, "inspect")]),
        config=ExplorerConfig(max_steps=3),
    ).run(task, ExploreInput())

    assert final_missing.fallback_used is True
    assert final_missing.report["files"][0]["path"] == "sales.csv"
    assert model_failure.fallback_used is True
    assert model_failure.evidence[0]["source_tool"] == "inspect_files"


def test_fallback_report_respects_character_budget(tmp_path):
    task = _task(tmp_path)
    for index in range(30):
        (task.context_dir / f"extra-{index:02d}.csv").write_text(
            "id,value\n1,alpha\n",
            encoding="utf-8",
        )
    model = ScriptedModelAdapter(responses=[_single_response("inspect_files", {}, "inspect")])

    result = ExplorerRunner(
        model=model,
        config=ExplorerConfig(max_report_chars=1_000),
    ).run(task, ExploreInput())

    assert result.fallback_used is True
    assert len(json.dumps(result.report, ensure_ascii=False, separators=(",", ":"))) <= 1_000


def test_inspect_failure_immediately_fails_open(tmp_path, monkeypatch):
    task = _task(tmp_path)
    events = []
    model = ScriptedModelAdapter(responses=[_single_response("inspect_files", {}, "inspect")])

    def fail_inspection(*_args, **_kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(
        "data_agent_baseline.exploration.runner.inspect_context",
        fail_inspection,
    )

    result = ExplorerRunner(
        model=model,
        config=ExplorerConfig(),
        event_sink=lambda kind, payload: events.append((kind, payload)),
    ).run(task, ExploreInput())

    assert result.success is False
    assert result.fallback_used is True
    assert result.steps_used == 1
    assert any(kind == "explorer_inspection_failed" for kind, _ in events)
    assert any(
        kind == "explorer_completed" and payload["fallback_used"] is True
        for kind, payload in events
    )


def test_soft_deadline_fails_open_without_model_request(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter([])
    events = []

    result = ExplorerRunner(
        model=model,
        config=ExplorerConfig(max_duration_seconds=0),
        event_sink=lambda kind, payload: events.append((kind, payload)),
    ).run(task, ExploreInput())

    assert result.fallback_used is True
    assert result.success is False
    assert model.requests == []
    fallback = next(payload for kind, payload in events if kind == "explorer_fallback_used")
    assert fallback["reason_code"] == "SOFT_TIMEOUT"


def test_main_agent_calls_no_argument_explore_once_then_restores_tools(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter(
        [
            _single_response("explore", {"{}": {}}, "main_explore"),
            _single_response(
                "inspect_files",
                {"example_parameter_1": "ignored"},
                "inspect",
            ),
            _single_response("report", _report(), "report"),
            _single_response("explore", {}, "repeated_explore"),
            _single_response(
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
        config=ExplorerConfig(max_steps=2),
    )
    agent = ReActAgent(model=model, tools=ToolRegistry(specs=specs))

    result = agent.run(task)

    assert result.succeeded is True
    assert [step.action for step in result.steps] == ["explore", "explore", "answer"]
    assert result.steps[0].tool_call_id == "main_explore"
    assert result.steps[1].observation["content"]["error"]["code"] == "UNKNOWN_TOOL"
    assert model.requested_tool_names[0] == ("explore",)
    assert model.requested_tool_names[1] == ("inspect_files",)
    assert model.requested_tool_names[2] == ("report",)
    assert "explore" not in model.requested_tool_names[3]
    assert model.requests[3][-1].tool_call_id == "main_explore"


def test_runner_forces_explore_without_injecting_inventory(tmp_path):
    task = _task(tmp_path)
    _write_task_json(task)
    model = ScriptedModelAdapter(
        [
            _single_response("explore", {}, "main_explore"),
            _single_response("inspect_files", {}, "inspect"),
            _single_response("report", _report(), "report"),
            _single_response(
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
        explorer=AppExplorerConfig(max_steps=2),
    )

    run_output_dir, artifacts = run_benchmark(
        config=config,
        model=model,
        task_ids=["task_1"],
    )

    assert artifacts[0].succeeded is True
    assert "Context Inventory" not in model.requests[0][1].content
    assert model.requested_tool_names[0] == ("explore",)
    events = [
        json.loads(line)
        for line in (run_output_dir / "task_1" / "events.jsonl").read_text().splitlines()
    ]
    assert sum(event["event_type"] == "explorer_started" for event in events) == 1
    assert sum(event["event_type"] == "explorer_inspection_created" for event in events) == 1
    assert not any(event["event_type"].startswith("context_inventory") for event in events)


def test_disabled_explorer_restores_master_prompt_and_tools(tmp_path):
    task = _task(tmp_path)
    _write_task_json(task)
    model = ScriptedModelAdapter(
        [_single_response("answer", {"columns": ["amount"], "rows": [["10"]]}, "answer")]
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
    assert "Context Inventory" not in model.requests[0][1].content
