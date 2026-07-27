from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    preview_context_file,
)
from data_agent_baseline.tools.registry import ToolExecutionResult, ToolRegistry, ToolSpec


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExploreInput(_StrictInput):
    focus: str = Field(min_length=1, max_length=500)
    candidate_paths: list[str] = Field(min_length=1, max_length=8)

    @field_validator("focus")
    @classmethod
    def _focus_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("focus must not be blank")
        return value

    @field_validator("candidate_paths")
    @classmethod
    def _candidate_paths_must_be_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("candidate_paths must not contain duplicates")
        return value


class PreviewFileInput(_StrictInput):
    path: str = Field(description="Path relative to the task context directory.")


class EvidenceBackedSource(_StrictInput):
    path: str
    reason: str = Field(min_length=1, max_length=300)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class KeyField(_StrictInput):
    path: str
    field: str = Field(min_length=1, max_length=200)
    table: str | None = Field(default=None, max_length=200)
    reason: str = Field(min_length=1, max_length=300)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class FieldReference(_StrictInput):
    path: str
    field: str = Field(min_length=1, max_length=200)
    table: str | None = Field(default=None, max_length=200)


class JoinCandidate(_StrictInput):
    status: Literal["candidate"] = "candidate"
    left: FieldReference
    right: FieldReference
    reason: str = Field(min_length=1, max_length=300)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class RecommendedCheck(_StrictInput):
    check_type: Literal[
        "source_relevance",
        "field_semantics",
        "join_coverage",
        "filter_domain",
    ]
    paths: list[str] = Field(min_length=1, max_length=2)
    fields: list[FieldReference] = Field(default_factory=list, max_length=4)
    instruction: str = Field(min_length=1, max_length=300)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class ExplorerReportInput(_StrictInput):
    selected_sources: list[EvidenceBackedSource] = Field(default_factory=list, max_length=8)
    evidence_refs: list[str] = Field(default_factory=list, max_length=16)
    key_fields: list[KeyField] = Field(default_factory=list, max_length=24)
    join_candidates: list[JoinCandidate] = Field(default_factory=list, max_length=12)
    recommended_checks: list[RecommendedCheck] = Field(default_factory=list, max_length=8)
    etl_candidates: list[EvidenceBackedSource] = Field(default_factory=list, max_length=8)
    warnings: list[str] = Field(default_factory=list, max_length=12)
    uncertainties: list[str] = Field(default_factory=list, max_length=8)


@dataclass(frozen=True, slots=True)
class ExplorerConfig:
    enabled: bool = True
    max_steps: int = 3
    max_files: int = 64
    max_preview_calls: int = 2
    max_preview_chars: int = 2_000
    max_inventory_chars: int = 12_000
    max_prompt_inventory_chars: int = 6_000
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
You are a bounded, evidence-first data-context Explorer. You do not solve the
question and you do not scan the whole context.

The caller supplies a focus and deterministic inventory evidence for selected
candidate paths. Use that evidence directly. You may preview at most two selected
files when more evidence is necessary. Every claim in report must cite evidence
IDs returned in the initial evidence or preview observations. File and field
relationships are candidates, never established facts. Call report no later than
your final turn. Do not calculate the final answer, execute Python or SQL, invent
paths, or include unsupported recommendations. Return concrete recommended_checks
when the main agent still needs to validate source relevance, field semantics,
join coverage, or a filter domain. Checks never contain executable code or SQL.
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


