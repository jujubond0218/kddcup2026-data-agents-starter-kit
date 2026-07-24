from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from io import StringIO
from typing import Any

from data_agent_baseline.benchmark.schema import AnswerTable


@dataclass(frozen=True, slots=True)
class AnswerVerificationFailure:
    code: str
    message: str


class AnswerVerifier:
    """Deterministically validate an in-memory answer before it becomes terminal."""

    def verify(self, answer: AnswerTable) -> AnswerVerificationFailure | None:
        if not answer.columns:
            return AnswerVerificationFailure(
                code="EMPTY_COLUMN_NAME",
                message="Answer must contain at least one non-empty column name.",
            )

        normalized_columns: list[str] = []
        for index, column in enumerate(answer.columns):
            if not column.strip():
                return AnswerVerificationFailure(
                    code="EMPTY_COLUMN_NAME",
                    message=f"Answer column {index} is blank. Provide a non-empty column name.",
                )
            if _contains_control_character(column):
                return AnswerVerificationFailure(
                    code="CONTROL_CHARACTER",
                    message=f"Answer column '{column}' contains a control character.",
                )
            normalized_columns.append(column.strip())

        if len(normalized_columns) != len(set(normalized_columns)):
            return AnswerVerificationFailure(
                code="DUPLICATE_COLUMN_NAME",
                message="Answer column names must be unique after trimming whitespace.",
            )

        column_count = len(answer.columns)
        for row_index, row in enumerate(answer.rows):
            if len(row) != column_count:
                return AnswerVerificationFailure(
                    code="ROW_WIDTH_MISMATCH",
                    message=(
                        f"Answer row {row_index} has {len(row)} values, but the answer has "
                        f"{column_count} columns."
                    ),
                )
            for column_index, value in enumerate(row):
                failure = _verify_cell(value, row_index=row_index, column_index=column_index)
                if failure is not None:
                    return failure

        for column_index, column in enumerate(answer.columns):
            if answer.rows and all(_is_null_cell(row[column_index]) for row in answer.rows):
                return AnswerVerificationFailure(
                    code="ALL_NULL_COLUMN",
                    message=(
                        f"Answer column '{column}' contains only null or empty values. "
                        "Remove it or provide the requested result values."
                    ),
                )

        try:
            rendered = StringIO(newline="")
            writer = csv.writer(rendered)
            writer.writerow(answer.columns)
            writer.writerows(answer.rows)
            rendered.seek(0)
            parsed_rows = list(csv.reader(rendered))
        except (csv.Error, TypeError, ValueError) as exc:
            return AnswerVerificationFailure(
                code="CSV_ROUND_TRIP_FAILED",
                message=f"Answer cannot be written and reread as CSV: {exc}",
            )

        expected_rows = [list(answer.columns), *[_csv_row(row) for row in answer.rows]]
        if parsed_rows != expected_rows:
            return AnswerVerificationFailure(
                code="CSV_ROUND_TRIP_FAILED",
                message="Answer changed when written and reread as CSV.",
            )
        return None


def _verify_cell(
    value: Any,
    *,
    row_index: int,
    column_index: int,
) -> AnswerVerificationFailure | None:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return AnswerVerificationFailure(
            code="NONFINITE_NUMBER",
            message=(
                f"Answer cell at row {row_index}, column {column_index} must be a finite number."
            ),
        )
    if not isinstance(value, (str, int, float, bool)):
        return AnswerVerificationFailure(
            code="UNSUPPORTED_CELL_TYPE",
            message=(
                f"Answer cell at row {row_index}, column {column_index} must be a scalar "
                "CSV value, not a nested object."
            ),
        )
    if isinstance(value, str) and _contains_control_character(value):
        return AnswerVerificationFailure(
            code="CONTROL_CHARACTER",
            message=f"Answer cell at row {row_index}, column {column_index} contains a control character.",
        )
    return None


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _is_null_cell(value: Any) -> bool:
    return value is None or value == ""


def _csv_row(row: list[Any]) -> list[str]:
    return ["" if value is None else str(value) for value in row]
