from __future__ import annotations

import json
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable, Protocol, cast

from pydantic import BaseModel, ValidationError

from data_agent_baseline.benchmark.schema import AnswerTable, PublicTask
from data_agent_baseline.tools.contracts import (
    AnswerInput,
    ExecuteContextSqlInput,
    ExecutePythonInput,
    InspectSqliteSchemaInput,
    ListContextInput,
    ReadCsvInput,
    ReadDocInput,
    ReadJsonInput,
)
from data_agent_baseline.tools.answer_artifact import (
    ANSWER_ARTIFACT_HANDLE,
    ANSWER_ARTIFACT_MAX_BYTES,
    INLINE_ANSWER_MAX_CELLS_EXCLUSIVE,
    INLINE_ANSWER_MAX_ROWS_EXCLUSIVE,
    AnswerArtifactError,
    answer_artifact_path,
    inspect_answer_artifact,
    load_answer_artifact,
)
from data_agent_baseline.tools.filesystem import (
    list_context_tree,
    read_csv_preview,
    read_doc_preview,
    read_json_preview,
    resolve_context_path,
)
from data_agent_baseline.tools.python_exec import (
    PYTHON_CAPTURE_STREAM_MAX_BYTES,
    execute_python_code,
)
from data_agent_baseline.tools.sqlite import execute_read_only_sql, inspect_sqlite_schema

EXECUTE_PYTHON_TIMEOUT_SECONDS = 30
SQLITE_HEADER = b"SQLite format 3\x00"
SQLITE_TOOL_NAMES = frozenset({"execute_context_sql", "inspect_sqlite_schema"})


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    is_terminal: bool = False

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    ok: bool
    content: dict[str, Any]
    is_terminal: bool = False
    answer: AnswerTable | None = None
    action_input: dict[str, Any] | None = None
    error_code: str | None = None
    recoverable: bool = False


ToolHandler = Callable[[PublicTask, Any], ToolExecutionResult]


class ToolCall(Protocol):
    name: str
    arguments: str


def _list_context(task: PublicTask, action_input: ListContextInput) -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=True,
        content=list_context_tree(task, max_depth=action_input.max_depth),
    )


def _read_csv(task: PublicTask, action_input: ReadCsvInput) -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=True,
        content=read_csv_preview(task, action_input.path, max_rows=action_input.max_rows),
    )


def _read_json(task: PublicTask, action_input: ReadJsonInput) -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=True,
        content=read_json_preview(task, action_input.path, max_chars=action_input.max_chars),
    )


def _read_doc(task: PublicTask, action_input: ReadDocInput) -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=True,
        content=read_doc_preview(task, action_input.path, max_chars=action_input.max_chars),
    )


def _inspect_sqlite_schema(
    task: PublicTask,
    action_input: InspectSqliteSchemaInput,
) -> ToolExecutionResult:
    path = resolve_context_path(task, action_input.path)
    return ToolExecutionResult(ok=True, content=inspect_sqlite_schema(path))


def _execute_context_sql(
    task: PublicTask,
    action_input: ExecuteContextSqlInput,
) -> ToolExecutionResult:
    path = resolve_context_path(task, action_input.path)
    return ToolExecutionResult(
        ok=True,
        content=execute_read_only_sql(path, action_input.sql, limit=action_input.limit),
    )


def _execute_python(
    task: PublicTask,
    action_input: ExecutePythonInput,
    *,
    artifact_root: Path | None = None,
) -> ToolExecutionResult:
    content = execute_python_code(
        context_root=task.context_dir,
        code=action_input.code,
        timeout_seconds=EXECUTE_PYTHON_TIMEOUT_SECONDS,
        answer_csv_path=(answer_artifact_path(artifact_root) if artifact_root else None),
    )
    artifact = inspect_answer_artifact(artifact_root)
    if artifact is not None:
        content["answer_artifact"] = {
            **artifact,
            "guidance": (
                f"Submit the complete file with answer(from_csv={ANSWER_ARTIFACT_HANDLE!r}); "
                "do not print or copy its rows into the tool call."
            ),
        }
    return ToolExecutionResult(ok=bool(content.get("success")), content=content)


