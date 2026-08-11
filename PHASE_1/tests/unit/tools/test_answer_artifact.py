from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_agent_baseline.agents.model import ModelToolCall
from data_agent_baseline.benchmark.schema import PublicTask, TaskAssets, TaskRecord
from data_agent_baseline.tools.answer_artifact import (
    ANSWER_ARTIFACT_MAX_BYTES,
    AnswerArtifactError,
    load_answer_artifact,
)
from data_agent_baseline.tools.registry import create_default_tool_registry
from data_agent_baseline.verification.answer import AnswerVerifier


def _task(tmp_path: Path) -> PublicTask:
    task_dir = tmp_path / "task_1"
    context_dir = task_dir / "context"
    context_dir.mkdir(parents=True)
    return PublicTask(
        record=TaskRecord(task_id="task_1", difficulty="easy", question="Answer."),
        assets=TaskAssets(task_dir=task_dir, context_dir=context_dir),
    )


def _call(payload: dict[str, object], *, call_id: str = "call_answer") -> ModelToolCall:
    return ModelToolCall(id=call_id, name="answer", arguments=json.dumps(payload))


def _artifact_root(tmp_path: Path) -> Path:
    root = tmp_path / "artifact"
    root.mkdir()
    return root


def test_loads_utf8_bom_quoted_fields_and_preserves_strings(tmp_path):
    root = _artifact_root(tmp_path)
    (root / "answer.csv").write_text(
        '\ufeffcode,label\r\n001,"A, B"\r\n002,"line one\nline two"\r\n',
        encoding="utf-8",
    )

    loaded = load_answer_artifact(root, "answer.csv")

    assert loaded.answer.columns == ["code", "label"]
    assert loaded.answer.rows == [["001", "A, B"], ["002", "line one\nline two"]]
    assert loaded.metadata["row_count"] == 2
    assert len(loaded.sha256) == 64


@pytest.mark.parametrize(
    ("csv_text", "verification_code"),
    [
        ("value,value\none,two\n", "DUPLICATE_COLUMN_NAME"),
        (",value\none,two\n", "EMPTY_COLUMN_NAME"),
        ('value\n""\n', "ALL_NULL_COLUMN"),
        ('value\n"line one\nline two"\n', "CONTROL_CHARACTER"),
    ],
)
def test_existing_verifier_rejects_invalid_artifact_tables(tmp_path, csv_text, verification_code):
    root = _artifact_root(tmp_path)
    (root / "answer.csv").write_text(csv_text, encoding="utf-8")

    loaded = load_answer_artifact(root, "answer.csv")
    failure = AnswerVerifier().verify(loaded.answer)

    assert failure is not None
    assert failure.code == verification_code


@pytest.mark.parametrize(
    ("contents", "code"),
    [
        (b"", "ANSWER_ARTIFACT_EMPTY"),
        (b"a,b\n1\n", "ANSWER_ARTIFACT_RAGGED_ROWS"),
        (b"a\n\xff\n", "ANSWER_ARTIFACT_INVALID_ENCODING"),
        (b'a\n"unterminated\n', "ANSWER_ARTIFACT_INVALID_CSV"),
    ],
)
def test_rejects_invalid_artifact_content(tmp_path, contents, code):
    root = _artifact_root(tmp_path)
    (root / "answer.csv").write_bytes(contents)

    with pytest.raises(AnswerArtifactError, match="answer.csv") as caught:
        load_answer_artifact(root, "answer.csv")

    assert caught.value.code == code