def _inventory_entries(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for item in inventory.get("files", []):
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            entries[item["path"]] = item
    return entries


class _ExplorerTools:
    def __init__(
        self,
        *,
        config: ExplorerConfig,
        inventory: dict[str, Any],
        candidate_paths: list[str],
    ) -> None:
        self.config = config
        self.candidate_paths = tuple(candidate_paths)
        self.preview_calls = 0
        self.evidence: dict[str, dict[str, Any]] = {}
        inventory_by_path = _inventory_entries(inventory)
        for index, path in enumerate(self.candidate_paths, start=1):
            item = inventory_by_path[path]
            evidence_id = f"inventory:{index}"
            self.evidence[evidence_id] = {
                "evidence_id": evidence_id,
                "source_tool": "context_inventory",
                "path": path,
                "kind": item.get("kind"),
                "observation": item,
            }

    def initial_evidence(self) -> list[dict[str, Any]]:
        return list(self.evidence.values())

    def preview(self, task: PublicTask, arguments: PreviewFileInput) -> ToolExecutionResult:
        if arguments.path not in self.candidate_paths:
            return _error_result(
                "PATH_NOT_SELECTED",
                "preview_file path must be one of explore.candidate_paths.",
            )
        if self.preview_calls >= self.config.max_preview_calls:
            return _error_result(
                "PREVIEW_BUDGET_EXHAUSTED",
                f"Explorer may preview at most {self.config.max_preview_calls} files.",
            )
        self.preview_calls += 1
        preview = preview_context_file(
            task,
            arguments.path,
            self.config.inventory_limits(),
            self.config.max_preview_chars,
        )
        evidence_id = f"preview:{self.preview_calls}"
        evidence = {
            "evidence_id": evidence_id,
            "source_tool": "preview_file",
            "path": arguments.path,
            "kind": preview.get("kind"),
            "observation": preview,
        }
        self.evidence[evidence_id] = evidence
        return ToolExecutionResult(ok=True, content=evidence)

    @staticmethod
    def _referenced_paths(arguments: ExplorerReportInput) -> set[str]:
        paths = {item.path for item in arguments.selected_sources}
        paths.update(item.path for item in arguments.key_fields)
        paths.update(item.path for item in arguments.etl_candidates)
        for candidate in arguments.join_candidates:
            paths.add(candidate.left.path)
            paths.add(candidate.right.path)
        for check in arguments.recommended_checks:
            paths.update(check.paths)
            paths.update(field.path for field in check.fields)
        return paths

    @staticmethod
    def _referenced_evidence(arguments: ExplorerReportInput) -> set[str]:
        refs = set(arguments.evidence_refs)
        for item in arguments.selected_sources:
            refs.update(item.evidence_refs)
        for item in arguments.key_fields:
            refs.update(item.evidence_refs)
        for item in arguments.join_candidates:
            refs.update(item.evidence_refs)
        for item in arguments.recommended_checks:
            refs.update(item.evidence_refs)
        for item in arguments.etl_candidates:
            refs.update(item.evidence_refs)
        return refs

    @staticmethod
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

    def _known_fields(self) -> dict[str, set[tuple[str | None, str]]]:
        known: dict[str, set[tuple[str | None, str]]] = {}
        for evidence in self.evidence.values():
            path = evidence.get("path")
            observation = evidence.get("observation")
            if not isinstance(path, str) or not isinstance(observation, dict):
                continue
            summary = observation.get("summary")
            if isinstance(summary, dict):
                known.setdefault(path, set()).update(self._summary_fields(summary))
        return known

    @staticmethod
    def _field_references(arguments: ExplorerReportInput) -> list[FieldReference]:
        references = [
            FieldReference(path=item.path, table=item.table, field=item.field)
            for item in arguments.key_fields
        ]
        for candidate in arguments.join_candidates:
            references.extend([candidate.left, candidate.right])
        for check in arguments.recommended_checks:
            references.extend(check.fields)
        return references

    def _unknown_field_references(
        self,
        arguments: ExplorerReportInput,
    ) -> list[FieldReference]:
        known = self._known_fields()
        unknown = []
        for reference in self._field_references(arguments):
            available = known.get(reference.path, set())
            exact = (reference.table, reference.field) in available
            unqualified = reference.table is None and any(
                field == reference.field for _, field in available
            )
            if not exact and not unqualified:
                unknown.append(reference)
        return unknown

    def report(self, _: PublicTask, arguments: ExplorerReportInput) -> ToolExecutionResult:
        unknown_paths = self._referenced_paths(arguments) - set(self.candidate_paths)
        if unknown_paths:
            return _error_result(
                "REPORT_PATH_NOT_SELECTED",
                f"Report references paths outside candidate_paths: {sorted(unknown_paths)}",
            )
        referenced_evidence = self._referenced_evidence(arguments)
        unknown_evidence = referenced_evidence - set(self.evidence)
        if unknown_evidence:
            return _error_result(
                "UNKNOWN_EVIDENCE_REF",
                f"Report references unknown evidence IDs: {sorted(unknown_evidence)}",
            )
        unknown_fields = self._unknown_field_references(arguments)
        if unknown_fields:
            rendered_fields = [
                {
                    "path": reference.path,
                    "table": reference.table,
                    "field": reference.field,
                }
                for reference in unknown_fields
            ]
            return _error_result(
                "UNKNOWN_FIELD_REF",
                f"Report references fields absent from evidence: {rendered_fields}",
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
                "evidence": [
                    self.evidence[evidence_id] for evidence_id in sorted(referenced_evidence)
                ],
            },
            is_terminal=True,
        )

    def fallback(self, reason: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        all_refs = list(self.evidence)
        report = {
            "selected_sources": [
                {
                    "path": evidence["path"],
                    "reason": "Selected by the main agent for focused exploration.",
                    "evidence_refs": [evidence_id],
                }
                for evidence_id, evidence in self.evidence.items()
                if evidence["source_tool"] == "context_inventory"
            ],
            "evidence_refs": all_refs,
            "key_fields": [],
            "join_candidates": [],
            "recommended_checks": [],
            "etl_candidates": [],
            "warnings": [reason],
            "uncertainties": ["The Explorer did not submit a validated evidence-first report."],
        }
        return report, [self.evidence[evidence_id] for evidence_id in all_refs]

    def registry(self) -> ToolRegistry:
        return ToolRegistry(
            specs={
                "preview_file": ToolSpec(
                    name="preview_file",
                    description=(
                        "Preview one file from explore.candidate_paths and receive an immutable "
                        "evidence_id. At most two previews are allowed."
                    ),
                    input_model=PreviewFileInput,
                    handler=self.preview,
                ),
                "report": ToolSpec(
                    name="report",
                    description=(
                        "Submit the evidence-first Explorer report and finish. Every claim must "
                        "reference an evidence_id already supplied by inventory or preview_file. "
                        "Use recommended_checks for bounded validations the main agent should "
                        "perform; never include executable code or SQL."
                    ),
                    input_model=ExplorerReportInput,
                    handler=self.report,
                    is_terminal=True,
                ),
            }
        )


class ExplorerRunner:
    def __init__(
        self,
        *,
        model: ModelAdapter,
        config: ExplorerConfig,
        inventory: dict[str, Any],
        event_sink: EventSink | None = None,
    ) -> None:
        self.model = model
        self.config = config
        self.inventory = inventory
        self.event_sink = event_sink

    def _fallback_result(
        self,
        *,
        tools: _ExplorerTools,
        steps_used: int,
        reason: str,
    ) -> ExplorerResult:
        report, evidence = tools.fallback(reason)
        emit_event(
            self.event_sink,
            "explorer_fallback_used",
            {"steps_used": steps_used, "reason_code": "REPORT_UNAVAILABLE"},
        )
        return ExplorerResult(
            success=True,
            report=report,
            evidence=evidence,
            steps_used=steps_used,
            fallback_used=True,
            failure_reason=reason,
        )

    def run(self, task: PublicTask, request: ExploreInput) -> ExplorerResult:
        tools = _ExplorerTools(
            config=self.config,
            inventory=self.inventory,
            candidate_paths=request.candidate_paths,
        )
        registry = tools.registry()
        messages = [
            ModelMessage(role="system", content=EXPLORER_SYSTEM_PROMPT),
            ModelMessage(
                role="user",
                content=json.dumps(
                    {
                        "question": task.question,
                        "focus": request.focus,
                        "candidate_evidence": tools.initial_evidence(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            ),
        ]
        emit_event(
            self.event_sink,
            "explorer_started",
            {
                "max_steps": self.config.max_steps,
                "candidate_path_count": len(request.candidate_paths),
            },
        )
        for step_index in range(1, self.config.max_steps + 1):
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
                )

            if len(response.tool_calls) != 1:
                message = "Explorer must call exactly one native tool per turn."
                if response.tool_calls and all(
                    call.id and call.name for call in response.tool_calls
                ):
                    messages.append(_assistant_message(response))
                    for call in response.tool_calls:
                        messages.append(_tool_message(call, {"ok": False, "error": message}))
                else:
                    messages.append(ModelMessage(role="user", content=message))
                emit_event(
                    self.event_sink,
                    "explorer_step_completed",
                    {"explorer_step_index": step_index, "ok": False, "error": "PROTOCOL_ERROR"},
                )
                continue

            call = response.tool_calls[0]
            if not call.id or not call.name:
                messages.append(
                    ModelMessage(role="user", content="Explorer tool calls require id and name.")
                )
                emit_event(
                    self.event_sink,
                    "explorer_step_completed",
                    {"explorer_step_index": step_index, "ok": False, "error": "INVALID_TOOL_CALL"},
                )
                continue
            if step_index == self.config.max_steps and call.name != "report":
                emit_event(
                    self.event_sink,
                    "explorer_step_completed",
                    {
                        "explorer_step_index": step_index,
                        "tool": call.name,
                        "ok": False,
                        "error": "FINAL_STEP_REPORT_REQUIRED",
                    },
                )
                return self._fallback_result(
                    tools=tools,
                    steps_used=step_index,
                    reason="Explorer final step did not submit report.",
                )

            messages.append(_assistant_message(response))
            result = registry.execute(task, call)
            observation = {"ok": result.ok, "tool": call.name, "content": result.content}
            messages.append(_tool_message(call, observation))
            emit_event(
                self.event_sink,
                "explorer_step_completed",
                {
                    "explorer_step_index": step_index,
                    "tool": call.name,
                    "ok": result.ok,
                    "error_code": result.error_code,
                },
            )
            if result.is_terminal and result.ok:
                emit_event(
                    self.event_sink,
                    "explorer_completed",
                    {"steps_used": step_index, "fallback_used": False},
                )
                return ExplorerResult(
                    success=True,
                    report=result.content["report"],
                    evidence=result.content["evidence"],
                    steps_used=step_index,
                )

        return self._fallback_result(
            tools=tools,
            steps_used=self.config.max_steps,
            reason="Explorer exhausted its step budget without a valid report.",
        )


@dataclass(slots=True)
class ExplorerToolHandler:
    model: ModelAdapter
    config: ExplorerConfig
    inventory: dict[str, Any]
    event_sink: EventSink | None = None
    _results: dict[tuple[str, str, tuple[str, ...]], ExplorerResult] = field(default_factory=dict)

    def __call__(self, task: PublicTask, request: ExploreInput) -> ToolExecutionResult:
        exploration = self.inventory.get("exploration")
        if isinstance(exploration, dict) and exploration.get("recommended") is True:
            expected_focus = exploration.get("focus")
            expected_paths = exploration.get("candidate_paths")
            if request.focus != expected_focus or request.candidate_paths != expected_paths:
                return _error_result(
                    "EXPLORATION_REQUEST_MISMATCH",
                    "Use exploration.focus and exploration.candidate_paths exactly as supplied.",
                )
        available_paths = set(_inventory_entries(self.inventory))
        unknown_paths = set(request.candidate_paths) - available_paths
        if unknown_paths:
            return _error_result(
                "INVALID_CANDIDATE_PATH",
                f"candidate_paths are not present in Context Inventory: {sorted(unknown_paths)}",
            )
        cache_key = (task.task_id, request.focus, tuple(request.candidate_paths))
        cached = cache_key in self._results
        result = self._results.get(cache_key)
        if result is None:
            result = ExplorerRunner(
                model=self.model,
                config=self.config,
                inventory=self.inventory,
                event_sink=self.event_sink,
            ).run(task, request)
            self._results[cache_key] = result
        return ToolExecutionResult(
            ok=True,
            content={
                "explorer_status": "fallback" if result.fallback_used else "ok",
                "focus": request.focus,
                "report": result.report,
                "evidence": result.evidence,
                "steps_used": result.steps_used,
                "fallback_used": result.fallback_used,
                "cached": cached,
            },
        )


def create_explorer_tool_spec(
    *,
    model: ModelAdapter,
    config: ExplorerConfig,
    inventory: dict[str, Any],
    event_sink: EventSink | None = None,
) -> ToolSpec:
    return ToolSpec(
        name="explore",
        description=(
            "Use only when the injected Context Inventory leaves a specific ambiguity about "
            "source selection, field semantics, joins, or document mapping. Provide a concise "
            "focus and 1-8 candidate paths copied exactly from the Inventory. A bounded "
            "evidence-first sub-agent may preview at most two selected files and returns "
            "evidence plus candidates that the main agent must verify before computation."
        ),
        input_model=ExploreInput,
        handler=ExplorerToolHandler(
            model=model,
            config=config,
            inventory=inventory,
            event_sink=event_sink,
        ),
    )