def _answer(
    _: PublicTask,
    action_input: AnswerInput,
    *,
    artifact_root: Path | None = None,
) -> ToolExecutionResult:
    if action_input.from_csv is not None:
        try:
            loaded = load_answer_artifact(artifact_root, action_input.from_csv)
        except AnswerArtifactError as exc:
            return _tool_error(
                code=exc.code,
                message=str(exc),
                guidance=(
                    "answer_csv_path is a predefined Path; do not assign or replace it. "
                    "Rewrite the complete CSV directly to that Path with one header row and "
                    "equal-width data rows, then submit from_csv again."
                ),
                suggested_tools=["execute_python", "answer"],
            )
        return ToolExecutionResult(
            ok=True,
            content={"status": "submitted", "source": "artifact", **loaded.metadata},
            is_terminal=True,
            answer=loaded.answer,
        )

    existing_artifact = inspect_answer_artifact(artifact_root)
    if existing_artifact is not None:
        return _tool_error(
            code="ANSWER_ARTIFACT_AVAILABLE",
            message="answer.csv already exists for this attempt; submit the complete artifact.",
            guidance=f"Call answer with only from_csv={ANSWER_ARTIFACT_HANDLE!r}.",
            suggested_tools=["answer"],
        )

    normalized_rows = [list(row) for row in action_input.rows]
    row_count = len(normalized_rows)
    cell_count = row_count * len(action_input.columns)
    if (
        row_count >= INLINE_ANSWER_MAX_ROWS_EXCLUSIVE
        or cell_count >= INLINE_ANSWER_MAX_CELLS_EXCLUSIVE
    ):
        return _tool_error(
            code="INLINE_ANSWER_TOO_LARGE",
            message=(
                "Inline answers must contain fewer than "
                f"{INLINE_ANSWER_MAX_ROWS_EXCLUSIVE} rows and fewer than "
                f"{INLINE_ANSWER_MAX_CELLS_EXCLUSIVE} data cells."
            ),
            guidance=(
                "Use the predefined answer_csv_path without assigning or replacing it. Write "
                "the complete table there with execute_python, "
                f"then call answer with from_csv={ANSWER_ARTIFACT_HANDLE!r}."
            ),
            suggested_tools=["execute_python", "answer"],
        )
    answer = AnswerTable(columns=list(action_input.columns), rows=normalized_rows)
    return ToolExecutionResult(
        ok=True,
        content={
            "status": "submitted",
            "column_count": len(action_input.columns),
            "row_count": len(normalized_rows),
        },
        is_terminal=True,
        answer=answer,
    )


def _tool_error(
    *,
    code: str,
    message: str,
    action_input: dict[str, Any] | None = None,
    guidance: str | None = None,
    suggested_tools: list[str] | None = None,
    do_not_retry_same_call: bool = False,
) -> ToolExecutionResult:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "recoverable": True,
    }
    if guidance is not None:
        error["guidance"] = guidance
    if suggested_tools:
        error["suggested_tools"] = suggested_tools
    if do_not_retry_same_call:
        error["do_not_retry_same_call"] = True
    return ToolExecutionResult(
        ok=False,
        content={"error": error},
        action_input=action_input,
        error_code=code,
        recoverable=True,
    )


def _validation_message(tool_name: str, error: ValidationError) -> str:
    details = []
    for item in error.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        details.append(f"{location}: {item['msg']}")
    return f"{tool_name} arguments are invalid: {'; '.join(details)}"


def _suggested_reader(path: str) -> str:
    suffix = path.rsplit(".", 1)[-1].casefold() if "." in path else ""
    if suffix in {"csv", "tsv"}:
        return "read_csv"
    if suffix == "json":
        return "read_json"
    if suffix in {"md", "markdown", "txt", "pdf"}:
        return "read_doc"
    return "execute_python"


def _is_non_sqlite_target(
    task: PublicTask,
    action_input: dict[str, Any],
) -> bool:
    raw_path = action_input.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return False
    try:
        path = resolve_context_path(task, raw_path)
        with path.open("rb") as stream:
            return stream.read(len(SQLITE_HEADER)) != SQLITE_HEADER
    except (OSError, ValueError):
        return False


def _execution_error(
    *,
    task: PublicTask,
    tool_name: str,
    action_input: dict[str, Any],
    exception: Exception,
) -> ToolExecutionResult:
    if isinstance(exception, FileNotFoundError):
        return _tool_error(
            code="PATH_NOT_FOUND",
            message=f"{tool_name} could not find the requested path inside task context.",
            guidance=(
                "Do not guess or retry the same path. Call list_context, or reuse an exact "
                "path from the earlier explore report's files/recommended_sources."
            ),
            suggested_tools=["list_context"],
            do_not_retry_same_call=True,
            action_input=action_input,
        )
    if tool_name in SQLITE_TOOL_NAMES and _is_non_sqlite_target(task, action_input):
        raw_path = action_input.get("path")
        suggested_tool = _suggested_reader(str(raw_path))
        return _tool_error(
            code="NOT_SQLITE",
            message=f"{tool_name} only accepts SQLite database files; this path is not SQLite.",
            guidance=(
                f"Do not retry a SQLite tool on this path. Use {suggested_tool} for this "
                "file type, or reuse the file kind and recommended source from the earlier "
                "explore report."
            ),
            suggested_tools=[suggested_tool],
            do_not_retry_same_call=True,
            action_input=action_input,
        )
    return _tool_error(
        code="TOOL_EXECUTION_ERROR",
        message=f"{tool_name} failed: {type(exception).__name__}: {exception}",
        action_input=action_input,
    )


