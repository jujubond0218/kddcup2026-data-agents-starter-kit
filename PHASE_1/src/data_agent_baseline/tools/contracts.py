from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListContextInput(ToolInput):
    max_depth: int = Field(
        default=4,
        ge=1,
        description="Maximum directory depth to include.",
    )


class ReadCsvInput(ToolInput):
    path: str = Field(description="Path relative to the task context directory.")
    max_rows: int = Field(
        default=20,
        ge=1,
        description="Maximum number of data rows to return.",
    )


class ReadJsonInput(ToolInput):
    path: str = Field(description="Path relative to the task context directory.")
    max_chars: int = Field(
        default=4000,
        ge=1,
        description="Maximum number of serialized characters to return.",
    )


class ReadDocInput(ToolInput):
    path: str = Field(description="Path relative to the task context directory.")
    max_chars: int = Field(
        default=4000,
        ge=1,
        description="Maximum number of document characters to return.",
    )


class InspectSqliteSchemaInput(ToolInput):
    path: str = Field(description="SQLite or DB path relative to the task context directory.")


class ExecuteContextSqlInput(ToolInput):
    path: str = Field(description="SQLite or DB path relative to the task context directory.")
    sql: str = Field(description="A read-only SELECT, WITH, or PRAGMA statement.")
    limit: int = Field(
        default=200,
        ge=1,
        description="Maximum number of rows to return.",
    )


class ExecutePythonInput(ToolInput):
    code: str = Field(description="Python source code to execute in the task context directory.")


class AnswerInput(ToolInput):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "examples": [
                {
                    "columns": ["average_long_shots"],
                    "rows": [["63.5"]],
                },
                {"from_csv": "answer.csv"},
            ]
        },
    )

    columns: list[str] = Field(
        default_factory=list,
        description=(
            "Names of ONLY the final result columns explicitly requested by the "
            "question. Do not include join keys, filter values, source columns, "
            "rankings, or intermediate statistics just for context: every extra "
            "column is penalized."
        ),
    )
    rows: list[list[Any]] = Field(
        default_factory=list,
        description=(
            "Final-result rows in the same order as columns. Each row must contain "
            "exactly one value per submitted final column; a single-value question "
            "normally has one column and one row."
        ),
    )
    from_csv: Literal["answer.csv"] | None = Field(
        default=None,
        description=(
            "Submit the complete CSV written to the predefined answer_csv_path exposed by "
            "execute_python. Never assign or replace that Path. This mode is mutually "
            "exclusive with columns and rows."
        ),
    )

    @model_validator(mode="after")
    def validate_mode_and_row_widths(self) -> AnswerInput:
        if self.from_csv is not None:
            if self.columns or self.rows:
                raise ValueError("from_csv is mutually exclusive with columns and rows")
            return self
        if not self.columns:
            raise ValueError("inline answer requires at least one column")
        column_count = len(self.columns)
        if any(len(row) != column_count for row in self.rows):
            raise ValueError("each row must contain exactly one value per column")
        return self
