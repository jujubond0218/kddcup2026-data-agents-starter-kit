from __future__ import annotations

import json
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from data_agent_baseline.agents.model import (
    ModelAdapter,
    ModelMessage,
    ModelResponse,
    ModelToolCall,
)
from data_agent_baseline.benchmark.schema import PublicTask
from data_agent_baseline.events import EventSink, emit_event
from data_agent_baseline.exploration.inventory import (
    InventoryLimits,
    inspect_context,
    preview_context_file,
)
from data_agent_baseline.exploration.search import grep_context
from data_agent_baseline.tools.filesystem import resolve_context_path
from data_agent_baseline.tools.registry import ToolExecutionResult, ToolRegistry, ToolSpec
from data_agent_baseline.tools.sqlite import execute_exploration_sql


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _EmptyInput(_StrictInput):
    @model_validator(mode="before")
    @classmethod
    def _discard_placeholder_properties(cls, value: Any) -> Any:
        # Some OpenAI-compatible models serialize an empty argument object as
        # {"{}": {}} or invent example placeholders even though the advertised
        # schema has no properties. These values cannot influence an argument-free
        # tool, so normalize only object-shaped payloads at this narrow boundary.
        return {} if isinstance(value, dict) else value


class ExploreInput(_EmptyInput):
    pass


class InspectFilesInput(_EmptyInput):
    pass


class PreviewFileInput(_StrictInput):
    path: str = Field(
        min_length=1,
        max_length=500,
        description="One exact relative file path returned by inspect_files.",
    )


class GrepContextInput(_StrictInput):
    pattern: str = Field(
        min_length=1,
        max_length=200,
        description="Case-insensitive regular expression used only to locate evidence.",
    )
    path: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Optional exact inspected file path or inspected directory prefix.",
    )


class ExplorerSqlInput(_StrictInput):
    path: str = Field(
        min_length=1,
        max_length=500,
        description="An inspected .db, .sqlite, or .sqlite3 file; never a CSV or document.",
    )
    sql: str = Field(
        min_length=1,
        max_length=4_000,
        description="One read-only SELECT, WITH, PRAGMA, or EXPLAIN discovery statement.",
    )
    limit: int = Field(
        default=200,
        ge=1,
        le=200,
        description="Maximum rows returned by this discovery query.",
    )


class EvidenceFile(_StrictInput):
    path: str
    format: str
    row_count: int | None = Field(default=None, ge=0)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class SchemaEntry(_StrictInput):
    path: str
    table: str | None = None
    columns: list[str] = Field(default_factory=list, max_length=64)
    semantics: dict[str, str] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class KnowledgeEntry(_StrictInput):
    path: str
    kind: Literal[
        "field_mapping",
        "formula",
        "unit",
        "value_mapping",
        "disambiguation",
        "example",
        "other",
    ]
    text: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class FieldReference(_StrictInput):
    path: str
    field: str = Field(min_length=1, max_length=200)
    table: str | None = Field(default=None, max_length=200)


class JoinPath(_StrictInput):
    status: Literal["candidate"] = "candidate"
    left: FieldReference
    right: FieldReference
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class EtlCandidate(_StrictInput):
    path: str
    reason: str = Field(min_length=1, max_length=300)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class ValueSample(_StrictInput):
    values: list[Any] = Field(max_length=20)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class EvidenceWarning(_StrictInput):
    path: str | None = None
    message: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class ExplorerReportInput(_StrictInput):
    files: list[EvidenceFile] = Field(default_factory=list, max_length=64)
    schema_map: dict[str, SchemaEntry] = Field(default_factory=dict)
    knowledge: list[KnowledgeEntry] = Field(default_factory=list, max_length=24)
    etl_candidates: list[EtlCandidate] = Field(default_factory=list, max_length=8)
    join_paths: list[JoinPath] = Field(default_factory=list, max_length=12)
    value_samples: dict[str, ValueSample] = Field(default_factory=dict)
    warnings: list[EvidenceWarning] = Field(default_factory=list, max_length=12)