def test_accepts_exact_size_limit_and_rejects_one_byte_over(tmp_path):
    root = _artifact_root(tmp_path)
    path = root / "answer.csv"
    path.write_bytes(b"v\n" + b"x\n" * ((ANSWER_ARTIFACT_MAX_BYTES - 2) // 2))

    loaded = load_answer_artifact(root, "answer.csv")
    assert loaded.byte_count == ANSWER_ARTIFACT_MAX_BYTES

    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(AnswerArtifactError) as caught:
        load_answer_artifact(root, "answer.csv")
    assert caught.value.code == "ANSWER_ARTIFACT_TOO_LARGE"


def test_rejects_missing_directory_symlink_and_noncanonical_handle(tmp_path):
    root = _artifact_root(tmp_path)

    with pytest.raises(AnswerArtifactError) as missing:
        load_answer_artifact(root, "answer.csv")
    assert missing.value.code == "ANSWER_ARTIFACT_NOT_FOUND"

    (root / "answer.csv").mkdir()
    with pytest.raises(AnswerArtifactError) as directory:
        load_answer_artifact(root, "answer.csv")
    assert directory.value.code == "ANSWER_ARTIFACT_PATH_INVALID"
    (root / "answer.csv").rmdir()

    outside = tmp_path / "outside.csv"
    outside.write_text("v\n1\n", encoding="utf-8")
    (root / "answer.csv").symlink_to(outside)
    with pytest.raises(AnswerArtifactError) as symlink:
        load_answer_artifact(root, "answer.csv")
    assert symlink.value.code == "ANSWER_ARTIFACT_PATH_INVALID"

    with pytest.raises(AnswerArtifactError) as escape:
        load_answer_artifact(root, "../outside.csv")
    assert escape.value.code == "ANSWER_ARTIFACT_PATH_INVALID"


def test_current_attempt_cannot_submit_another_attempt_artifact(tmp_path):
    prior_root = _artifact_root(tmp_path)
    (prior_root / "answer.csv").write_text("value\nstale\n", encoding="utf-8")
    current_root = tmp_path / "current-artifact"
    current_root.mkdir()

    with pytest.raises(AnswerArtifactError) as caught:
        load_answer_artifact(current_root, "answer.csv")

    assert caught.value.code == "ANSWER_ARTIFACT_NOT_FOUND"


def test_answer_modes_are_mutually_exclusive_and_paths_are_fixed(tmp_path):
    task = _task(tmp_path)
    registry = create_default_tool_registry(artifact_root=_artifact_root(tmp_path))

    mixed = registry.execute(
        task,
        _call({"columns": ["v"], "rows": [["1"]], "from_csv": "answer.csv"}),
    )
    absolute = registry.execute(task, _call({"from_csv": "/tmp/answer.csv"}))
    escape = registry.execute(task, _call({"from_csv": "../answer.csv"}))

    assert mixed.error_code == "ARGUMENT_VALIDATION_ERROR"
    assert absolute.error_code == "ARGUMENT_VALIDATION_ERROR"
    assert escape.error_code == "ARGUMENT_VALIDATION_ERROR"


def test_inline_boundary_and_existing_artifact_are_recoverable(tmp_path):
    task = _task(tmp_path)
    root = _artifact_root(tmp_path)
    registry = create_default_tool_registry(artifact_root=root)

    nineteen_rows = registry.execute(
        task,
        _call({"columns": ["v"], "rows": [[str(index)] for index in range(19)]}),
    )
    twenty_rows = registry.execute(
        task,
        _call({"columns": ["v"], "rows": [[str(index)] for index in range(20)]}),
    )
    ninety_cells = registry.execute(
        task,
        _call({"columns": [f"c{index}" for index in range(10)], "rows": [["1"] * 10] * 9}),
    )
    one_hundred_cells = registry.execute(
        task,
        _call({"columns": [f"c{index}" for index in range(10)], "rows": [["1"] * 10] * 10}),
    )

    assert nineteen_rows.is_terminal is True
    assert twenty_rows.error_code == "INLINE_ANSWER_TOO_LARGE"
    assert ninety_cells.is_terminal is True
    assert one_hundred_cells.error_code == "INLINE_ANSWER_TOO_LARGE"

    (root / "answer.csv").write_text("v\ncomplete\n", encoding="utf-8")
    preview = registry.execute(task, _call({"columns": ["v"], "rows": [["preview"]]}))
    assert preview.error_code == "ANSWER_ARTIFACT_AVAILABLE"


def test_submits_complete_artifact_with_bounded_arguments(tmp_path):
    task = _task(tmp_path)
    root = _artifact_root(tmp_path)
    rows = [f"{index:05d}" for index in range(10_000)]
    (root / "answer.csv").write_text(
        "code\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    registry = create_default_tool_registry(artifact_root=root)
    call = _call({"from_csv": "answer.csv"})

    result = registry.execute(task, call)

    assert len(call.arguments.encode("utf-8")) < 40
    assert result.is_terminal is True
    assert result.answer is not None
    assert result.answer.columns == ["code"]
    assert result.answer.rows == [[row] for row in rows]
    assert result.content["row_count"] == 10_000
    assert "rows" not in result.content
