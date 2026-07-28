import json
import sqlite3

import pytest

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
    LockRequirementsInput,
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


def _targeted(
    arguments: dict,
    *,
    requirement_id: str = "source_data",
    purpose: str = "Locate data required by the task.",
    target_fields: list[dict] | None = None,
) -> dict:
    return {
        **arguments,
        "requirement_ids": [requirement_id],
        "purpose": purpose,
        "target_fields": list(target_fields or []),
    }


def _preview_input(path: str, *, requirement_id: str = "source_data") -> PreviewFileInput:
    return PreviewFileInput.model_validate(_targeted({"path": path}, requirement_id=requirement_id))


def _grep_input(
    pattern: str,
    *,
    path: str = "sales.csv",
    requirement_id: str = "source_data",
) -> GrepContextInput:
    arguments = {"pattern": pattern, "path": path}
    return GrepContextInput.model_validate(_targeted(arguments, requirement_id=requirement_id))


def _sql_input(path: str, sql: str, *, limit: int = 200) -> ExplorerSqlInput:
    return ExplorerSqlInput.model_validate(_targeted({"path": path, "sql": sql, "limit": limit}))


def _lock_payload(
    paths: list[str],
    *,
    requirement_ids: tuple[str, ...] = ("source_data",),
) -> dict:
    knowledge_paths = [
        path for path in paths if path.rsplit("/", 1)[-1].casefold() == "knowledge.md"
    ]
    data_paths = [path for path in paths if path not in knowledge_paths] or paths
    requirements = [
        {
            "id": requirement_id,
            "kind": "output",
            "description": "Locate data required by the task.",
            "candidate_paths": data_paths,
            "candidate_fields": [],
            "search_terms": [],
            "needs_discovery": True,
        }
        for requirement_id in requirement_ids
    ]
    requirements.extend(
        {
            "id": "knowledge_rule",
            "kind": "knowledge",
            "description": "Review the task knowledge mapping.",
            "candidate_paths": [path],
            "candidate_fields": [],
            "search_terms": [],
            "needs_discovery": True,
        }
        for path in knowledge_paths
    )
    return {"requirements": requirements}


def _lock(
    tools: _ExplorerTools,
    task: PublicTask,
    *,
    requirement_ids: tuple[str, ...] = ("source_data",),
):
    return tools.lock_requirements(
        task,
        LockRequirementsInput.model_validate(
            _lock_payload(sorted(tools.discovered_paths), requirement_ids=requirement_ids)
        ),
    )