@dataclass(frozen=True, slots=True)
class ExplorerConfig:
    enabled: bool = True
    max_steps: int = 10
    max_duration_seconds: float = 60.0
    max_files: int = 64
    max_preview_calls: int = 2
    max_preview_chars: int = 2_000
    max_inventory_chars: int = 12_000
    max_report_chars: int = 4_000
    max_total_read_bytes: int = 4 * 1024 * 1024
    max_single_file_bytes: int = 256 * 1024
    max_pdf_bytes: int = 2 * 1024 * 1024
    max_pdf_pages: int = 3

    def inventory_limits(self) -> InventoryLimits:
        return InventoryLimits(
            max_files=self.max_files,
            max_inventory_chars=self.max_inventory_chars,
            max_total_read_bytes=self.max_total_read_bytes,
            max_single_file_bytes=self.max_single_file_bytes,
            max_pdf_bytes=self.max_pdf_bytes,
            max_pdf_pages=self.max_pdf_pages,
        )


@dataclass(frozen=True, slots=True)
class ExplorerResult:
    success: bool
    report: dict[str, Any]
    evidence: list[dict[str, Any]]
    steps_used: int
    fallback_used: bool = False
    failure_reason: str | None = None


EXPLORER_SYSTEM_PROMPT = """
You are a Phase 1 data exploration specialist. You are discovery-only: map the
data landscape and never calculate the final answer.

Workflow:
1. Your first successful turn must call inspect_files({}) and no other tool.
2. If inspect_files lists knowledge.md (case-insensitive), you must call
   preview_file for every listed knowledge.md before report.
3. Use at most two independent tools in one turn. Targeted preview_file,
   grep_context, and read-only execute_context_sql calls may share a turn.
4. report must be the only tool call in its turn and is the only normal way to
   finish. Submit it as soon as required sources, fields, mappings, and candidate
   joins are located.

The report is a data map with files, schema_map, knowledge, advisory
etl_candidates, candidate join_paths, value_samples, and objective warnings.
Every semantic claim must cite immutable evidence IDs returned by tools.
Observed data wins on conflict. Do not execute Python, write SQL, create indexes,
perform ETL, give computation advice, or inspect anything outside context/.
""".strip()


def _assistant_message(response: ModelResponse) -> ModelMessage:
    return ModelMessage(role="assistant", content=response.content, tool_calls=response.tool_calls)


def _tool_message(call: ModelToolCall, observation: dict[str, Any]) -> ModelMessage:
    return ModelMessage(
        role="tool",
        content=json.dumps(observation, ensure_ascii=False, separators=(",", ":"), default=str),
        tool_call_id=call.id,
    )


def _error_result(code: str, message: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=False,
        content={"error": {"code": code, "message": message, "recoverable": True}},
        error_code=code,
        recoverable=True,
    )


def _summary_fields(summary: dict[str, Any]) -> set[tuple[str | None, str]]:
    fields = {(None, str(column)) for column in summary.get("columns", [])}
    fields.update((None, str(field)) for field in summary.get("field_paths", []))
    fields.update((None, str(field)) for field in summary.get("keys", []))
    for table in summary.get("tables", []):
        table_name = str(table.get("name"))
        fields.update(
            (table_name, str(column.get("name")))
            for column in table.get("columns", [])
            if column.get("name") is not None
        )
    return fields


