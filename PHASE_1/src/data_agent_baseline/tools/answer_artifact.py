from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

from data_agent_baseline.benchmark.schema import AnswerTable

ANSWER_ARTIFACT_HANDLE = "answer.csv"
ANSWER_ARTIFACT_MAX_BYTES = 5 * 1024 * 1024
INLINE_ANSWER_MAX_ROWS_EXCLUSIVE = 20
INLINE_ANSWER_MAX_CELLS_EXCLUSIVE = 100


class AnswerArtifactError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class LoadedAnswerArtifact:
    answer: AnswerTable
    byte_count: int
    sha256: str

    @property
    def metadata(self) -> dict[str, int | str]:
        return {
            "handle": ANSWER_ARTIFACT_HANDLE,
            "byte_count": self.byte_count,
            "row_count": len(self.answer.rows),
            "column_count": len(self.answer.columns),
            "sha256": self.sha256,
        }


def answer_artifact_path(artifact_root: Path) -> Path:
    return artifact_root / ANSWER_ARTIFACT_HANDLE


def inspect_answer_artifact(artifact_root: Path | None) -> dict[str, int | str] | None:
    if artifact_root is None:
        return None
    path = answer_artifact_path(artifact_root)
    if path.is_symlink() or not path.is_file():
        return None
    return {
        "handle": ANSWER_ARTIFACT_HANDLE,
        "byte_count": path.stat().st_size,
    }


def load_answer_artifact(artifact_root: Path | None, handle: str) -> LoadedAnswerArtifact:
    if handle != ANSWER_ARTIFACT_HANDLE:
        raise AnswerArtifactError(
            "ANSWER_ARTIFACT_PATH_INVALID",
            f"from_csv must be the fixed handle {ANSWER_ARTIFACT_HANDLE!r}.",
        )
    if artifact_root is None:
        raise AnswerArtifactError(
            "ANSWER_ARTIFACT_UNAVAILABLE",
            "This run did not allocate an answer artifact directory.",
        )

    root = artifact_root.resolve(strict=True)
    path = answer_artifact_path(root)
    if path.is_symlink():
        raise AnswerArtifactError(
            "ANSWER_ARTIFACT_PATH_INVALID",
            "The answer artifact must be a regular file, not a symbolic link.",
        )
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise AnswerArtifactError(
            "ANSWER_ARTIFACT_NOT_FOUND",
            "answer.csv does not exist; write the complete CSV to answer_csv_path first.",
        ) from exc
    if resolved.parent != root or not resolved.is_file():
        raise AnswerArtifactError(
            "ANSWER_ARTIFACT_PATH_INVALID",
            "The answer artifact must be the regular answer.csv file in this attempt.",
        )

    byte_count = resolved.stat().st_size
    if byte_count > ANSWER_ARTIFACT_MAX_BYTES:
        raise AnswerArtifactError(
            "ANSWER_ARTIFACT_TOO_LARGE",
            f"answer.csv exceeds the {ANSWER_ARTIFACT_MAX_BYTES}-byte limit.",
        )

    digest = hashlib.sha256()
    rows: list[list[str]] = []
    try:
        with resolved.open("rb") as binary_stream:
            for chunk in iter(lambda: binary_stream.read(64 * 1024), b""):
                digest.update(chunk)
        with resolved.open("r", encoding="utf-8-sig", newline="") as text_stream:
            rows = [list(row) for row in csv.reader(text_stream, strict=True)]
    except UnicodeDecodeError as exc:
        raise AnswerArtifactError(
            "ANSWER_ARTIFACT_INVALID_ENCODING",
            "answer.csv must be valid UTF-8 (an optional UTF-8 BOM is accepted).",
        ) from exc
    except csv.Error as exc:
        raise AnswerArtifactError(
            "ANSWER_ARTIFACT_INVALID_CSV",
            f"answer.csv is not valid CSV: {exc}",
        ) from exc

    if not rows:
        raise AnswerArtifactError(
            "ANSWER_ARTIFACT_EMPTY",
            "answer.csv is empty; its first row must contain the answer columns.",
        )
    columns, data_rows = rows[0], rows[1:]
    column_count = len(columns)
    if any(len(row) != column_count for row in data_rows):
        raise AnswerArtifactError(
            "ANSWER_ARTIFACT_RAGGED_ROWS",
            "Every answer.csv data row must have exactly the same width as its header.",
        )

    return LoadedAnswerArtifact(
        answer=AnswerTable(columns=columns, rows=data_rows),
        byte_count=byte_count,
        sha256=digest.hexdigest(),
    )