def _report(
    *,
    inspect_ref: str = "inspect:1",
    sales_ref: str | None = None,
    knowledge_ref: str | None = None,
    knowledge_path: str = "knowledge.md",
) -> dict:
    del inspect_ref
    report = {
        "relevant_evidence": [],
        "requirement_resolutions": [],
        "selected_sources": ["sales.csv"],
        "field_semantics": [],
        "knowledge": [],
        "etl_candidates": [],
        "join_paths": [],
        "warnings": [],
        "uncertainties": [],
    }
    if sales_ref is not None:
        report["relevant_evidence"].append({"evidence_id": sales_ref, "supports": ["source_data"]})
        report["field_semantics"].append(
            {
                "path": "sales.csv",
                "table": None,
                "field": "amount",
                "meaning": "Sales amount.",
                "evidence_refs": [sales_ref],
            }
        )
    if knowledge_ref is not None:
        report["relevant_evidence"].append(
            {"evidence_id": knowledge_ref, "supports": ["knowledge_rule"]}
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
    config = ExplorerConfig(max_steps=4, max_preview_calls=2)
    model = ScriptedModelAdapter(
        [
            _single_response("inspect_files", {}, "inspect_call"),
            _single_response(
                "lock_requirements",
                _lock_payload(["knowledge.md", "sales.csv"]),
                "lock_call",
            ),
            _response(
                _call(
                    "preview_file",
                    _targeted({"path": "knowledge.md"}, requirement_id="knowledge_rule"),
                    "knowledge_preview",
                ),
                _call("preview_file", _targeted({"path": "sales.csv"}), "sales_preview"),
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
    assert result.steps_used == 4
    assert {item["path"] for item in result.report["files"]} == {
        "knowledge.md",
        "sales.csv",
    }
    assert result.report["schema_map"]["sales.csv"]["columns"] == ["customer_id", "amount"]
    assert {item["source_tool"] for item in result.report["evidence_summaries"]} == {"preview_file"}
    assert {item["evidence_id"] for item in result.evidence} == {
        "inspect:1",
        "preview:1",
        "preview:2",
    }
    assert model.requested_tool_names[0] == ("inspect_files",)
    assert model.requested_tool_names[1] == ("lock_requirements",)
    assert "execute_context_sql" not in model.requested_tool_names[2]
    assert "inspect_files" not in model.requested_tool_names[2]
    preview_observations = [
        message
        for message in model.requests[3]
        if message.role == "tool" and message.tool_call_id in {"knowledge_preview", "sales_preview"}
    ]
    assert [message.tool_call_id for message in preview_observations] == [
        "knowledge_preview",
        "sales_preview",
    ]
    assert any(kind == "explorer_knowledge_reviewed" for kind, _ in events)
    completed = next(payload for kind, payload in events if kind == "explorer_completed")
    assert completed["task_requirement_count"] == 2
    assert completed["relevant_evidence_count"] == 2
    assert completed["selected_source_count"] == 2
    assert completed["report_chars"] > 0


def test_explorer_requires_inspect_as_first_successful_turn(tmp_path):
    task = _task(tmp_path)
    model = ScriptedModelAdapter(
        [
            _single_response("preview_file", _targeted({"path": "sales.csv"}), "premature"),
            _single_response("inspect_files", {}, "inspect"),
            _single_response(
                "lock_requirements",
                _lock_payload(["sales.csv"]),
                "lock",
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
            _single_response(
                "lock_requirements",
                _lock_payload(["sales.csv"]),
                "lock",
            ),
            _response(
                _call("preview_file", _targeted({"path": "sales.csv"}), "preview"),
                _call("report", _report(), "mixed_report"),
            ),
            _single_response("report", _report(), "report"),
        ]
    )

    result = ExplorerRunner(
        model=model,
        config=ExplorerConfig(max_steps=5),
    ).run(task, ExploreInput())

    assert result.success is True
    assert result.steps_used == 5
    first_error = json.loads(model.requests[1][-1].content)
    assert first_error["content"]["error"]["code"] == "TOO_MANY_TOOL_CALLS"
    mixed_error_message = next(
        message
        for message in model.requests[4]
        if message.role == "tool" and message.tool_call_id == "mixed_report"
    )
    mixed_error = json.loads(mixed_error_message.content)
    assert mixed_error["content"]["error"]["code"] == "REPORT_MUST_BE_EXCLUSIVE"


def test_knowledge_must_be_attempted_before_report(tmp_path):
    task = _task(tmp_path, knowledge=True)
    tools = _ExplorerTools(config=ExplorerConfig())
    inspected = tools.inspect(task, InspectFilesInput())
    locked = _lock(tools, task)
    rejected = tools.report(task, ExplorerReportInput.model_validate(_report()))
    previewed = tools.preview(
        task,
        _preview_input("knowledge.md", requirement_id="knowledge_rule"),
    )
    accepted = tools.report(
        task,
        ExplorerReportInput.model_validate(_report(knowledge_ref=previewed.content["evidence_id"])),
    )

    assert inspected.ok is True
    assert locked.ok is True
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


def test_lock_normalizes_ambiguity_and_adds_missing_knowledge_requirement(tmp_path):
    task = _task(tmp_path, knowledge=True)
    tools = _ExplorerTools(config=ExplorerConfig())
    tools.inspect(task, InspectFilesInput())
    payload = {
        "requirements": [
            {
                "id": "measure_sales",
                "kind": "measure",
                "description": "Resolve the requested sales measure.",
                "candidate_paths": ["sales.csv"],
                "candidate_fields": [
                    {"path": "sales.csv", "table": None, "field": "amount"},
                    {"path": "sales.csv", "table": None, "field": "customer_id"},
                ],
                "search_terms": ["sales"],
                "needs_discovery": False,
            }
        ]
    }

    result = tools.lock_requirements(
        task,
        LockRequirementsInput.model_validate(payload),
    )

    assert result.ok is True
    assert result.content["runtime_normalizations"] == {
        "ambiguity_flags": 1,
        "knowledge_requirements": 1,
    }
    assert tools.locked_requirements["measure_sales"].needs_discovery is True
    knowledge = tools.locked_requirements["knowledge_context"]
    assert knowledge.kind == "knowledge"
    assert knowledge.candidate_paths == ["knowledge.md"]
    assert set(tools.registry().specs) == {
        "grep_context",
        "preview_file",
    }


def test_knowledge_detection_is_case_insensitive(tmp_path):
    task = _task(tmp_path, knowledge=True)
    (task.context_dir / "knowledge.md").rename(task.context_dir / "Knowledge.MD")
    tools = _ExplorerTools(config=ExplorerConfig())
    inspected = tools.inspect(task, InspectFilesInput())
    _lock(tools, task)
    previewed = tools.preview(
        task,
        _preview_input("Knowledge.MD", requirement_id="knowledge_rule"),
    )
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
    _lock(tools, task)

    def fail_preview(*_args, **_kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(
        "data_agent_baseline.exploration.runner.preview_context_file",
        fail_preview,
    )
    failed = tools.preview(
        task,
        _preview_input("knowledge.md", requirement_id="knowledge_rule"),
    )
    report = _report(inspect_ref=inspected.content["evidence_id"])
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

    before_inspect = tools.preview(task, _preview_input("sales.csv"))
    tools.inspect(task, InspectFilesInput())
    _lock(tools, task)
    first = tools.preview(task, _preview_input("sales.csv"))
    second = tools.preview(task, _preview_input("sales.csv"))
    outside = tools.preview(task, _preview_input("../outside.csv"))

    assert before_inspect.error_code == "INSPECT_REQUIRED"
    assert first.ok is True
    assert first.content["observation"]["summary"]["sample_row_count"] == 3
    assert second.error_code == "PREVIEW_BUDGET_EXHAUSTED"
    assert outside.error_code == "PATH_NOT_INSPECTED"


def test_deep_discovery_requires_stable_requirement_binding():
    with pytest.raises(ValueError):
        PreviewFileInput.model_validate({"path": "sales.csv"})
    with pytest.raises(ValueError):
        GrepContextInput.model_validate(
            {
                "pattern": "sales",
                "requirement_ids": ["Invalid ID"],
                "purpose": "Locate sales.",
            }
        )


def test_requirements_lock_only_inventory_paths_and_fields_and_become_immutable(tmp_path):
    task = _task(tmp_path)
    tools = _ExplorerTools(config=ExplorerConfig())
    tools.inspect(task, InspectFilesInput())
    unknown_path = _lock_payload(["missing.csv"])
    rejected_path = tools.lock_requirements(
        task,
        LockRequirementsInput.model_validate(unknown_path),
    )
    unknown_field = _lock_payload(["sales.csv"])
    unknown_field["requirements"][0]["candidate_fields"] = [
        {"path": "sales.csv", "table": None, "field": "missing"}
    ]
    unknown_field["requirements"][0]["needs_discovery"] = True
    rejected_field = tools.lock_requirements(
        task,
        LockRequirementsInput.model_validate(unknown_field),
    )
    accepted = _lock(tools, task)
    repeated = _lock(tools, task)

    assert rejected_path.error_code == "UNKNOWN_CANDIDATE_PATH"
    assert rejected_field.error_code == "UNKNOWN_CANDIDATE_FIELD"
    assert accepted.ok is True
    assert repeated.error_code == "REQUIREMENTS_ALREADY_LOCKED"
    assert set(tools.registry().specs) == {"grep_context", "preview_file", "report"}


def test_locked_candidates_constrain_paths_fields_and_per_requirement_budget(tmp_path):
    task = _task(tmp_path)
    (task.context_dir / "other.csv").write_text("name\nAlice\n", encoding="utf-8")
    tools = _ExplorerTools(config=ExplorerConfig(max_preview_calls=4))
    tools.inspect(task, InspectFilesInput())
    tools.lock_requirements(
        task,
        LockRequirementsInput.model_validate(_lock_payload(["sales.csv"])),
    )

    outside_path = tools.preview(task, _preview_input("other.csv"))
    outside_field = tools.preview(
        task,
        PreviewFileInput.model_validate(
            _targeted(
                {"path": "sales.csv"},
                target_fields=[{"path": "sales.csv", "table": None, "field": "customer_id"}],
            )
        ),
    )
    calls = [tools.grep(task, _grep_input("C1")) for _ in range(4)]

    assert outside_path.error_code == "PATH_OUTSIDE_REQUIREMENT"
    assert outside_field.ok is True
    assert outside_field.content["target_fields"] == []
    assert all(result.ok for result in calls[:2])
    assert calls[2].error_code == "REQUIREMENT_DISCOVERY_BUDGET_EXHAUSTED"
    assert calls[3].error_code == "REQUIREMENT_DISCOVERY_BUDGET_EXHAUSTED"


def test_ambiguous_fields_require_targeted_coverage_and_report_resolution(tmp_path):
    task = _task(tmp_path)
    (task.context_dir / "sales.csv").write_text(
        "region,region_name,amount\nN,North,10\nS,South,20\n",
        encoding="utf-8",
    )
    tools = _ExplorerTools(config=ExplorerConfig(max_preview_calls=2))
    tools.inspect(task, InspectFilesInput())
    candidates = [
        {"path": "sales.csv", "table": None, "field": "region"},
        {"path": "sales.csv", "table": None, "field": "region_name"},
    ]
    lock_payload = {
        "requirements": [
            {
                "id": "filter_region",
                "kind": "filter",
                "description": "Resolve which field represents the requested region.",
                "candidate_paths": ["sales.csv"],
                "candidate_fields": candidates,
                "search_terms": ["region"],
                "needs_discovery": True,
            }
        ]
    }
    locked = tools.lock_requirements(
        task,
        LockRequirementsInput.model_validate(lock_payload),
    )
    first = tools.preview(
        task,
        PreviewFileInput.model_validate(
            _targeted(
                {"path": "sales.csv"},
                requirement_id="filter_region",
                target_fields=[candidates[0]],
            )
        ),
    )

    assert locked.ok is True
    assert tools.requirements_ready_for_report() is False
    assert "report" in tools.registry().specs

    second = tools.grep(
        task,
        GrepContextInput.model_validate(
            _targeted(
                {"path": "sales.csv", "pattern": "North"},
                requirement_id="filter_region",
                target_fields=[candidates[1]],
            )
        ),
    )

    assert second.ok is True
    assert tools.requirements_ready_for_report() is True
    assert set(tools.registry().specs) == {"report"}

    report = _report()
    report["relevant_evidence"] = [
        {"evidence_id": first.content["evidence_id"], "supports": ["filter_region"]},
        {"evidence_id": second.content["evidence_id"], "supports": ["filter_region"]},
    ]
    report["requirement_resolutions"] = [
        {
            "requirement_id": "filter_region",
            "status": "resolved",
            "selected_field": candidates[1],
            "rejected_fields": [candidates[0]],
            "evidence_refs": [
                first.content["evidence_id"],
                second.content["evidence_id"],
            ],
            "note": "Observed values show full region labels in region_name.",
        }
    ]
    result = tools.report(task, ExplorerReportInput.model_validate(report))

    assert result.ok is True
    assembled = result.content["report"]
    assert assembled["task_requirements"][0]["ambiguous"] is True
    assert assembled["task_requirements"][0]["covered"] is True
    assert assembled["requirement_resolutions"][0]["selected_field"]["field"] == "region_name"


def test_grep_context_searches_text_and_rejects_invalid_regex(tmp_path):
    task = _task(tmp_path)
    tools = _ExplorerTools(config=ExplorerConfig())
    tools.inspect(task, InspectFilesInput())
    _lock(tools, task)

    found = tools.grep(task, _grep_input("C[12]"))
    invalid = tools.grep(task, _grep_input("["))
    unsafe_filter = tools.grep(task, _grep_input("C1", path="../outside"))

    assert found.ok is True
    assert found.content["observation"]["match_count"] == 2
    assert invalid.error_code == "INVALID_GREP_PATTERN"
    assert unsafe_filter.error_code == "PATH_NOT_INSPECTED"


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
    _lock(tools, task)

    found = tools.grep(task, _grep_input("alpha", path="0facts.db"))
    bounded_tools = _ExplorerTools(
        config=ExplorerConfig(max_single_file_bytes=1, max_total_read_bytes=1)
    )
    bounded_tools.inspect(task, InspectFilesInput())
    _lock(bounded_tools, task)
    bounded = bounded_tools.grep(task, _grep_input("alpha", path="0facts.db"))

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
    _lock(tools, task)

    result = tools.grep(task, _grep_input("match", path="long.txt"))

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
    _lock(tools, task)

    selected = tools.sql(
        task,
        _sql_input("facts.db", "SELECT id, value FROM facts", limit=2),
    )
    explained = tools.sql(
        task,
        _sql_input(
            "facts.db",
            "EXPLAIN QUERY PLAN SELECT * FROM facts WHERE id = 1",
        ),
    )
    pragma = tools.sql(
        task,
        _sql_input("facts.db", "PRAGMA table_info(facts)"),
    )
    validation_tools = _ExplorerTools(config=ExplorerConfig())
    validation_tools.inspect(task, InspectFilesInput())
    _lock(validation_tools, task)
    rejected = validation_tools.sql(
        task,
        _sql_input("facts.db", "DELETE FROM facts"),
    )
    multi_statement = validation_tools.sql(
        task,
        _sql_input("facts.db", "SELECT 1; SELECT 2"),
    )
    non_sqlite = validation_tools.sql(
        task,
        _sql_input("sales.csv", "SELECT 1"),
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
    _lock(tools, task)

    result = tools.sql(
        task,
        _sql_input("large.db", "SELECT value FROM facts"),
    )

    assert result.ok is True
    observation = result.content["observation"]
    assert len(json.dumps(observation, ensure_ascii=False, separators=(",", ":"))) <= 500
    assert observation["truncated"] is True


def test_report_ignores_unsupported_semantic_increments_without_losing_runtime_map(tmp_path):
    task = _task(tmp_path)
    tools = _ExplorerTools(config=ExplorerConfig())
    inspected = tools.inspect(task, InspectFilesInput())
    _lock(tools, task)
    inspect_ref = inspected.content["evidence_id"]

    unknown_path = _report(inspect_ref=inspect_ref)
    unknown_path["selected_sources"] = ["missing.csv"]
    path_result = tools.report(task, ExplorerReportInput.model_validate(unknown_path))

    unknown_evidence = _report()
    unknown_evidence["field_semantics"] = [
        {
            "path": "sales.csv",
            "field": "amount",
            "table": None,
            "meaning": "Sales amount.",
            "evidence_refs": ["inspect:99"],
        }
    ]
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

    for result in (path_result, evidence_result, field_result):
        assert result.ok is True
        assert result.content["report"]["files"][0]["path"] == "sales.csv"
        assert any(
            warning["message"].startswith("Ignored unsupported")
            or warning["message"].startswith("Ignored unknown")
            for warning in result.content["report"]["warnings"]
        )


def test_runtime_projects_only_selected_deep_evidence_and_omits_automatic_relations(tmp_path):
    task = _task(tmp_path)
    (task.context_dir / "customers.csv").write_text(
        "customer_id,name\nC1,Alice\nC2,Bob\n",
        encoding="utf-8",
    )
    tools = _ExplorerTools(config=ExplorerConfig())
    inspected = tools.inspect(task, InspectFilesInput())
    _lock(tools, task)
    previewed = tools.preview(task, _preview_input("sales.csv"))
    grepped = tools.grep(task, _grep_input("C1"))
    report = _report(sales_ref=previewed.content["evidence_id"])

    result = tools.report(task, ExplorerReportInput.model_validate(report))

    assert inspected.content["observation"]["relation_candidates"]
    assert grepped.ok is True
    assert result.ok is True
    assembled = result.content["report"]
    assert {item["path"] for item in assembled["files"]} == {
        "customers.csv",
        "sales.csv",
    }
    assert assembled["join_paths"] == []
    assert [item["evidence_id"] for item in assembled["evidence_summaries"]] == [
        previewed.content["evidence_id"]
    ]
    assert not any(key.startswith("grep:") for key in assembled["value_samples"])


def test_relevant_evidence_must_match_its_discovery_requirement(tmp_path):
    task = _task(tmp_path)
    tools = _ExplorerTools(config=ExplorerConfig())
    tools.inspect(task, InspectFilesInput())
    _lock(tools, task, requirement_ids=("source_data", "filter_region"))
    previewed = tools.preview(task, _preview_input("sales.csv"))
    report = _report()
    report["relevant_evidence"] = [
        {
            "evidence_id": previewed.content["evidence_id"],
            "supports": ["filter_region"],
        }
    ]

    result = tools.report(task, ExplorerReportInput.model_validate(report))

    assert result.ok is True
    assembled = result.content["report"]
    assert assembled["relevant_evidence"] == []
    assert assembled["evidence_summaries"] == []
    assert any(
        warning["message"].startswith("Ignored unsupported relevant evidence")
        for warning in assembled["warnings"]
    )


def test_final_turn_free_retry_accepts_semantic_report(tmp_path):
    task = _task(tmp_path)
    events = []
    result = ExplorerRunner(
        model=ScriptedModelAdapter(
            [
                _single_response("inspect_files", {}, "inspect"),
                _single_response(
                    "lock_requirements",
                    _lock_payload(["sales.csv"]),
                    "lock",
                ),
                _single_response("report", _report(), "retry_report"),
            ]
        ),
        config=ExplorerConfig(max_steps=2),
        event_sink=lambda kind, payload: events.append((kind, payload)),
    ).run(task, ExploreInput())

    assert result.success is True
    assert result.fallback_used is False
    assert result.steps_used == 2
    retry = next(payload for kind, payload in events if kind == "explorer_final_report_retry")
    assert retry["retry_index"] == 1
    assert retry["error_code"] == "FINAL_REPORT_REQUIRED"


def test_final_retries_and_model_failure_use_deterministic_fallback(tmp_path):
    task = _task(tmp_path)
    final_missing = ExplorerRunner(
        model=ScriptedModelAdapter(
            [
                _single_response("inspect_files", {}, "inspect"),
                _single_response("preview_file", _targeted({"path": "sales.csv"}), "late"),
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
        config=ExplorerConfig(max_inventory_chars=1_000),
    ).run(task, ExploreInput())

    assert result.fallback_used is True
    assert len(json.dumps(result.report, ensure_ascii=False, separators=(",", ":"))) <= 1_000


def test_fallback_absorbs_successful_preview_grep_and_sql_observations(tmp_path):
    task = _task(tmp_path)
    database_path = task.context_dir / "facts.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE facts (id INTEGER, label TEXT)")
        connection.executemany("INSERT INTO facts VALUES (?, ?)", [(1, "Alpha"), (2, "Beta")])
    model = ScriptedModelAdapter(
        [
            _single_response("inspect_files", {}, "inspect"),
            _single_response(
                "lock_requirements",
                _lock_payload(
                    ["facts.db", "sales.csv"],
                    requirement_ids=("sales_preview", "sales_match", "sqlite_schema"),
                ),
                "lock",
            ),
            _response(
                _call(
                    "preview_file",
                    _targeted({"path": "sales.csv"}, requirement_id="sales_preview"),
                    "preview",
                ),
                _call(
                    "grep_context",
                    _targeted(
                        {"pattern": "C1", "path": "sales.csv"},
                        requirement_id="sales_match",
                    ),
                    "grep",
                ),
            ),
            _single_response(
                "execute_context_sql",
                {
                    **_targeted(
                        {
                            "path": "facts.db",
                            "sql": "SELECT id, label FROM facts",
                            "limit": 2,
                        },
                        requirement_id="sqlite_schema",
                    ),
                },
                "sql",
            ),
        ]
    )

    result = ExplorerRunner(
        model=model,
        config=ExplorerConfig(max_steps=5),
    ).run(task, ExploreInput())

    assert result.fallback_used is True
    assert {item["source_tool"] for item in result.report["evidence_summaries"]} == {
        "preview_file",
        "grep_context",
        "execute_context_sql",
    }
    assert any(key.startswith("preview:") for key in result.report["value_samples"])
    assert any(key.startswith("grep:") for key in result.report["value_samples"])
    assert any(key.startswith("sql:") for key in result.report["value_samples"])
    assert {"inspect:1", "preview:1"}.issubset(
        result.report["schema_map"]["sales.csv"]["evidence_refs"]
    )


def test_fallback_relevance_projection_is_limited_to_eight_deep_observations(tmp_path):
    task = _task(tmp_path)
    tools = _ExplorerTools(config=ExplorerConfig())
    tools.inspect(task, InspectFilesInput())
    _lock(
        tools,
        task,
        requirement_ids=tuple(f"filter_{index}" for index in range(10)),
    )
    for index in range(10):
        tools.grep(
            task,
            _grep_input(
                "C1",
                requirement_id=f"filter_{index}",
            ),
        )

    report, _ = tools.fallback("No valid report.")

    assert len(report["relevant_evidence"]) == 8
    assert len(report["evidence_summaries"]) == 8
    assert {item["evidence_id"] for item in report["relevant_evidence"]} == {
        f"grep:{index}" for index in range(3, 11)
    }
    assert any(
        warning["message"].startswith("Fallback relevance projection omitted")
        for warning in report["warnings"]
    )


def test_budget_warnings_and_final_retry_are_observable(tmp_path):
    task = _task(tmp_path)
    requirement_ids = tuple(f"filter_{index}" for index in range(8))
    responses = [
        _single_response("inspect_files", {}, "inspect"),
        _single_response(
            "lock_requirements",
            _lock_payload(["sales.csv"], requirement_ids=requirement_ids),
            "lock",
        ),
    ]
    responses.extend(
        _single_response(
            "grep_context",
            _targeted(
                {"pattern": "amount", "path": "sales.csv"},
                requirement_id=f"filter_{index}",
            ),
            f"grep-{index}",
        )
        for index in range(7)
    )
    responses.extend(
        [
            _single_response(
                "grep_context",
                _targeted(
                    {"pattern": "amount", "path": "sales.csv"},
                    requirement_id="filter_7",
                ),
                "blocked-final",
            ),
            _single_response("report", _report(), "retry-report"),
        ]
    )
    events = []
    model = ScriptedModelAdapter(responses)

    result = ExplorerRunner(
        model=model,
        config=ExplorerConfig(max_steps=10),
        event_sink=lambda kind, payload: events.append((kind, payload)),
    ).run(task, ExploreInput())

    assert result.success is True
    assert result.steps_used == 10
    levels = [payload["level"] for kind, payload in events if kind == "explorer_budget_warning"]
    assert levels == ["warning", "critical"]
    assert model.requested_tool_names[-2:] == [("report",), ("report",)]
    assert any(
        kind == "explorer_final_report_retry" and payload["retry_index"] == 1
        for kind, payload in events
    )


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
            _single_response(
                "lock_requirements",
                _lock_payload(["sales.csv"]),
                "lock",
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
        config=ExplorerConfig(max_steps=3),
    )
    agent = ReActAgent(model=model, tools=ToolRegistry(specs=specs))

    result = agent.run(task)

    assert result.succeeded is True
    assert [step.action for step in result.steps] == ["explore", "explore", "answer"]
    assert result.steps[0].tool_call_id == "main_explore"
    assert result.steps[1].observation["content"]["error"]["code"] == "UNKNOWN_TOOL"
    assert model.requested_tool_names[0] == ("explore",)
    assert model.requested_tool_names[1] == ("inspect_files",)
    assert model.requested_tool_names[2] == ("lock_requirements",)
    assert model.requested_tool_names[3] == ("report",)
    assert "explore" not in model.requested_tool_names[4]
    assert model.requests[4][-1].tool_call_id == "main_explore"


def test_runner_forces_explore_without_injecting_inventory(tmp_path):
    task = _task(tmp_path)
    _write_task_json(task)
    model = ScriptedModelAdapter(
        [
            _single_response("explore", {}, "main_explore"),
            _single_response("inspect_files", {}, "inspect"),
            _single_response(
                "lock_requirements",
                _lock_payload(["sales.csv"]),
                "lock",
            ),
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
        explorer=AppExplorerConfig(max_steps=3),
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