@dataclass(frozen=True, slots=True)
class ToolRegistry:
    specs: dict[str, ToolSpec]

    def to_openai_tools(self) -> list[dict[str, Any]]:
        rendered = []
        for name in sorted(self.specs):
            rendered.append(self.specs[name].to_openai_tool())
        return rendered

    def execute(self, task: PublicTask, call: ToolCall) -> ToolExecutionResult:
        spec = self.specs.get(call.name)
        if spec is None:
            return _tool_error(
                code="UNKNOWN_TOOL",
                message=f"Unknown tool: {call.name}",
            )

        try:
            raw_arguments = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as exc:
            return _tool_error(
                code="INVALID_ARGUMENTS_JSON",
                message=f"{call.name} arguments are not valid JSON: {exc.msg}",
            )
        if not isinstance(raw_arguments, dict):
            return _tool_error(
                code="ARGUMENTS_NOT_OBJECT",
                message=f"{call.name} arguments must decode to a JSON object.",
            )

        action_input = cast(dict[str, Any], raw_arguments)
        try:
            validated = spec.input_model.model_validate(action_input)
        except ValidationError as exc:
            return _tool_error(
                code="ARGUMENT_VALIDATION_ERROR",
                message=_validation_message(call.name, exc),
                action_input=action_input,
            )

        normalized_input = validated.model_dump(mode="json")
        try:
            result = spec.handler(task, validated)
        except Exception as exc:
            return _execution_error(
                task=task,
                tool_name=call.name,
                action_input=normalized_input,
                exception=exc,
            )
        return ToolExecutionResult(
            ok=result.ok,
            content=result.content,
            is_terminal=spec.is_terminal and result.ok and result.is_terminal,
            answer=result.answer,
            action_input=normalized_input,
            error_code=result.error_code,
            recoverable=result.recoverable,
        )


def create_default_tool_registry(*, artifact_root: Path | None = None) -> ToolRegistry:
    specs = {
        "answer": ToolSpec(
            name="answer",
            description=(
                "Submit the final answer table and terminate the task. Use columns/rows "
                "for answers below 20 rows and 100 data cells. For larger answers, write "
                "the complete CSV to answer_csv_path with execute_python and submit "
                f'{{"from_csv":"{ANSWER_ARTIFACT_HANDLE}"}}. The two modes are mutually '
                "exclusive. Submit ONLY "
                "the columns explicitly requested by the question; omit join keys, "
                "filter values, source columns, rankings, and intermediate "
                "statistics. Extra columns are penalized. Example: "
                '{"columns":["average_long_shots"],"rows":[["63.5"]]}.'
            ),
            input_model=AnswerInput,
            handler=partial(_answer, artifact_root=artifact_root),
            is_terminal=True,
        ),
        "execute_context_sql": ToolSpec(
            name="execute_context_sql",
            description="Run a read-only SQL query against a sqlite/db file inside context.",
            input_model=ExecuteContextSqlInput,
            handler=_execute_context_sql,
        ),
        "execute_python": ToolSpec(
            name="execute_python",
            description=(
                "Execute arbitrary Python code with the task context directory as the "
                "working directory. The tool returns captured stdout as `output`; stdout and "
                f"stderr are each capped at {PYTHON_CAPTURE_STREAM_MAX_BYTES // 1024} KiB, "
                "with the head and tail retained when truncated. "
                f"The execution timeout is fixed at {EXECUTE_PYTHON_TIMEOUT_SECONDS} seconds. "
                "A predefined fixed Path named answer_csv_path is available for writing a "
                "complete answer CSV; never assign or replace this variable. Files are limited "
                f"to {ANSWER_ARTIFACT_MAX_BYTES // (1024 * 1024)} MiB; submit "
                f"it with answer(from_csv={ANSWER_ARTIFACT_HANDLE!r})."
            ),
            input_model=ExecutePythonInput,
            handler=partial(_execute_python, artifact_root=artifact_root),
        ),
        "inspect_sqlite_schema": ToolSpec(
            name="inspect_sqlite_schema",
            description="Inspect tables and columns in a sqlite/db file inside context.",
            input_model=InspectSqliteSchemaInput,
            handler=_inspect_sqlite_schema,
        ),
        "list_context": ToolSpec(
            name="list_context",
            description="List files and directories available under context.",
            input_model=ListContextInput,
            handler=_list_context,
        ),
        "read_csv": ToolSpec(
            name="read_csv",
            description="Read a preview of a CSV file inside context.",
            input_model=ReadCsvInput,
            handler=_read_csv,
        ),
        "read_doc": ToolSpec(
            name="read_doc",
            description="Read a text-like document inside context.",
            input_model=ReadDocInput,
            handler=_read_doc,
        ),
        "read_json": ToolSpec(
            name="read_json",
            description="Read a preview of a JSON file inside context.",
            input_model=ReadJsonInput,
            handler=_read_json,
        ),
    }
    return ToolRegistry(specs=specs)
