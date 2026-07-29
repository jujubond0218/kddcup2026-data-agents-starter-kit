import json
import sqlite3

from pypdf import PdfWriter

from data_agent_baseline.benchmark.schema import PublicTask, TaskAssets, TaskRecord
from data_agent_baseline.exploration.inventory import (
    InventoryLimits,
    compact_inventory,
    inspect_context,
    preview_context_file,
)


def _task(tmp_path) -> PublicTask:
    task_dir = tmp_path / "task_1"
    context_dir = task_dir / "context"
    context_dir.mkdir(parents=True)
    return PublicTask(
        record=TaskRecord(task_id="task_1", difficulty="easy", question="Inspect context."),
        assets=TaskAssets(task_dir=task_dir, context_dir=context_dir),
    )


def _limits(**overrides) -> InventoryLimits:
    defaults = {
        "max_files": 64,
        "max_inventory_chars": 12_000,
        "max_total_read_bytes": 1_000_000,
        "max_single_file_bytes": 1_000,
        "max_pdf_bytes": 1_000,
        "max_pdf_pages": 3,
    }
    return InventoryLimits(**{**defaults, **overrides})


def test_inventory_returns_bounded_mixed_context_schema(tmp_path):
    task = _task(tmp_path)
    (task.context_dir / "data.csv").write_text("id,value\n1,alpha\n2,beta\n", encoding="utf-8")
    (task.context_dir / "data.json").write_text(
        json.dumps({"id": 1, "name": "Ada"}), encoding="utf-8"
    )
    (task.context_dir / "notes.md").write_text("# Notes\n\n| a | b |\n", encoding="utf-8")
    database_path = task.context_dir / "facts.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE facts (id INTEGER, value TEXT)")

    report = inspect_context(task, _limits())

    assert report["status"] == "ok"
    entries = {entry["path"]: entry for entry in report["files"]}
    assert entries["data.csv"]["summary"]["columns"] == ["id", "value"]
    assert entries["data.json"]["summary"]["keys"] == ["id", "name"]
    assert entries["notes.md"]["summary"]["headings"] == ["Notes"]
    assert entries["facts.db"]["summary"]["tables"][0]["columns"][0]["name"] == "id"


def test_inventory_warns_for_large_and_damaged_files_without_raising(tmp_path):
    task = _task(tmp_path)
    (task.context_dir / "large.json").write_text(
        json.dumps([{"record": {"id": index, "label": f"value-{index}"}} for index in range(100)]),
        encoding="utf-8",
    )
    (task.context_dir / "broken.json").write_text("{not-json", encoding="utf-8")

    report = inspect_context(task, _limits(max_single_file_bytes=100))

    codes = {warning["code"] for warning in report["warnings"]}
    assert "JSON_STREAM_TRUNCATED" in codes
    assert "JSON_PARSE_ERROR" in codes
    assert report["budget"]["read_bytes"] <= 200
    large = next(entry for entry in report["files"] if entry["path"] == "large.json")
    assert "record.id" in large["summary"]["field_paths"]
    assert len(large["summary"]["sample_objects"]) <= 3


def test_inventory_respects_file_and_rendering_budgets(tmp_path):
    task = _task(tmp_path)
    for index in range(4):
        (task.context_dir / f"{index}.txt").write_text("x" * 200, encoding="utf-8")

    report = inspect_context(task, _limits(max_files=2, max_inventory_chars=350))

    assert report["truncated"] is True
    assert len(report["files"]) <= 2
    assert len(json.dumps(report, ensure_ascii=False, separators=(",", ":"))) <= 350