class _ExplorerTools:
    def __init__(self, *, config: ExplorerConfig, event_sink: EventSink | None = None) -> None:
        self.config = config
        self.event_sink = event_sink
        self.inspect_attempted = False
        self.inspect_completed = False
        self.preview_calls = 0
        self.grep_calls = 0
        self.sql_calls = 0
        self.discovered_paths: set[str] = set()
        self.knowledge_paths: set[str] = set()
        self.knowledge_attempted: set[str] = set()
        self.knowledge_failures: set[str] = set()
        self.evidence: dict[str, dict[str, Any]] = {}

    def _add_evidence(
        self,
        *,
        prefix: str,
        source_tool: str,
        observation: dict[str, Any],
        path: str | None = None,
    ) -> dict[str, Any]:
        evidence_id = f"{prefix}:{sum(key.startswith(f'{prefix}:') for key in self.evidence) + 1}"
        evidence = {
            "evidence_id": evidence_id,
            "source_tool": source_tool,
            "path": path,
            "observation": observation,
        }
        self.evidence[evidence_id] = evidence
        return evidence

    def inspect(self, task: PublicTask, _: InspectFilesInput) -> ToolExecutionResult:
        if self.inspect_attempted:
            return _error_result("INSPECT_ALREADY_CALLED", "inspect_files may be called once.")
        self.inspect_attempted = True
        try:
            inspection = inspect_context(task, self.config.inventory_limits())
        except Exception as exc:  # noqa: BLE001
            emit_event(
                self.event_sink,
                "explorer_inspection_failed",
                {"error_type": type(exc).__name__},
            )
            return _error_result(
                "INSPECT_FAILED",
                f"inspect_files failed: {type(exc).__name__}.",
            )
        self.inspect_completed = True
        self.discovered_paths = {
            str(item["path"])
            for item in inspection.get("files", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        self.knowledge_paths = {
            path
            for path in self.discovered_paths
            if path.rsplit("/", 1)[-1].casefold() == "knowledge.md"
        }
        evidence = self._add_evidence(
            prefix="inspect",
            source_tool="inspect_files",
            observation=inspection,
        )
        emit_event(
            self.event_sink,
            "explorer_inspection_created",
            {
                "file_count": len(self.discovered_paths),
                "knowledge_file_count": len(self.knowledge_paths),
                "warning_count": len(inspection.get("warnings", [])),
                "read_bytes": inspection.get("budget", {}).get("read_bytes", 0),
                "truncated": bool(inspection.get("truncated")),
            },
        )
        return ToolExecutionResult(ok=True, content=evidence)

    def _require_inspection(self) -> ToolExecutionResult | None:
        if self.inspect_completed:
            return None
        return _error_result("INSPECT_REQUIRED", "Call inspect_files({}) successfully first.")

    def preview(self, task: PublicTask, arguments: PreviewFileInput) -> ToolExecutionResult:
        if error := self._require_inspection():
            return error
        if arguments.path not in self.discovered_paths:
            return _error_result(
                "PATH_NOT_INSPECTED",
                "preview_file path must be listed by inspect_files.",
            )
        if self.preview_calls >= self.config.max_preview_calls:
            return _error_result(
                "PREVIEW_BUDGET_EXHAUSTED",
                f"Explorer may preview at most {self.config.max_preview_calls} files.",
            )
        self.preview_calls += 1
        is_knowledge = arguments.path in self.knowledge_paths
        if is_knowledge:
            self.knowledge_attempted.add(arguments.path)
        try:
            preview = preview_context_file(
                task,
                arguments.path,
                self.config.inventory_limits(),
                self.config.max_preview_chars,
            )
        except Exception as exc:  # noqa: BLE001
            if is_knowledge:
                self.knowledge_failures.add(arguments.path)
                emit_event(
                    self.event_sink,
                    "explorer_knowledge_reviewed",
                    {"path": arguments.path, "ok": False, "error_type": type(exc).__name__},
                )
            evidence = self._add_evidence(
                prefix="preview",
                source_tool="preview_file",
                path=arguments.path,
                observation={
                    "path": arguments.path,
                    "warnings": [
                        {
                            "code": "PREVIEW_FAILED",
                            "message": f"preview_file failed: {type(exc).__name__}.",
                        }
                    ],
                },
            )
            return ToolExecutionResult(
                ok=False,
                content={
                    "error": {
                        "code": "PREVIEW_FAILED",
                        "message": f"preview_file failed: {type(exc).__name__}.",
                        "recoverable": True,
                    },
                    "evidence": evidence,
                },
                error_code="PREVIEW_FAILED",
                recoverable=True,
            )
        evidence = self._add_evidence(
            prefix="preview",
            source_tool="preview_file",
            path=arguments.path,
            observation=preview,
        )
        if is_knowledge:
            emit_event(
                self.event_sink,
                "explorer_knowledge_reviewed",
                {"path": arguments.path, "ok": True, "evidence_id": evidence["evidence_id"]},
            )
        return ToolExecutionResult(ok=True, content=evidence)

    def grep(self, task: PublicTask, arguments: GrepContextInput) -> ToolExecutionResult:
        if error := self._require_inspection():
            return error
        if arguments.path is not None:
            if arguments.path.startswith(("/", "\\")) or ".." in arguments.path.split("/"):
                return _error_result("INVALID_PATH_FILTER", "grep path filter must be relative.")
            normalized_filter = arguments.path.rstrip("/")
            if not any(
                path == normalized_filter or path.startswith(f"{normalized_filter}/")
                for path in self.discovered_paths
            ):
                return _error_result(
                    "PATH_NOT_INSPECTED",
                    "grep path filter must select a path listed by inspect_files.",
                )
        self.grep_calls += 1
        try:
            observation = grep_context(
                task,
                pattern=arguments.pattern,
                path_filter=arguments.path,
                max_results=30,
                max_files=self.config.max_files,
                max_total_read_bytes=self.config.max_total_read_bytes,
                max_single_file_bytes=self.config.max_single_file_bytes,
                max_output_chars=self.config.max_inventory_chars,
            )
        except ValueError as exc:
            return _error_result("INVALID_GREP_PATTERN", str(exc))
        evidence = self._add_evidence(
            prefix="grep",
            source_tool="grep_context",
            observation=observation,
        )
        return ToolExecutionResult(ok=True, content=evidence)

    def sql(self, task: PublicTask, arguments: ExplorerSqlInput) -> ToolExecutionResult:
        if error := self._require_inspection():
            return error
        if arguments.path not in self.discovered_paths:
            return _error_result(
                "PATH_NOT_INSPECTED",
                "execute_context_sql path must be listed by inspect_files.",
            )
        if not arguments.path.casefold().endswith((".db", ".sqlite", ".sqlite3")):
            return _error_result(
                "NOT_SQLITE",
                "execute_context_sql requires a .db, .sqlite, or .sqlite3 file.",
            )
        self.sql_calls += 1
        try:
            path = resolve_context_path(task, arguments.path)
            observation = execute_exploration_sql(
                path,
                arguments.sql,
                limit=arguments.limit,
                max_output_chars=self.config.max_inventory_chars,
            )
        except (OSError, ValueError) as exc:
            return _error_result("EXPLORATION_SQL_ERROR", str(exc))
        evidence = self._add_evidence(
            prefix="sql",
            source_tool="execute_context_sql",
            path=arguments.path,
            observation=observation,
        )
        return ToolExecutionResult(ok=True, content=evidence)

    @staticmethod
    def _all_evidence_refs(arguments: ExplorerReportInput) -> set[str]:
        refs: set[str] = set()
        for item in arguments.files:
            refs.update(item.evidence_refs)
        for item in arguments.schema_map.values():
            refs.update(item.evidence_refs)
        for item in arguments.knowledge:
            refs.update(item.evidence_refs)
        for item in arguments.etl_candidates:
            refs.update(item.evidence_refs)
        for item in arguments.join_paths:
            refs.update(item.evidence_refs)
        for item in arguments.value_samples.values():
            refs.update(item.evidence_refs)
        for item in arguments.warnings:
            refs.update(item.evidence_refs)
        return refs

    @staticmethod
    def _report_paths(arguments: ExplorerReportInput) -> set[str]:
        paths = {item.path for item in arguments.files}
        paths.update(item.path for item in arguments.schema_map.values())
        paths.update(item.path for item in arguments.etl_candidates)
        paths.update(item.path for item in arguments.knowledge)
        paths.update(item.path for item in arguments.warnings if item.path is not None)
        for item in arguments.join_paths:
            paths.update((item.left.path, item.right.path))
        return paths

    def _known_fields(self) -> dict[str, set[tuple[str | None, str]]]:
        known: dict[str, set[tuple[str | None, str]]] = {}
        for evidence in self.evidence.values():
            observation = evidence.get("observation")
            if not isinstance(observation, dict):
                continue
            if evidence["source_tool"] == "inspect_files":
                for item in observation.get("files", []):
                    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                        continue
                    summary = item.get("summary")
                    if isinstance(summary, dict):
                        known.setdefault(item["path"], set()).update(_summary_fields(summary))
            else:
                path = evidence.get("path")
                summary = observation.get("summary")
                if isinstance(path, str) and isinstance(summary, dict):
                    known.setdefault(path, set()).update(_summary_fields(summary))
                if isinstance(path, str) and evidence["source_tool"] == "execute_context_sql":
                    known.setdefault(path, set()).update(
                        (None, str(column)) for column in observation.get("columns", [])
                    )
        return known

    def report(self, _: PublicTask, arguments: ExplorerReportInput) -> ToolExecutionResult:
        if error := self._require_inspection():
            return error
        missing_knowledge = self.knowledge_paths - self.knowledge_attempted
        if missing_knowledge:
            return _error_result(
                "KNOWLEDGE_NOT_REVIEWED",
                f"preview_file must be attempted for: {sorted(missing_knowledge)}",
            )
        unknown_paths = self._report_paths(arguments) - self.discovered_paths
        if unknown_paths:
            return _error_result(
                "UNKNOWN_REPORT_PATH",
                f"Report references paths absent from inspect_files: {sorted(unknown_paths)}",
            )
        reported_paths = {item.path for item in arguments.files}
        missing_paths = self.discovered_paths - reported_paths
        if missing_paths:
            return _error_result(
                "INCOMPLETE_FILE_MAP",
                f"Report files must include every inspected path: {sorted(missing_paths)}",
            )
        represented_knowledge = {item.path for item in arguments.knowledge}
        represented_knowledge.update(
            item.path for item in arguments.warnings if item.path in self.knowledge_paths
        )
        missing_knowledge_mapping = self.knowledge_paths - represented_knowledge
        if missing_knowledge_mapping:
            return _error_result(
                "KNOWLEDGE_NOT_MAPPED",
                "Each knowledge.md requires extracted knowledge or an evidence-backed warning: "
                f"{sorted(missing_knowledge_mapping)}",
            )
        for item in [*arguments.knowledge, *arguments.warnings]:
            if item.path not in self.knowledge_paths:
                continue
            if not any(
                self.evidence[ref]["source_tool"] == "preview_file"
                and self.evidence[ref].get("path") == item.path
                for ref in item.evidence_refs
                if ref in self.evidence
            ):
                return _error_result(
                    "KNOWLEDGE_EVIDENCE_REQUIRED",
                    f"Knowledge mapping for {item.path} must cite its preview_file evidence.",
                )
        evidence_refs = self._all_evidence_refs(arguments)
        unknown_refs = evidence_refs - set(self.evidence)
        if unknown_refs:
            return _error_result(
                "UNKNOWN_EVIDENCE_REF",
                f"Report references unknown evidence IDs: {sorted(unknown_refs)}",
            )
        known_fields = self._known_fields()
        unknown_fields = []
        for join in arguments.join_paths:
            for reference in (join.left, join.right):
                available = known_fields.get(reference.path, set())
                if (reference.table, reference.field) not in available and not any(
                    field == reference.field for _, field in available
                ):
                    unknown_fields.append(
                        {
                            "path": reference.path,
                            "table": reference.table,
                            "field": reference.field,
                        }
                    )
        if unknown_fields:
            return _error_result(
                "UNKNOWN_FIELD_REF",
                f"Report references fields absent from evidence: {unknown_fields}",
            )
        report = arguments.model_dump(mode="json")
        rendered = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) > self.config.max_report_chars:
            return _error_result(
                "REPORT_TOO_LARGE",
                f"Explorer report exceeds {self.config.max_report_chars} characters.",
            )
        return ToolExecutionResult(
            ok=True,
            content={
                "status": "reported",
                "report": report,
                "evidence": [self.evidence[ref] for ref in sorted(evidence_refs)],
            },
            is_terminal=True,
        )

    def fallback(self, reason: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        inspection_ref = next(
            (
                evidence_id
                for evidence_id, evidence in self.evidence.items()
                if evidence["source_tool"] == "inspect_files"
            ),
            None,
        )
        files: list[dict[str, Any]] = []
        schema_map: dict[str, dict[str, Any]] = {}
        if inspection_ref is not None:
            inspection = self.evidence[inspection_ref]["observation"]
            for item in inspection.get("files", []):
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    continue
                path = item["path"]
                summary = item.get("summary", {})
                row_count = summary.get("row_count") if isinstance(summary, dict) else None
                files.append(
                    {
                        "path": path,
                        "format": str(item.get("kind", "unknown")),
                        "row_count": row_count if isinstance(row_count, int) else None,
                        "evidence_refs": [inspection_ref],
                    }
                )
                columns = summary.get("columns", []) if isinstance(summary, dict) else []
                schema_map[path] = {
                    "path": path,
                    "table": None,
                    "columns": [str(column) for column in columns[:64]],
                    "semantics": {},
                    "evidence_refs": [inspection_ref],
                }
        evidence_refs = list(self.evidence)
        warning_refs = evidence_refs[:1]
        report = {
            "files": files,
            "schema_map": schema_map,
            "knowledge": [],
            "etl_candidates": [],
            "join_paths": [],
            "value_samples": {},
            "warnings": (
                [{"message": reason, "evidence_refs": warning_refs}] if warning_refs else []
            ),
        }
        truncated = False
        while (
            len(json.dumps(report, ensure_ascii=False, separators=(",", ":"), default=str))
            > self.config.max_report_chars
            and report["schema_map"]
        ):
            report["schema_map"].pop(next(reversed(report["schema_map"])))
            truncated = True
        while (
            len(json.dumps(report, ensure_ascii=False, separators=(",", ":"), default=str))
            > self.config.max_report_chars
            and report["files"]
        ):
            report["files"].pop()
            truncated = True
        if truncated and warning_refs:
            report["warnings"].append(
                {
                    "message": "Fallback data map was truncated to the report character budget.",
                    "evidence_refs": warning_refs,
                }
            )
            while (
                len(json.dumps(report, ensure_ascii=False, separators=(",", ":"), default=str))
                > self.config.max_report_chars
                and report["files"]
            ):
                report["files"].pop()
        return report, list(self.evidence.values())

    def registry(self, *, final_step: bool = False) -> ToolRegistry:
        specs = {
            "execute_context_sql": ToolSpec(
                name="execute_context_sql",
                description=(
                    "Run bounded read-only SELECT, WITH, PRAGMA, or EXPLAIN SQL against an "
                    "inspected SQLite file. Never compute the final answer."
                ),
                input_model=ExplorerSqlInput,
                handler=self.sql,
            ),
            "grep_context": ToolSpec(
                name="grep_context",
                description=(
                    "Search a bounded case-insensitive regex across inspected Phase 1 text "
                    "and SQLite sources. Use only to locate evidence, not aggregate data."
                ),
                input_model=GrepContextInput,
                handler=self.grep,
            ),
            "inspect_files": ToolSpec(
                name="inspect_files",
                description=(
                    "Use first and alone to build a bounded map of every Phase 1 context "
                    "file, schema, warning, and candidate relationship."
                ),
                input_model=InspectFilesInput,
                handler=self.inspect,
            ),
            "preview_file": ToolSpec(
                name="preview_file",
                description=(
                    "Preview one path returned by inspect_files and receive immutable "
                    "evidence. Every knowledge.md must be previewed before report."
                ),
                input_model=PreviewFileInput,
                handler=self.preview,
            ),
            "report": ToolSpec(
                name="report",
                description=(
                    "Submit the evidence-backed data map and finish. Must be the only call "
                    "in its turn; joins and ETL entries remain advisory candidates."
                ),
                input_model=ExplorerReportInput,
                handler=self.report,
                is_terminal=True,
            ),
        }
        if not self.inspect_completed:
            return ToolRegistry(specs={"inspect_files": specs["inspect_files"]})
        specs.pop("inspect_files")
        if not any(
            path.casefold().endswith((".db", ".sqlite", ".sqlite3"))
            for path in self.discovered_paths
        ):
            specs.pop("execute_context_sql")
        if self.preview_calls >= self.config.max_preview_calls:
            specs.pop("preview_file")
        if final_step:
            return ToolRegistry(specs={"report": specs["report"]})
        return ToolRegistry(specs=specs)


class ExplorerRunner:
    def __init__(
        self,
        *,
        model: ModelAdapter,
        config: ExplorerConfig,
        event_sink: EventSink | None = None,
    ) -> None:
        self.model = model
        self.config = config
        self.event_sink = event_sink

    def _fallback_result(
        self,
        *,
        tools: _ExplorerTools,
        steps_used: int,
        reason: str,
        reason_code: str,
    ) -> ExplorerResult:
        report, evidence = tools.fallback(reason)
        emit_event(
            self.event_sink,
            "explorer_fallback_used",
            {"steps_used": steps_used, "reason_code": reason_code},
        )
        emit_event(
            self.event_sink,
            "explorer_completed",
            {"steps_used": steps_used, "fallback_used": True, "success": bool(evidence)},
        )
        return ExplorerResult(
            success=bool(evidence),
            report=report,
            evidence=evidence,
            steps_used=steps_used,
            fallback_used=True,
            failure_reason=reason,
        )

    @staticmethod
    def _protocol_observation(code: str, message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "content": {"error": {"code": code, "message": message, "recoverable": True}},
        }

    def run(self, task: PublicTask, _: ExploreInput) -> ExplorerResult:
        tools = _ExplorerTools(config=self.config, event_sink=self.event_sink)
        messages = [
            ModelMessage(role="system", content=EXPLORER_SYSTEM_PROMPT),
            ModelMessage(
                role="user",
                content=(
                    f"Question: {task.question}\n"
                    "Begin with inspect_files({}). All paths are relative to context/."
                ),
            ),
        ]
        started_at = monotonic()
        emit_event(
            self.event_sink,
            "explorer_started",
            {
                "max_steps": self.config.max_steps,
                "max_duration_seconds": self.config.max_duration_seconds,
            },
        )
        for step_index in range(1, self.config.max_steps + 1):
            if monotonic() - started_at >= self.config.max_duration_seconds:
                return self._fallback_result(
                    tools=tools,
                    steps_used=step_index - 1,
                    reason="Explorer exceeded its soft time budget.",
                    reason_code="SOFT_TIMEOUT",
                )
            registry = tools.registry(final_step=step_index == self.config.max_steps)
            try:
                response = self.model.complete(
                    messages,
                    tools=registry,
                    request_context={
                        "task_id": task.task_id,
                        "agent_scope": "explorer",
                        "explorer_step_index": step_index,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                return self._fallback_result(
                    tools=tools,
                    steps_used=step_index,
                    reason=f"Explorer model request failed: {type(exc).__name__}.",
                    reason_code="MODEL_FAILURE",
                )

            calls = response.tool_calls
            replayable = bool(calls) and all(call.id and call.name for call in calls)
            protocol_error: tuple[str, str] | None = None
            if not calls:
                protocol_error = ("NO_TOOL_CALL", "Explorer must call one or two native tools.")
            elif len(calls) > 2:
                protocol_error = (
                    "TOO_MANY_TOOL_CALLS",
                    "Explorer may call at most two tools in one model turn.",
                )
            elif not replayable:
                protocol_error = ("INVALID_TOOL_CALL", "Explorer tool calls require id and name.")
            elif not tools.inspect_completed and (
                len(calls) != 1 or calls[0].name != "inspect_files"
            ):
                protocol_error = (
                    "INSPECT_REQUIRED",
                    "The first successful Explorer turn must call inspect_files alone.",
                )
            elif any(call.name == "report" for call in calls) and (
                len(calls) != 1 or calls[0].name != "report"
            ):
                protocol_error = (
                    "REPORT_MUST_BE_EXCLUSIVE",
                    "report must be the only tool call in its turn.",
                )

            if protocol_error is not None:
                code, message = protocol_error
                if replayable:
                    messages.append(_assistant_message(response))
                    for call in calls:
                        messages.append(
                            _tool_message(call, self._protocol_observation(code, message))
                        )
                else:
                    messages.append(
                        ModelMessage(
                            role="user",
                            content=f"Explorer protocol error ({code}): {message}",
                        )
                    )
                emit_event(
                    self.event_sink,
                    "explorer_step_completed",
                    {
                        "explorer_step_index": step_index,
                        "ok": False,
                        "error_code": code,
                        "tool_call_count": len(calls),
                    },
                )
                continue

            if step_index == self.config.max_steps and calls[0].name != "report":
                return self._fallback_result(
                    tools=tools,
                    steps_used=step_index,
                    reason="Explorer final turn did not submit report.",
                    reason_code="FINAL_REPORT_MISSING",
                )

            messages.append(_assistant_message(response))
            terminal_result: ToolExecutionResult | None = None
            for call_index, call in enumerate(calls):
                if monotonic() - started_at >= self.config.max_duration_seconds:
                    return self._fallback_result(
                        tools=tools,
                        steps_used=step_index,
                        reason="Explorer exceeded its soft time budget.",
                        reason_code="SOFT_TIMEOUT",
                    )
                result = registry.execute(task, call)
                messages.append(
                    _tool_message(
                        call,
                        {"ok": result.ok, "tool": call.name, "content": result.content},
                    )
                )
                emit_event(
                    self.event_sink,
                    "explorer_step_completed",
                    {
                        "explorer_step_index": step_index,
                        "tool_call_index": call_index,
                        "tool_call_id": call.id,
                        "tool": call.name,
                        "ok": result.ok,
                        "error_code": result.error_code,
                    },
                )
                if call.name == "inspect_files" and result.error_code == "INSPECT_FAILED":
                    return self._fallback_result(
                        tools=tools,
                        steps_used=step_index,
                        reason="Explorer could not inspect the task context.",
                        reason_code="INSPECT_FAILED",
                    )
                if result.is_terminal and result.ok:
                    terminal_result = result

            if terminal_result is not None:
                emit_event(
                    self.event_sink,
                    "explorer_completed",
                    {"steps_used": step_index, "fallback_used": False},
                )
                return ExplorerResult(
                    success=True,
                    report=terminal_result.content["report"],
                    evidence=terminal_result.content["evidence"],
                    steps_used=step_index,
                )

        return self._fallback_result(
            tools=tools,
            steps_used=self.config.max_steps,
            reason="Explorer exhausted its step budget without a valid report.",
            reason_code="STEP_BUDGET_EXHAUSTED",
        )


@dataclass(slots=True)
class ExplorerToolHandler:
    model: ModelAdapter
    config: ExplorerConfig
    event_sink: EventSink | None = None
    _results: dict[str, ExplorerResult] = field(default_factory=dict)

    def __call__(self, task: PublicTask, request: ExploreInput) -> ToolExecutionResult:
        cached = task.task_id in self._results
        result = self._results.get(task.task_id)
        if result is None:
            result = ExplorerRunner(
                model=self.model,
                config=self.config,
                event_sink=self.event_sink,
            ).run(task, request)
            self._results[task.task_id] = result
        return ToolExecutionResult(
            ok=True,
            content={
                "explorer_status": (
                    "fallback" if result.fallback_used else ("ok" if result.success else "failed")
                ),
                "report": result.report,
                "evidence_index": [
                    {
                        "evidence_id": evidence["evidence_id"],
                        "source_tool": evidence["source_tool"],
                        "path": evidence.get("path"),
                    }
                    for evidence in result.evidence
                ],
                "evidence_count": len(result.evidence),
                "steps_used": result.steps_used,
                "fallback_used": result.fallback_used,
                "cached": cached,
            },
        )


def create_explorer_tool_spec(
    *,
    model: ModelAdapter,
    config: ExplorerConfig,
    event_sink: EventSink | None = None,
) -> ToolSpec:
    return ToolSpec(
        name="explore",
        description=(
            "Use first as explore({}). It launches a Phase 1 discovery-only sub-agent that "
            "inspects all context files, explicitly reads knowledge.md, performs bounded "
            "preview/grep/read-only SQL discovery, and returns an evidence-backed data map."
        ),
        input_model=ExploreInput,
        handler=ExplorerToolHandler(
            model=model,
            config=config,
            event_sink=event_sink,
        ),
    )
