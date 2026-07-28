from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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


class CandidateField(_StrictInput):
    path: str = Field(min_length=1, max_length=500)
    field: str = Field(min_length=1, max_length=200)
    table: str | None = Field(default=None, max_length=200)


class LockedRequirement(_StrictInput):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    kind: Literal[
        "entity",
        "measure",
        "filter",
        "time_scope",
        "output",
        "knowledge",
        "join",
        "other",
    ]
    description: str = Field(min_length=1, max_length=300)
    candidate_paths: list[str] = Field(min_length=1, max_length=8)
    candidate_fields: list[CandidateField] = Field(default_factory=list, max_length=8)
    search_terms: list[str] = Field(default_factory=list, max_length=8)
    needs_discovery: bool = False

    @field_validator("candidate_paths", "search_terms")
    @classmethod
    def _validate_unique_strings(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Values must be unique.")
        if any(not value or len(value) > 500 for value in values):
            raise ValueError("Candidate paths and search terms must contain 1-500 characters.")
        return values

    @field_validator("candidate_fields")
    @classmethod
    def _validate_unique_fields(cls, values: list[CandidateField]) -> list[CandidateField]:
        identities = {(item.path, item.table, item.field) for item in values}
        if len(values) != len(identities):
            raise ValueError("candidate_fields must be unique.")
        return values


class LockRequirementsInput(_StrictInput):
    requirements: list[LockedRequirement] = Field(min_length=1, max_length=12)

    @field_validator("requirements")
    @classmethod
    def _validate_unique_requirements(
        cls,
        values: list[LockedRequirement],
    ) -> list[LockedRequirement]:
        identifiers = [item.id for item in values]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("requirement IDs must be unique.")
        return values


class _TargetedDiscoveryInput(_StrictInput):
    requirement_ids: list[str] = Field(
        min_length=1,
        max_length=4,
        description=(
            "Stable task requirement IDs supported by this discovery call, such as "
            "filter_region or measure_revenue."
        ),
    )
    purpose: str = Field(
        min_length=1,
        max_length=300,
        description="Why this call is necessary for the task question.",
    )
    target_fields: list[CandidateField] = Field(
        default_factory=list,
        max_length=8,
        description="Locked candidate fields this call will inspect or disambiguate.",
    )

    @field_validator("requirement_ids")
    @classmethod
    def _validate_requirement_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("requirement_ids must be unique.")
        if any(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None for value in values):
            raise ValueError("requirement_ids must be lowercase identifiers such as filter_region.")
        return values


class PreviewFileInput(_TargetedDiscoveryInput):
    path: str = Field(
        min_length=1,
        max_length=500,
        description="One exact relative file path returned by inspect_files.",
    )


class GrepContextInput(_TargetedDiscoveryInput):
    pattern: str = Field(
        min_length=1,
        max_length=200,
        description="Case-insensitive regular expression used only to locate evidence.",
    )
    path: str = Field(
        min_length=1,
        max_length=500,
        description="One exact candidate path declared by lock_requirements.",
    )


class ExplorerSqlInput(_TargetedDiscoveryInput):
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


class FieldSemantic(_StrictInput):
    path: str
    field: str = Field(min_length=1, max_length=200)
    table: str | None = Field(default=None, max_length=200)
    meaning: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class EvidenceWarning(_StrictInput):
    path: str | None = None
    message: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class RelevantEvidence(_StrictInput):
    evidence_id: str = Field(min_length=1, max_length=100)
    supports: list[str] = Field(min_length=1, max_length=4)


class RequirementResolution(_StrictInput):
    requirement_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    status: Literal["resolved", "unresolved"]
    selected_field: CandidateField | None = None
    rejected_fields: list[CandidateField] = Field(default_factory=list, max_length=8)
    evidence_refs: list[str] = Field(default_factory=list, max_length=8)
    note: str | None = Field(default=None, max_length=300)


class ExplorerReportInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    relevant_evidence: list[RelevantEvidence] = Field(default_factory=list, max_length=24)
    requirement_resolutions: list[RequirementResolution] = Field(
        default_factory=list, max_length=12
    )
    selected_sources: list[str] = Field(default_factory=list, max_length=16)
    field_semantics: list[FieldSemantic] = Field(default_factory=list, max_length=32)
    knowledge: list[KnowledgeEntry] = Field(default_factory=list, max_length=24)
    etl_candidates: list[EtlCandidate] = Field(default_factory=list, max_length=8)
    join_paths: list[JoinPath] = Field(default_factory=list, max_length=12)
    warnings: list[EvidenceWarning] = Field(default_factory=list, max_length=12)
    uncertainties: list[EvidenceWarning] = Field(default_factory=list, max_length=12)

    @model_validator(mode="before")
    @classmethod
    def _keep_valid_semantic_increments(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return {}
        item_models: dict[str, type[BaseModel]] = {
            "relevant_evidence": RelevantEvidence,
            "requirement_resolutions": RequirementResolution,
            "field_semantics": FieldSemantic,
            "knowledge": KnowledgeEntry,
            "etl_candidates": EtlCandidate,
            "join_paths": JoinPath,
            "warnings": EvidenceWarning,
            "uncertainties": EvidenceWarning,
        }
        item_limits = {
            "relevant_evidence": 24,
            "requirement_resolutions": 12,
            "field_semantics": 32,
            "knowledge": 24,
            "etl_candidates": 8,
            "join_paths": 12,
            "warnings": 12,
            "uncertainties": 12,
        }
        normalized: dict[str, Any] = {}
        for field_name, item_model in item_models.items():
            raw_items = value.get(field_name, [])
            if not isinstance(raw_items, list):
                normalized[field_name] = []
                continue
            valid_items = []
            for item in raw_items:
                try:
                    valid_items.append(item_model.model_validate(item).model_dump(mode="json"))
                except ValidationError:
                    continue
            normalized[field_name] = valid_items[: item_limits[field_name]]
        selected_sources = value.get("selected_sources", [])
        normalized["selected_sources"] = (
            [item for item in selected_sources if isinstance(item, str)][:16]
            if isinstance(selected_sources, list)
            else []
        )
        return normalized


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
2. Your second successful turn must call lock_requirements alone. Decompose the
   question into stable requirements for entities, measures, filters, time scope,
   output fields, knowledge, and joins. For each requirement, name only candidate
   paths and fields that inspect_files actually returned. Mark needs_discovery=true
   only when inspect evidence is insufficient. Multiple candidate_fields mean an
   explicit ambiguity that should be checked, not a guessed relationship. Derive
   requirements and search_terms only from the question. For a question phrase
   that can map to several real fields, lock every plausible field; do not add a
   field merely because its name is similar. The runtime normalizes ambiguity flags
   and adds any required knowledge.md review, so submit the semantic plan once
   instead of retrying it for bookkeeping details.
3. After locking, every preview_file, grep_context, and execute_context_sql call
   must cite locked requirement_ids, candidate target_fields, and a concise purpose.
   Do not explore a path or field outside the locked plan. If inspect_files lists
   knowledge.md (case-insensitive), include it in a knowledge requirement and call
   preview_file for every listed knowledge.md before report.
4. Use at most two independent tools in one turn. Targeted preview_file,
   grep_context, and read-only execute_context_sql calls may share a turn.
5. The runtime tracks coverage of the immutable requirements. Starting with Turn
   3, report is available alongside discovery tools after required knowledge review;
   use it as soon as the evidence needed by the question is sufficient. Resolve
   schema ambiguity by comparing candidate values, value ambiguity with exact
   observed categories and knowledge rules, and numeric references with observed
   ranges. Leave vague semantics unresolved when evidence cannot decide. Do not
   cross-check already covered requirements in extra sources, and normally finish
   by Turn 6 even though the hard safety limit is larger.
6. report must be the only tool call in its turn and is the only normal way to
   finish. An incomplete semantic report is better than no report.

The runtime always preserves a compact background inventory, but it includes deep
preview/grep/SQL observations only when report.relevant_evidence selects them and
binds them to locked requirements. Do not select evidence merely because it was
collected. Submit relevant_evidence, requirement_resolutions, selected_sources,
field_semantics, knowledge rules, advisory etl_candidates, candidate join_paths,
objective warnings, and uncertainties. The runtime owns the requirements, file
inventory, schemas, and evidence provenance. Every semantic claim must cite
selected immutable evidence IDs returned by tools.
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
    _MAX_DISCOVERY_CALLS_PER_REQUIREMENT = 3

    def __init__(self, *, config: ExplorerConfig, event_sink: EventSink | None = None) -> None:
        self.config = config
        self.event_sink = event_sink
        self.inspect_attempted = False
        self.inspect_completed = False
        self.requirements_locked = False
        self.preview_calls = 0
        self.grep_calls = 0
        self.sql_calls = 0
        self.discovered_paths: set[str] = set()
        self.knowledge_paths: set[str] = set()
        self.knowledge_attempted: set[str] = set()
        self.knowledge_failures: set[str] = set()
        self.locked_requirements: dict[str, LockedRequirement] = {}
        self.requirement_call_counts: dict[str, int] = {}
        self.evidence: dict[str, dict[str, Any]] = {}

    def _add_evidence(
        self,
        *,
        prefix: str,
        source_tool: str,
        observation: dict[str, Any],
        path: str | None = None,
        requirement_ids: list[str] | None = None,
        purpose: str | None = None,
        target_fields: list[CandidateField] | None = None,
        ok: bool = True,
    ) -> dict[str, Any]:
        evidence_id = f"{prefix}:{sum(key.startswith(f'{prefix}:') for key in self.evidence) + 1}"
        evidence = {
            "evidence_id": evidence_id,
            "source_tool": source_tool,
            "path": path,
            "observation": observation,
            "requirement_ids": list(requirement_ids or []),
            "purpose": purpose,
            "target_fields": [item.model_dump(mode="json") for item in (target_fields or [])],
            "ok": ok,
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

    @staticmethod
    def _field_identity(field: CandidateField) -> tuple[str, str | None, str]:
        return (field.path, field.table, field.field)

    def _candidate_field_known(
        self,
        candidate: CandidateField,
        known_fields: dict[str, set[tuple[str | None, str]]],
    ) -> bool:
        available = known_fields.get(candidate.path, set())
        if candidate.table is not None:
            return (candidate.table, candidate.field) in available
        return any(field == candidate.field for _, field in available)

    def lock_requirements(
        self,
        _: PublicTask,
        arguments: LockRequirementsInput,
    ) -> ToolExecutionResult:
        if error := self._require_inspection():
            return error
        if self.requirements_locked:
            return _error_result(
                "REQUIREMENTS_ALREADY_LOCKED",
                "Task requirements are immutable after the first successful lock.",
            )
        rendered = json.dumps(
            arguments.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(rendered) > self.config.max_report_chars:
            return _error_result(
                "REQUIREMENT_PLAN_TOO_LARGE",
                f"Requirement plan exceeds {self.config.max_report_chars} characters.",
            )

        known_fields = self._known_fields()
        normalized_requirements: list[LockedRequirement] = []
        normalized_ambiguities = 0
        for requirement in arguments.requirements:
            unknown_paths = set(requirement.candidate_paths) - self.discovered_paths
            if unknown_paths:
                return _error_result(
                    "UNKNOWN_CANDIDATE_PATH",
                    f"Requirement {requirement.id} references uninspected paths: "
                    f"{sorted(unknown_paths)}",
                )
            for candidate in requirement.candidate_fields:
                if candidate.path not in requirement.candidate_paths:
                    return _error_result(
                        "CANDIDATE_PATH_MISMATCH",
                        f"Candidate field {candidate.path}.{candidate.field} is outside "
                        f"requirement {requirement.id} candidate_paths.",
                    )
                if not self._candidate_field_known(candidate, known_fields):
                    return _error_result(
                        "UNKNOWN_CANDIDATE_FIELD",
                        f"Inventory does not contain candidate field "
                        f"{candidate.path}.{candidate.field}.",
                    )
            needs_discovery = requirement.needs_discovery or len(requirement.candidate_fields) > 1
            if needs_discovery != requirement.needs_discovery:
                normalized_ambiguities += 1
            normalized_requirements.append(
                requirement.model_copy(
                    update={"needs_discovery": needs_discovery},
                )
            )

        uncovered_knowledge = self.knowledge_paths - {
            path
            for requirement in normalized_requirements
            if requirement.kind == "knowledge" and requirement.needs_discovery
            for path in requirement.candidate_paths
        }
        used_ids = {requirement.id for requirement in normalized_requirements}
        for index, path in enumerate(sorted(uncovered_knowledge), start=1):
            identifier = "knowledge_context"
            if identifier in used_ids:
                identifier = f"knowledge_context_{index}"
            while identifier in used_ids:
                index += 1
                identifier = f"knowledge_context_{index}"
            used_ids.add(identifier)
            normalized_requirements.append(
                LockedRequirement(
                    id=identifier,
                    kind="knowledge",
                    description="Review task-provided knowledge rules relevant to the question.",
                    candidate_paths=[path],
                    candidate_fields=[],
                    search_terms=[],
                    needs_discovery=True,
                )
            )

        normalized_rendered = json.dumps(
            [item.model_dump(mode="json") for item in normalized_requirements],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(normalized_rendered) > self.config.max_report_chars:
            return _error_result(
                "REQUIREMENT_PLAN_TOO_LARGE",
                f"Normalized requirement plan exceeds {self.config.max_report_chars} characters.",
            )

        self.locked_requirements = {item.id: item for item in normalized_requirements}
        self.requirement_call_counts = {item.id: 0 for item in normalized_requirements}
        self.requirements_locked = True
        ambiguous = [item.id for item in normalized_requirements if len(item.candidate_fields) > 1]
        emit_event(
            self.event_sink,
            "explorer_requirements_locked",
            {
                "requirement_count": len(normalized_requirements),
                "ambiguity_count": len(ambiguous),
                "normalized_ambiguity_count": normalized_ambiguities,
                "added_knowledge_requirement_count": len(uncovered_knowledge),
                "candidate_path_count": len(
                    {
                        path
                        for requirement in normalized_requirements
                        for path in requirement.candidate_paths
                    }
                ),
                "candidate_field_count": sum(
                    len(requirement.candidate_fields) for requirement in normalized_requirements
                ),
            },
        )
        return ToolExecutionResult(
            ok=True,
            content={
                "status": "locked",
                "requirement_ids": list(self.locked_requirements),
                "needs_discovery": [
                    item.id for item in normalized_requirements if item.needs_discovery
                ],
                "ambiguous_requirement_ids": ambiguous,
                "runtime_normalizations": {
                    "ambiguity_flags": normalized_ambiguities,
                    "knowledge_requirements": len(uncovered_knowledge),
                },
            },
        )

    def _bind_discovery(
        self,
        arguments: _TargetedDiscoveryInput,
        *,
        path: str,
    ) -> ToolExecutionResult | None:
        if not self.requirements_locked:
            return _error_result(
                "REQUIREMENTS_NOT_LOCKED",
                "Call lock_requirements after inspect_files and before discovery tools.",
            )
        unknown_ids = set(arguments.requirement_ids) - self.locked_requirements.keys()
        if unknown_ids:
            return _error_result(
                "UNKNOWN_REQUIREMENT",
                f"Discovery call references unlocked requirements: {sorted(unknown_ids)}",
            )
        for requirement_id in arguments.requirement_ids:
            requirement = self.locked_requirements[requirement_id]
            if path not in requirement.candidate_paths:
                return _error_result(
                    "PATH_OUTSIDE_REQUIREMENT",
                    f"{path} is not a candidate path for requirement {requirement_id}.",
                )
            if (
                self.requirement_call_counts[requirement_id]
                >= self._MAX_DISCOVERY_CALLS_PER_REQUIREMENT
            ):
                return _error_result(
                    "REQUIREMENT_DISCOVERY_BUDGET_EXHAUSTED",
                    f"Requirement {requirement_id} already used "
                    f"{self._MAX_DISCOVERY_CALLS_PER_REQUIREMENT} discovery calls.",
                )

        allowed_candidates = [
            candidate
            for requirement_id in arguments.requirement_ids
            for candidate in self.locked_requirements[requirement_id].candidate_fields
            if candidate.path == path
        ]
        allowed_by_identity = {
            self._field_identity(candidate): candidate for candidate in allowed_candidates
        }
        normalized_targets = [
            allowed_by_identity[self._field_identity(candidate)]
            for candidate in arguments.target_fields
            if candidate.path == path and self._field_identity(candidate) in allowed_by_identity
        ]
        if not normalized_targets and allowed_candidates:
            normalized_targets = list(allowed_by_identity.values())
        arguments.target_fields = list(
            {
                self._field_identity(candidate): candidate for candidate in normalized_targets
            }.values()
        )
        return None

    def _record_discovery_call(self, arguments: _TargetedDiscoveryInput) -> None:
        for requirement_id in arguments.requirement_ids:
            self.requirement_call_counts[requirement_id] += 1

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
        if error := self._bind_discovery(arguments, path=arguments.path):
            return error
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
            self._record_discovery_call(arguments)
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
                requirement_ids=arguments.requirement_ids,
                purpose=arguments.purpose,
                target_fields=arguments.target_fields,
                ok=False,
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
        self._record_discovery_call(arguments)
        evidence = self._add_evidence(
            prefix="preview",
            source_tool="preview_file",
            path=arguments.path,
            requirement_ids=arguments.requirement_ids,
            purpose=arguments.purpose,
            target_fields=arguments.target_fields,
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
        if arguments.path not in self.discovered_paths:
            return _error_result(
                "PATH_NOT_INSPECTED",
                "grep path must be one exact file listed by inspect_files.",
            )
        if error := self._bind_discovery(arguments, path=arguments.path):
            return error
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
        self._record_discovery_call(arguments)
        evidence = self._add_evidence(
            prefix="grep",
            source_tool="grep_context",
            path=arguments.path,
            requirement_ids=arguments.requirement_ids,
            purpose=arguments.purpose,
            target_fields=arguments.target_fields,
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
        if error := self._bind_discovery(arguments, path=arguments.path):
            return error
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
        self._record_discovery_call(arguments)
        evidence = self._add_evidence(
            prefix="sql",
            source_tool="execute_context_sql",
            path=arguments.path,
            requirement_ids=arguments.requirement_ids,
            purpose=arguments.purpose,
            target_fields=arguments.target_fields,
            observation=observation,
        )
        return ToolExecutionResult(ok=True, content=evidence)

    @staticmethod
    def _append_ref(entry: dict[str, Any], evidence_id: str) -> None:
        refs = entry.setdefault("evidence_refs", [])
        if evidence_id not in refs:
            refs.append(evidence_id)

    @staticmethod
    def _schema_key(path: str, table: str | None) -> str:
        return f"{path}::{table}" if table else path

    def _merge_schema(
        self,
        schema_map: dict[str, dict[str, Any]],
        *,
        path: str,
        table: str | None,
        columns: list[str],
        evidence_id: str,
        row_count: int | None = None,
        max_columns: int = 64,
    ) -> dict[str, Any]:
        entry = schema_map.setdefault(
            self._schema_key(path, table),
            {
                "path": path,
                "table": table,
                "columns": [],
                "semantics": {},
                "evidence_refs": [],
            },
        )
        for column in columns[:max_columns]:
            if column not in entry["columns"]:
                entry["columns"].append(column)
        if isinstance(row_count, int) and row_count >= 0:
            entry["row_count"] = row_count
        self._append_ref(entry, evidence_id)
        return entry

    @staticmethod
    def _summary_columns(summary: dict[str, Any]) -> list[str]:
        values = summary.get("columns") or summary.get("field_paths") or summary.get("keys") or []
        return [str(value) for value in values] if isinstance(values, list) else []

    @staticmethod
    def _bounded_values(values: list[Any], *, limit: int = 5) -> list[Any]:
        bounded: list[Any] = []
        for value in values[:limit]:
            if isinstance(value, str):
                bounded.append(f"{value[:299]}…" if len(value) > 300 else value)
            elif isinstance(value, dict):
                bounded.append(
                    {
                        str(key): (
                            f"{item[:299]}…" if isinstance(item, str) and len(item) > 300 else item
                        )
                        for key, item in list(value.items())[:12]
                    }
                )
            elif isinstance(value, (list, tuple)):
                bounded.append(list(value[:12]))
            else:
                bounded.append(value)
        return bounded

    @staticmethod
    def _warning(
        message: str,
        *,
        evidence_refs: list[str] | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        return {
            "path": path,
            "message": message,
            "evidence_refs": list(evidence_refs or []),
        }

    @staticmethod
    def _observation_summary(evidence: dict[str, Any]) -> dict[str, Any] | None:
        observation = evidence.get("observation")
        if not isinstance(observation, dict):
            return None
        tool = evidence["source_tool"]
        if tool == "preview_file":
            content = {
                "kind": observation.get("kind"),
                "summary": observation.get("summary", {}),
                "warnings": observation.get("warnings", []),
            }
        elif tool == "grep_context":
            content = {
                "pattern": observation.get("pattern"),
                "path_filter": observation.get("path_filter"),
                "match_count": observation.get("match_count"),
                "matches": observation.get("matches", [])[:5],
                "warnings": observation.get("warnings", []),
                "truncated": observation.get("truncated"),
            }
        elif tool == "execute_context_sql":
            content = {
                "columns": observation.get("columns", []),
                "rows": observation.get("rows", [])[:5],
                "row_count": observation.get("row_count"),
                "truncated": observation.get("truncated"),
            }
        else:
            return None
        return {
            "evidence_id": evidence["evidence_id"],
            "source_tool": tool,
            "path": evidence.get("path"),
            "observation": content,
        }

    def _known_fields(
        self,
        *,
        selected_evidence_ids: set[str] | None = None,
    ) -> dict[str, set[tuple[str | None, str]]]:
        known: dict[str, set[tuple[str | None, str]]] = {}
        for evidence_id, evidence in self.evidence.items():
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
            elif selected_evidence_ids is None or evidence_id in selected_evidence_ids:
                path = evidence.get("path")
                summary = observation.get("summary")
                if isinstance(path, str) and isinstance(summary, dict):
                    known.setdefault(path, set()).update(_summary_fields(summary))
                if isinstance(path, str) and evidence["source_tool"] == "execute_context_sql":
                    known.setdefault(path, set()).update(
                        (None, str(column)) for column in observation.get("columns", [])
                    )
        return known

    def _requirement_is_covered(self, requirement: LockedRequirement) -> bool:
        if not requirement.needs_discovery:
            return True
        supporting = [
            evidence
            for evidence in self.evidence.values()
            if evidence.get("source_tool") != "inspect_files"
            and evidence.get("ok") is not False
            and requirement.id in evidence.get("requirement_ids", [])
        ]
        if not supporting:
            return False
        if any(
            evidence.get("source_tool") == "preview_file"
            and evidence.get("path") in self.knowledge_paths
            for evidence in supporting
        ):
            return True
        required_fields = {
            self._field_identity(candidate) for candidate in requirement.candidate_fields
        }
        if not required_fields:
            return True
        observed_fields = {
            (
                str(candidate["path"]),
                candidate.get("table"),
                str(candidate["field"]),
            )
            for evidence in supporting
            for candidate in evidence.get("target_fields", [])
            if isinstance(candidate, dict)
            and candidate.get("path") is not None
            and candidate.get("field") is not None
        }
        return required_fields.issubset(observed_fields)

    def requirements_ready_for_report(self) -> bool:
        return self.requirements_locked and all(
            self._requirement_is_covered(requirement)
            for requirement in self.locked_requirements.values()
        )

    def _runtime_requirements(self) -> list[dict[str, Any]]:
        return [
            {
                **requirement.model_dump(mode="json"),
                "ambiguous": len(requirement.candidate_fields) > 1,
                "covered": self._requirement_is_covered(requirement),
                "discovery_calls": self.requirement_call_counts.get(requirement.id, 0),
            }
            for requirement in self.locked_requirements.values()
        ]

    def _runtime_report(self, *, selected_evidence_ids: set[str]) -> dict[str, Any]:
        report: dict[str, Any] = {
            "files": [],
            "schema_map": {},
            "task_requirements": [],
            "relevant_evidence": [],
            "requirement_resolutions": [],
            "selected_sources": [],
            "knowledge": [],
            "etl_candidates": [],
            "join_paths": [],
            "value_samples": {},
            "evidence_summaries": [],
            "warnings": [],
            "uncertainties": [],
        }
        files_by_path: dict[str, dict[str, Any]] = {}
        schema_map = report["schema_map"]

        for evidence_id, evidence in self.evidence.items():
            observation = evidence.get("observation")
            if not isinstance(observation, dict):
                continue
            tool = evidence["source_tool"]
            path = evidence.get("path")
            if tool == "inspect_files":
                for item in observation.get("files", []):
                    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                        continue
                    item_path = item["path"]
                    summary = item.get("summary")
                    summary = summary if isinstance(summary, dict) else {}
                    entry = {
                        "path": item_path,
                        "format": str(item.get("kind", "unknown")),
                        "size_bytes": item.get("size_bytes"),
                        "row_count": (
                            summary.get("row_count")
                            if isinstance(summary.get("row_count"), int)
                            else None
                        ),
                        "evidence_refs": [evidence_id],
                    }
                    report["files"].append(entry)
                    files_by_path[item_path] = entry
                    tables = summary.get("tables")
                    if isinstance(tables, list):
                        for table in tables:
                            if not isinstance(table, dict):
                                continue
                            self._merge_schema(
                                schema_map,
                                path=item_path,
                                table=str(table.get("name")),
                                columns=[
                                    str(column.get("name"))
                                    for column in table.get("columns", [])
                                    if isinstance(column, dict) and column.get("name") is not None
                                ],
                                evidence_id=evidence_id,
                                max_columns=16,
                                row_count=(
                                    table.get("row_count")
                                    if isinstance(table.get("row_count"), int)
                                    else None
                                ),
                            )
                    else:
                        columns = self._summary_columns(summary)
                        if columns:
                            self._merge_schema(
                                schema_map,
                                path=item_path,
                                table=None,
                                columns=columns,
                                evidence_id=evidence_id,
                                max_columns=16,
                                row_count=(
                                    summary.get("row_count")
                                    if isinstance(summary.get("row_count"), int)
                                    else None
                                ),
                            )
                for item in observation.get("warnings", []):
                    if isinstance(item, dict):
                        report["warnings"].append(
                            self._warning(
                                f"{item.get('code', 'INSPECT_WARNING')}: {item.get('message', '')}",
                                evidence_refs=[evidence_id],
                                path=item.get("path"),
                            )
                        )
                continue

            if evidence_id not in selected_evidence_ids:
                continue
            if isinstance(path, str) and path in files_by_path:
                self._append_ref(files_by_path[path], evidence_id)
            summary_entry = self._observation_summary(evidence)
            if summary_entry is not None:
                report["evidence_summaries"].append(summary_entry)

            if tool == "preview_file":
                summary = observation.get("summary")
                summary = summary if isinstance(summary, dict) else {}
                if isinstance(path, str):
                    tables = summary.get("tables")
                    if isinstance(tables, list):
                        for table in tables:
                            if not isinstance(table, dict):
                                continue
                            self._merge_schema(
                                schema_map,
                                path=path,
                                table=str(table.get("name")),
                                columns=[
                                    str(column.get("name"))
                                    for column in table.get("columns", [])
                                    if isinstance(column, dict) and column.get("name") is not None
                                ],
                                evidence_id=evidence_id,
                                row_count=(
                                    table.get("row_count")
                                    if isinstance(table.get("row_count"), int)
                                    else None
                                ),
                            )
                    else:
                        columns = self._summary_columns(summary)
                        if columns:
                            self._merge_schema(
                                schema_map,
                                path=path,
                                table=None,
                                columns=columns,
                                evidence_id=evidence_id,
                                row_count=(
                                    summary.get("row_count")
                                    if isinstance(summary.get("row_count"), int)
                                    else None
                                ),
                            )
                    sample_values = (
                        summary.get("sample_rows")
                        or summary.get("sample_objects")
                        or summary.get("sample")
                        or []
                    )
                    if isinstance(sample_values, list) and sample_values:
                        report["value_samples"][f"preview:{path}"] = {
                            "values": self._bounded_values(sample_values),
                            "evidence_refs": [evidence_id],
                        }
                    if path in self.knowledge_paths:
                        preview_text = summary.get("preview")
                        if isinstance(preview_text, str) and preview_text.strip():
                            report["knowledge"].append(
                                {
                                    "path": path,
                                    "kind": "source_preview",
                                    "text": preview_text[:500],
                                    "evidence_refs": [evidence_id],
                                }
                            )
                for item in observation.get("warnings", []):
                    if isinstance(item, dict):
                        report["warnings"].append(
                            self._warning(
                                f"{item.get('code', 'PREVIEW_WARNING')}: {item.get('message', '')}",
                                evidence_refs=[evidence_id],
                                path=path if isinstance(path, str) else None,
                            )
                        )

            elif tool == "grep_context":
                matches = observation.get("matches", [])
                if isinstance(matches, list) and matches:
                    report["value_samples"][f"grep:{evidence_id}"] = {
                        "values": self._bounded_values(matches),
                        "evidence_refs": [evidence_id],
                    }
                for item in observation.get("warnings", []):
                    if isinstance(item, dict):
                        report["warnings"].append(
                            self._warning(
                                f"{item.get('code', 'GREP_WARNING')}: {item.get('message', '')}",
                                evidence_refs=[evidence_id],
                                path=item.get("path"),
                            )
                        )

            elif tool == "execute_context_sql":
                columns = [str(column) for column in observation.get("columns", [])]
                if isinstance(path, str) and columns:
                    self._merge_schema(
                        schema_map,
                        path=path,
                        table=None,
                        columns=columns,
                        evidence_id=evidence_id,
                    )
                rows = observation.get("rows", [])
                if isinstance(rows, list) and rows:
                    report["value_samples"][f"sql:{evidence_id}"] = {
                        "values": self._bounded_values(rows),
                        "evidence_refs": [evidence_id],
                    }
        return report

    def _validated_relevance(
        self,
        arguments: ExplorerReportInput,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str], list[dict[str, Any]]]:
        requirements = self._runtime_requirements()
        requirement_ids = set(self.locked_requirements)
        warnings: list[dict[str, Any]] = []

        relevant_evidence: list[dict[str, Any]] = []
        selected_evidence_ids: set[str] = set()
        for selection in arguments.relevant_evidence:
            evidence = self.evidence.get(selection.evidence_id)
            supports = set(selection.supports)
            bound_requirements = (
                set(evidence.get("requirement_ids", [])) if evidence is not None else set()
            )
            if (
                evidence is None
                or evidence.get("source_tool") == "inspect_files"
                or evidence.get("ok") is False
                or not supports.issubset(requirement_ids)
                or not supports.issubset(bound_requirements)
            ):
                warnings.append(
                    self._warning(f"Ignored unsupported relevant evidence: {selection.evidence_id}")
                )
                continue
            selected_evidence_ids.add(selection.evidence_id)
            relevant_evidence.append(
                {
                    **selection.model_dump(mode="json"),
                    "source_tool": evidence["source_tool"],
                    "path": evidence.get("path"),
                    "purpose": evidence.get("purpose"),
                }
            )
        return requirements, relevant_evidence, selected_evidence_ids, warnings

    def _apply_requirement_resolutions(
        self,
        report: dict[str, Any],
        arguments: ExplorerReportInput,
        *,
        selected_evidence_ids: set[str],
    ) -> None:
        seen: set[str] = set()
        for resolution in arguments.requirement_resolutions:
            requirement = self.locked_requirements.get(resolution.requirement_id)
            if requirement is None or resolution.requirement_id in seen:
                report["warnings"].append(
                    self._warning(
                        f"Ignored unknown or duplicate requirement resolution: "
                        f"{resolution.requirement_id}"
                    )
                )
                continue
            seen.add(resolution.requirement_id)
            refs = set(resolution.evidence_refs)
            refs_supported = (
                bool(refs)
                and refs.issubset(selected_evidence_ids)
                and all(
                    resolution.requirement_id
                    in self.evidence[evidence_id].get("requirement_ids", [])
                    for evidence_id in refs
                )
            )
            candidate_identities = {
                self._field_identity(candidate) for candidate in requirement.candidate_fields
            }
            selected_identity = (
                self._field_identity(resolution.selected_field)
                if resolution.selected_field is not None
                else None
            )
            rejected_identities = {
                self._field_identity(candidate) for candidate in resolution.rejected_fields
            }
            fields_supported = (
                (selected_identity is None or selected_identity in candidate_identities)
                and rejected_identities.issubset(candidate_identities)
                and selected_identity not in rejected_identities
            )
            resolved_shape_valid = not (
                resolution.status == "resolved"
                and requirement.candidate_fields
                and selected_identity is None
            )
            if not refs_supported or not fields_supported or not resolved_shape_valid:
                report["warnings"].append(
                    self._warning(
                        f"Ignored unsupported requirement resolution: {resolution.requirement_id}"
                    )
                )
                continue
            report["requirement_resolutions"].append(resolution.model_dump(mode="json"))

    def _apply_semantic_increments(
        self,
        report: dict[str, Any],
        arguments: ExplorerReportInput,
        *,
        selected_evidence_ids: set[str],
    ) -> None:
        known_fields = self._known_fields(selected_evidence_ids=selected_evidence_ids)

        def refs_are_valid(refs: list[str]) -> bool:
            return bool(refs) and set(refs).issubset(selected_evidence_ids)

        for path in arguments.selected_sources:
            if path in self.discovered_paths and path not in report["selected_sources"]:
                report["selected_sources"].append(path)
            elif path not in self.discovered_paths:
                report["warnings"].append(self._warning(f"Ignored unknown source: {path}"))

        for semantic in arguments.field_semantics:
            available = known_fields.get(semantic.path, set())
            field_known = (semantic.table, semantic.field) in available or any(
                field == semantic.field for _, field in available
            )
            if (
                semantic.path not in self.discovered_paths
                or not field_known
                or not refs_are_valid(semantic.evidence_refs)
            ):
                report["warnings"].append(
                    self._warning(
                        f"Ignored unsupported field semantic: {semantic.path}.{semantic.field}"
                    )
                )
                continue
            entry = self._merge_schema(
                report["schema_map"],
                path=semantic.path,
                table=semantic.table,
                columns=[semantic.field],
                evidence_id=semantic.evidence_refs[0],
            )
            entry["semantics"][semantic.field] = semantic.meaning
            for evidence_id in semantic.evidence_refs[1:]:
                self._append_ref(entry, evidence_id)

        for item in arguments.knowledge:
            preview_supported = any(
                ref in self.evidence
                and self.evidence[ref]["source_tool"] == "preview_file"
                and self.evidence[ref].get("path") == item.path
                for ref in item.evidence_refs
            )
            if (
                item.path in self.discovered_paths
                and refs_are_valid(item.evidence_refs)
                and preview_supported
            ):
                report["knowledge"].append(item.model_dump(mode="json"))
            else:
                report["warnings"].append(
                    self._warning(f"Ignored unsupported knowledge claim for: {item.path}")
                )

        for item in arguments.join_paths:
            references = (item.left, item.right)
            fields_known = all(
                reference.path in self.discovered_paths
                and (
                    (reference.table, reference.field) in known_fields.get(reference.path, set())
                    or any(
                        field == reference.field
                        for _, field in known_fields.get(reference.path, set())
                    )
                )
                for reference in references
            )
            if fields_known and refs_are_valid(item.evidence_refs):
                report["join_paths"].append(item.model_dump(mode="json"))
            else:
                report["warnings"].append(self._warning("Ignored unsupported join candidate."))

        for item in arguments.etl_candidates:
            if item.path in self.discovered_paths and refs_are_valid(item.evidence_refs):
                report["etl_candidates"].append(item.model_dump(mode="json"))
            else:
                report["warnings"].append(
                    self._warning(f"Ignored unsupported ETL candidate: {item.path}")
                )

        for item in arguments.warnings:
            if (item.path is None or item.path in self.discovered_paths) and refs_are_valid(
                item.evidence_refs
            ):
                report["warnings"].append(item.model_dump(mode="json"))
        for item in arguments.uncertainties:
            if (item.path is None or item.path in self.discovered_paths) and refs_are_valid(
                item.evidence_refs
            ):
                report["uncertainties"].append(item.model_dump(mode="json"))

    def _bound_assembled_report(self, report: dict[str, Any]) -> None:
        def rendered_length() -> int:
            return len(json.dumps(report, ensure_ascii=False, separators=(",", ":"), default=str))

        truncated = False
        # Drop duplicated or advisory material before the observations that only
        # preview/grep/SQL could provide. The main Agent can rediscover an omitted
        # inspect row more cheaply than it can reproduce a targeted observation.
        for key in ("join_paths", "etl_candidates", "uncertainties"):
            while rendered_length() > self.config.max_inventory_chars and report[key]:
                report[key].pop()
                truncated = True
        for key in ("value_samples", "schema_map"):
            while rendered_length() > self.config.max_inventory_chars and report[key]:
                report[key].pop(next(reversed(report[key])))
                truncated = True
        for key in ("files", "evidence_summaries"):
            while rendered_length() > self.config.max_inventory_chars and report[key]:
                report[key].pop()
                truncated = True
        if truncated:
            truncation_warning = self._warning(
                "Assembled data map was truncated to its character budget."
            )
            report["warnings"].append(truncation_warning)
            while (
                rendered_length() > self.config.max_inventory_chars and len(report["warnings"]) > 1
            ):
                report["warnings"].pop(0)
            if rendered_length() > self.config.max_inventory_chars:
                report["warnings"].remove(truncation_warning)

    def _assemble_report(
        self,
        arguments: ExplorerReportInput | None,
        *,
        fallback_reason: str | None = None,
    ) -> dict[str, Any]:
        relevance_warnings: list[dict[str, Any]] = []
        if arguments is None:
            (
                requirements,
                relevant_evidence,
                selected_evidence_ids,
                relevance_warnings,
            ) = self._fallback_relevance()
        else:
            (
                requirements,
                relevant_evidence,
                selected_evidence_ids,
                relevance_warnings,
            ) = self._validated_relevance(arguments)
        report = self._runtime_report(selected_evidence_ids=selected_evidence_ids)
        report["task_requirements"] = requirements
        report["relevant_evidence"] = relevant_evidence
        report["warnings"].extend(relevance_warnings)

        selected_paths = {
            str(self.evidence[evidence_id]["path"])
            for evidence_id in selected_evidence_ids
            if isinstance(self.evidence[evidence_id].get("path"), str)
            and self.evidence[evidence_id]["path"] in self.discovered_paths
        }
        for evidence_id in selected_evidence_ids:
            evidence = self.evidence[evidence_id]
            if evidence.get("source_tool") != "grep_context":
                continue
            observation = evidence.get("observation")
            if not isinstance(observation, dict):
                continue
            for match in observation.get("matches", []):
                if (
                    isinstance(match, dict)
                    and isinstance(match.get("path"), str)
                    and match["path"] in self.discovered_paths
                ):
                    selected_paths.add(match["path"])
        if arguments is not None:
            self._apply_requirement_resolutions(
                report,
                arguments,
                selected_evidence_ids=selected_evidence_ids,
            )
            self._apply_semantic_increments(
                report,
                arguments,
                selected_evidence_ids=selected_evidence_ids,
            )
            selected_paths.update(
                path for path in arguments.selected_sources if path in self.discovered_paths
            )
        elif self.locked_requirements:
            report["requirement_resolutions"] = [
                {
                    "requirement_id": requirement.id,
                    "status": "unresolved",
                    "selected_field": None,
                    "rejected_fields": [],
                    "evidence_refs": [
                        evidence_id
                        for evidence_id in selected_evidence_ids
                        if requirement.id in self.evidence[evidence_id].get("requirement_ids", [])
                    ][:8],
                    "note": "Fallback preserved evidence without inferring a semantic resolution.",
                }
                for requirement in self.locked_requirements.values()
            ]
        report["selected_sources"] = sorted(selected_paths)
        if fallback_reason is not None:
            first_ref = next(iter(self.evidence), None)
            report["warnings"].append(
                self._warning(
                    fallback_reason,
                    evidence_refs=[first_ref] if first_ref else [],
                )
            )
        self._bound_assembled_report(report)
        return report

    def _fallback_relevance(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str], list[dict[str, Any]]]:
        candidates = [
            evidence
            for evidence in self.evidence.values()
            if evidence.get("source_tool") != "inspect_files"
            and evidence.get("ok") is not False
            and evidence.get("requirement_ids")
        ]
        prioritized: list[dict[str, Any]] = []
        for evidence in candidates:
            if evidence.get("path") in self.knowledge_paths:
                prioritized.append(evidence)

        seen_pairs: set[tuple[str, str]] = set()
        for evidence in reversed(candidates):
            tool = str(evidence.get("source_tool"))
            adds_pair = False
            for requirement_id in evidence.get("requirement_ids", []):
                pair = (str(requirement_id), tool)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    adds_pair = True
            if adds_pair and evidence not in prioritized:
                prioritized.append(evidence)
        for evidence in reversed(candidates):
            if evidence not in prioritized:
                prioritized.append(evidence)

        selected = prioritized[:8]
        selected_ids = {str(evidence["evidence_id"]) for evidence in selected}
        requirements = self._runtime_requirements()
        relevant_evidence = [
            {
                "evidence_id": evidence["evidence_id"],
                "supports": [
                    str(requirement_id) for requirement_id in evidence.get("requirement_ids", [])
                ],
                "source_tool": evidence["source_tool"],
                "path": evidence.get("path"),
                "purpose": evidence.get("purpose"),
            }
            for evidence in selected
        ]
        warnings = []
        if len(candidates) > len(selected):
            warnings.append(
                self._warning(
                    "Fallback relevance projection omitted lower-priority deep observations."
                )
            )
        return requirements, relevant_evidence, selected_ids, warnings

    def report(self, _: PublicTask, arguments: ExplorerReportInput) -> ToolExecutionResult:
        if error := self._require_inspection():
            return error
        if not self.requirements_locked:
            return _error_result(
                "REQUIREMENTS_NOT_LOCKED",
                "Call lock_requirements successfully before report.",
            )
        missing_knowledge = self.knowledge_paths - self.knowledge_attempted
        if missing_knowledge:
            return _error_result(
                "KNOWLEDGE_NOT_REVIEWED",
                f"preview_file must be attempted for: {sorted(missing_knowledge)}",
            )
        rendered = json.dumps(
            arguments.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(rendered) > self.config.max_report_chars:
            emit_event(
                self.event_sink,
                "explorer_report_budget_exceeded",
                {
                    "semantic_report_chars": len(rendered),
                    "preferred_report_chars": self.config.max_report_chars,
                },
            )
        return ToolExecutionResult(
            ok=True,
            content={
                "status": "reported",
                "report": self._assemble_report(arguments),
                "evidence": list(self.evidence.values()),
            },
            is_terminal=True,
        )

    def fallback(self, reason: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return (
            self._assemble_report(None, fallback_reason=reason),
            list(self.evidence.values()),
        )

    def registry(self, *, final_step: bool = False) -> ToolRegistry:
        specs = {
            "execute_context_sql": ToolSpec(
                name="execute_context_sql",
                description=(
                    "Run bounded read-only SELECT, WITH, PRAGMA, or EXPLAIN SQL against an "
                    "inspected SQLite file. State stable requirement_ids and why this query "
                    "is necessary. Never compute the final answer."
                ),
                input_model=ExplorerSqlInput,
                handler=self.sql,
            ),
            "grep_context": ToolSpec(
                name="grep_context",
                description=(
                    "Search a bounded case-insensitive regex across inspected Phase 1 text "
                    "and SQLite sources. State stable requirement_ids and why this search is "
                    "necessary. Use only to locate evidence, not aggregate data."
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
            "lock_requirements": ToolSpec(
                name="lock_requirements",
                description=(
                    "Use once and alone immediately after inspect_files. Lock the question's "
                    "entities, measures, filters, time scope, output, knowledge, and joins "
                    "to real inspected candidate paths and fields. Requirements are immutable."
                ),
                input_model=LockRequirementsInput,
                handler=self.lock_requirements,
            ),
            "preview_file": ToolSpec(
                name="preview_file",
                description=(
                    "Preview one path returned by inspect_files and receive immutable "
                    "evidence. State stable requirement_ids and why the preview is necessary. "
                    "Every knowledge.md must be previewed before report."
                ),
                input_model=PreviewFileInput,
                handler=self.preview,
            ),
            "report": ToolSpec(
                name="report",
                description=(
                    "Select only question-relevant deep observations and submit supported "
                    "requirement resolutions and semantic increments. The runtime owns the "
                    "locked requirements, compact inventory, schemas, and evidence provenance. "
                    "Must be the only call."
                ),
                input_model=ExplorerReportInput,
                handler=self.report,
                is_terminal=True,
            ),
        }
        if not self.inspect_completed:
            return ToolRegistry(specs={"inspect_files": specs["inspect_files"]})
        specs.pop("inspect_files")
        if not self.requirements_locked:
            return ToolRegistry(specs={"lock_requirements": specs["lock_requirements"]})
        specs.pop("lock_requirements")
        if final_step or self.requirements_ready_for_report():
            return ToolRegistry(specs={"report": specs["report"]})
        if not any(
            path.casefold().endswith((".db", ".sqlite", ".sqlite3"))
            for path in self.discovered_paths
        ):
            specs.pop("execute_context_sql")
        if self.preview_calls >= self.config.max_preview_calls:
            specs.pop("preview_file")
        if self.knowledge_paths - self.knowledge_attempted:
            specs.pop("report")
        return ToolRegistry(specs=specs)


class ExplorerRunner:
    _MAX_FINAL_REPORT_RETRIES = 2
    _REPORT_DUE_STEP = 6

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
        report_chars = len(
            json.dumps(report, ensure_ascii=False, separators=(",", ":"), default=str)
        )
        relevance_metrics = {
            "task_requirement_count": len(report.get("task_requirements", [])),
            "ambiguous_requirement_count": sum(
                bool(item.get("ambiguous")) for item in report.get("task_requirements", [])
            ),
            "covered_requirement_count": sum(
                bool(item.get("covered")) for item in report.get("task_requirements", [])
            ),
            "relevant_evidence_count": len(report.get("relevant_evidence", [])),
            "requirement_resolution_count": len(report.get("requirement_resolutions", [])),
            "selected_source_count": len(report.get("selected_sources", [])),
            "report_chars": report_chars,
        }
        emit_event(
            self.event_sink,
            "explorer_fallback_used",
            {
                "steps_used": steps_used,
                "reason_code": reason_code,
                **relevance_metrics,
            },
        )
        emit_event(
            self.event_sink,
            "explorer_completed",
            {
                "steps_used": steps_used,
                "fallback_used": True,
                "success": bool(evidence),
                **relevance_metrics,
            },
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

    def _emit_budget_warning(
        self,
        *,
        messages: list[ModelMessage],
        step_index: int,
        emitted_levels: set[str],
    ) -> None:
        expected_steps = min(self.config.max_steps, self._REPORT_DUE_STEP)
        ratio = step_index / expected_steps
        level: str | None = None
        message: str | None = None
        if ratio >= 0.9:
            level = "critical"
            message = (
                "CRITICAL EXPLORER BUDGET: at least 90% of normal turns are allocated. "
                "Call report now with only the semantic increments already supported by "
                "evidence. The runtime will assemble files, schemas, samples, warnings, "
                "and evidence provenance."
            )
        elif ratio >= 0.7:
            level = "warning"
            message = (
                "EXPLORER BUDGET WARNING: at least 70% of normal turns are allocated. "
                "If the required sources and fields are located, call report now. Perform "
                "at most one more targeted discovery turn only when essential evidence is "
                "still missing."
            )
        if level is None or level in emitted_levels:
            return
        emitted_levels.add(level)
        messages.append(ModelMessage(role="user", content=message))
        emit_event(
            self.event_sink,
            "explorer_budget_warning",
            {
                "explorer_step_index": step_index,
                "level": level,
                "allocated_ratio": ratio,
            },
        )

    def _append_protocol_error(
        self,
        *,
        messages: list[ModelMessage],
        response: ModelResponse,
        code: str,
        message: str,
    ) -> None:
        calls = response.tool_calls
        replayable = bool(calls) and all(call.id and call.name for call in calls)
        if replayable:
            messages.append(_assistant_message(response))
            for call in calls:
                messages.append(_tool_message(call, self._protocol_observation(code, message)))
            return
        messages.append(
            ModelMessage(
                role="user",
                content=f"Explorer protocol error ({code}): {message}",
            )
        )

    def _request_final_report_retry(
        self,
        *,
        messages: list[ModelMessage],
        step_index: int,
        retry_index: int,
        error_code: str,
    ) -> None:
        messages.append(
            ModelMessage(
                role="user",
                content=(
                    f"FINAL REPORT RETRY {retry_index}/{self._MAX_FINAL_REPORT_RETRIES}: "
                    f"the prior final attempt failed with {error_code}. Call report as the "
                    "only tool now. Select only evidence whose recorded requirement_ids "
                    "support the immutable locked plan. Empty semantic lists are valid "
                    "because the runtime preserves requirements and the compact inventory."
                ),
            )
        )
        emit_event(
            self.event_sink,
            "explorer_final_report_retry",
            {
                "explorer_step_index": step_index,
                "retry_index": retry_index,
                "error_code": error_code,
            },
        )

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
        emitted_budget_levels: set[str] = set()
        for step_index in range(1, self.config.max_steps + 1):
            if monotonic() - started_at >= self.config.max_duration_seconds:
                return self._fallback_result(
                    tools=tools,
                    steps_used=step_index - 1,
                    reason="Explorer exceeded its soft time budget.",
                    reason_code="SOFT_TIMEOUT",
                )
            self._emit_budget_warning(
                messages=messages,
                step_index=step_index,
                emitted_levels=emitted_budget_levels,
            )
            hard_final_step = step_index == self.config.max_steps
            final_retry_index = 0
            while True:
                if monotonic() - started_at >= self.config.max_duration_seconds:
                    return self._fallback_result(
                        tools=tools,
                        steps_used=step_index - (0 if final_retry_index else 1),
                        reason="Explorer exceeded its soft time budget.",
                        reason_code="SOFT_TIMEOUT",
                    )
                report_due = hard_final_step or (
                    tools.requirements_locked and step_index >= self._REPORT_DUE_STEP
                )
                registry = tools.registry(final_step=report_due)
                try:
                    response = self.model.complete(
                        messages,
                        tools=registry,
                        request_context={
                            "task_id": task.task_id,
                            "agent_scope": "explorer",
                            "explorer_step_index": step_index,
                            "explorer_final_retry_index": final_retry_index,
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
                    protocol_error = (
                        "INVALID_TOOL_CALL",
                        "Explorer tool calls require id and name.",
                    )
                elif not tools.inspect_completed and (
                    len(calls) != 1 or calls[0].name != "inspect_files"
                ):
                    protocol_error = (
                        "INSPECT_REQUIRED",
                        "The first successful Explorer turn must call inspect_files alone.",
                    )
                elif (
                    tools.inspect_completed
                    and not tools.requirements_locked
                    and (len(calls) != 1 or calls[0].name != "lock_requirements")
                ):
                    protocol_error = (
                        "REQUIREMENTS_LOCK_REQUIRED",
                        "The second successful Explorer turn must call lock_requirements alone.",
                    )
                elif any(call.name == "lock_requirements" for call in calls) and (
                    len(calls) != 1 or calls[0].name != "lock_requirements"
                ):
                    protocol_error = (
                        "LOCK_REQUIREMENTS_MUST_BE_EXCLUSIVE",
                        "lock_requirements must be the only tool call in its turn.",
                    )
                elif any(call.name == "report" for call in calls) and (
                    len(calls) != 1 or calls[0].name != "report"
                ):
                    protocol_error = (
                        "REPORT_MUST_BE_EXCLUSIVE",
                        "report must be the only tool call in its turn.",
                    )
                elif (
                    report_due
                    and tools.requirements_locked
                    and (len(calls) != 1 or calls[0].name != "report")
                ):
                    protocol_error = (
                        "FINAL_REPORT_REQUIRED",
                        "The final Explorer turn accepts only one report call.",
                    )

                if protocol_error is not None:
                    code, message = protocol_error
                    self._append_protocol_error(
                        messages=messages,
                        response=response,
                        code=code,
                        message=message,
                    )
                    emit_event(
                        self.event_sink,
                        "explorer_step_completed",
                        {
                            "explorer_step_index": step_index,
                            "final_retry_index": final_retry_index,
                            "ok": False,
                            "error_code": code,
                            "tool_call_count": len(calls),
                        },
                    )
                    if report_due and final_retry_index < self._MAX_FINAL_REPORT_RETRIES:
                        final_retry_index += 1
                        self._request_final_report_retry(
                            messages=messages,
                            step_index=step_index,
                            retry_index=final_retry_index,
                            error_code=code,
                        )
                        continue
                    if report_due:
                        return self._fallback_result(
                            tools=tools,
                            steps_used=step_index,
                            reason="Explorer final report retries were exhausted.",
                            reason_code="FINAL_REPORT_RETRIES_EXHAUSTED",
                        )
                    break

                messages.append(_assistant_message(response))
                terminal_result: ToolExecutionResult | None = None
                final_error_code: str | None = None
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
                            "final_retry_index": final_retry_index,
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
                    elif report_due:
                        final_error_code = result.error_code or (
                            "FINAL_REPORT_REQUIRED"
                            if call.name != "report"
                            else "FINAL_REPORT_REJECTED"
                        )

                if terminal_result is not None:
                    report = terminal_result.content["report"]
                    emit_event(
                        self.event_sink,
                        "explorer_completed",
                        {
                            "steps_used": step_index,
                            "fallback_used": False,
                            "final_report_retries": final_retry_index,
                            "task_requirement_count": len(report.get("task_requirements", [])),
                            "ambiguous_requirement_count": sum(
                                bool(item.get("ambiguous"))
                                for item in report.get("task_requirements", [])
                            ),
                            "covered_requirement_count": sum(
                                bool(item.get("covered"))
                                for item in report.get("task_requirements", [])
                            ),
                            "relevant_evidence_count": len(report.get("relevant_evidence", [])),
                            "requirement_resolution_count": len(
                                report.get("requirement_resolutions", [])
                            ),
                            "selected_source_count": len(report.get("selected_sources", [])),
                            "report_chars": len(
                                json.dumps(
                                    report,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    default=str,
                                )
                            ),
                        },
                    )
                    return ExplorerResult(
                        success=True,
                        report=report,
                        evidence=terminal_result.content["evidence"],
                        steps_used=step_index,
                    )
                if report_due:
                    if final_retry_index < self._MAX_FINAL_REPORT_RETRIES:
                        final_retry_index += 1
                        self._request_final_report_retry(
                            messages=messages,
                            step_index=step_index,
                            retry_index=final_retry_index,
                            error_code=final_error_code or "FINAL_REPORT_REJECTED",
                        )
                        continue
                    return self._fallback_result(
                        tools=tools,
                        steps_used=step_index,
                        reason="Explorer final report retries were exhausted.",
                        reason_code="FINAL_REPORT_RETRIES_EXHAUSTED",
                    )
                break

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
            "inspects context files, locks question requirements to real candidate paths and "
            "fields, explicitly reads knowledge.md, performs bounded targeted discovery, and "
            "returns an evidence-backed data map."
        ),
        input_model=ExploreInput,
        handler=ExplorerToolHandler(
            model=model,
            config=config,
            event_sink=event_sink,
        ),
    )