def test_inventory_skips_symlink_that_escapes_context(tmp_path):
    task = _task(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not context", encoding="utf-8")
    (task.context_dir / "outside-link.txt").symlink_to(outside)

    report = inspect_context(task, _limits())

    assert report["files"] == []
    assert any(warning["code"] == "PATH_ESCAPES_CONTEXT" for warning in report["warnings"])


def test_compact_inventory_is_detached_and_respects_prompt_budget(tmp_path):
    task = _task(tmp_path)
    for index in range(4):
        (task.context_dir / f"{index}.csv").write_text(
            "id,value\n1," + ("x" * 500) + "\n",
            encoding="utf-8",
        )
    inventory = inspect_context(task, _limits(max_inventory_chars=8_000))

    compact = compact_inventory(inventory, 2_000)

    assert len(json.dumps(compact, ensure_ascii=False, separators=(",", ":"))) <= 2_000
    assert compact["files"][0]["summary"]["columns"] == ["id", "value"]
    assert "sample_rows" not in compact["files"][0]["summary"]
    assert inventory["files"][0].get("summary") is not None


def test_inspection_builds_bounded_relation_candidates_without_trigger_state(tmp_path):
    task = _task(tmp_path)
    (task.context_dir / "orders.csv").write_text(
        "customer_id,region,amount\nC1,north,10\nC2,south,20\n",
        encoding="utf-8",
    )
    database_path = task.context_dir / "customers.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE customers (customer_id TEXT, region TEXT, amount INTEGER, name TEXT)"
        )
        connection.executemany(
            "INSERT INTO customers VALUES (?, ?, ?, ?)",
            [("C1", "north", 10, "Ada"), ("C2", "south", 20, "Lin")],
        )
    task = PublicTask(
        record=TaskRecord(
            task_id=task.task_id,
            difficulty=task.difficulty,
            question="Find the amount and region for each customer_id.",
        ),
        assets=task.assets,
    )

    report = inspect_context(task, _limits(max_inventory_chars=12_000))

    assert report["schema_version"] == 3
    assert "exploration" not in report
    assert len(report["relation_candidates"]) <= 12
    assert all(item["status"] == "candidate" for item in report["relation_candidates"])
    assert any(
        item["signals"]["sample_overlap_count"] >= 2 for item in report["relation_candidates"]
    )
    assert "_field_profiles" not in json.dumps(report)


def test_relation_candidates_remain_advisory_inspection_output(tmp_path):
    task = _task(tmp_path)
    (task.context_dir / "left.csv").write_text("id,value\n1,a\n2,b\n", encoding="utf-8")
    (task.context_dir / "right.csv").write_text("id,value\n1,a\n2,b\n", encoding="utf-8")

    report = inspect_context(task, _limits())

    assert report["relation_candidates"]
    assert "exploration" not in report


def test_targeted_preview_is_deeper_than_inventory_and_output_is_bounded(tmp_path):
    task = _task(tmp_path)
    rows = "\n".join(f"{index},value-{index}" for index in range(20))
    (task.context_dir / "data.csv").write_text(
        f"id,value\n{rows}\n",
        encoding="utf-8",
    )
    limits = _limits(max_single_file_bytes=10_000)
    inventory = inspect_context(task, limits)

    preview = preview_context_file(task, "data.csv", limits, max_chars=500)

    assert len(inventory["files"][0]["summary"]["sample_rows"]) == 2
    assert preview["summary"]["sample_row_count"] == 10
    assert len(json.dumps(preview["summary"], ensure_ascii=False, separators=(",", ":"))) <= 500


def test_targeted_preview_supports_phase1_structured_and_document_inputs(tmp_path):
    task = _task(tmp_path)
    (task.context_dir / "data.tsv").write_text("id\tvalue\n1\talpha\n", encoding="utf-8")
    (task.context_dir / "data.json").write_text(
        json.dumps({"records": [{"id": 1, "value": "alpha"}]}),
        encoding="utf-8",
    )
    (task.context_dir / "notes.md").write_text("# Rules\nUse value.", encoding="utf-8")
    database_path = task.context_dir / "facts.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE facts (id INTEGER, value TEXT)")
        connection.execute("INSERT INTO facts VALUES (1, 'alpha')")
    pdf_path = task.context_dir / "notes.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with pdf_path.open("wb") as stream:
        writer.write(stream)
    limits = _limits(
        max_single_file_bytes=10_000,
        max_pdf_bytes=10_000,
    )

    previews = {
        name: preview_context_file(task, name, limits, max_chars=1_000)
        for name in ("data.tsv", "data.json", "facts.sqlite", "notes.md", "notes.pdf")
    }

    assert previews["data.tsv"]["kind"] == "tabular"
    assert previews["data.json"]["summary"]["top_level"] == "object"
    assert previews["facts.sqlite"]["summary"]["tables"][0]["name"] == "facts"
    assert previews["notes.md"]["summary"]["headings"] == ["Rules"]
    assert previews["notes.pdf"]["kind"] == "pdf"
    assert previews["notes.pdf"]["summary"]["page_count"] == 1


def test_targeted_preview_rejects_path_escape(tmp_path):
    task = _task(tmp_path)
    outside = tmp_path / "outside.csv"
    outside.write_text("id\n1\n", encoding="utf-8")

    try:
        preview_context_file(task, "../outside.csv", _limits(), max_chars=500)
    except ValueError as exc:
        assert "escapes context dir" in str(exc)
    else:
        raise AssertionError("path escape must be rejected")
