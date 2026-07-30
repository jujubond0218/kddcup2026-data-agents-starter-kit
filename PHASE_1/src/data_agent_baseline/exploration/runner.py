from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)

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

SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
KNOWLEDGE_NAME = "knowledge.md"
MAX_REQUIREMENTS = 12
MAX_OUTPUT_COLUMNS = 12
MAX_HELPER_FIELDS = 24
MAX_RECOMMENDED_SOURCES = 16
MAX_KNOWLEDGE_RULES = 24
MAX_UNCERTAINTIES = 12
MAX_JOIN_PATHS = 12
MAX_ETL_CANDIDATES = 8
MAX_VALUE_SAMPLES = 24
MAX_WARNINGS = 20
MAX_MODEL_REQUESTS = 2

RequirementKind = Literal[
    "entity",
    "measure",
    "filter",
    "time_scope",
    "output",
    "knowledge",
    "join",
    "other",
]
RequirementStatus = Literal["resolved", "unresolved"]
SourceStatus = Literal["confirmed", "candidate"]
OutputKind = Literal["direct", "derived", "semantic"]
HelperRole = Literal["filter", "join", "sort", "group"]
KnowledgeKind = Literal[
    "field_mapping",
    "formula",
    "unit",
    "value_mapping",
    "disambiguation",
    "output_shape",
    "example",
    "other",
]


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _GuideItem(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _EmptyInput(_StrictInput):
    @model_validator(mode="before")
    @classmethod
    def _discard_placeholder_properties(cls, value: Any) -> Any:
        # Some OpenAI-compatible models invent placeholder fields for an empty
        # object schema. They cannot affect an argument-free tool.
        return {} if isinstance(value, dict) else value


class ExploreInput(_EmptyInput):
    pass


class PreviewFileInput(_StrictInput):
    path: str = Field(min_length=1, max_length=500)


class GrepContextInput(_StrictInput):
    pattern: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=500)


class ExplorerSqlInput(_StrictInput):
    path: str = Field(min_length=1, max_length=500)
    sql: str = Field(min_length=1, max_length=4_000)
    limit: int = Field(default=200, ge=1, le=200)


class TaskRequirement(_GuideItem):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    kind: RequirementKind
    description: str = Field(min_length=1, max_length=300)
    status: RequirementStatus

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["id"] = normalized.get("id") or normalized.get("requirement_id")
        normalized["kind"] = normalized.get("kind") or normalized.get("type") or "other"
        normalized["description"] = (
            normalized.get("description")
            or normalized.get("requirement")
            or normalized.get("meaning")
        )
        raw_status = str(normalized.get("status", "unresolved")).casefold()
        normalized["status"] = (
            "resolved" if raw_status in {"resolved", "confirmed", "complete"} else "unresolved"
        )
        return normalized


class SourceField(_GuideItem):
    path: str = Field(min_length=1, max_length=500)
    table: str | None = Field(default=None, max_length=200)
    field: str | None = Field(default=None, max_length=200)


class OutputColumn(_GuideItem):
    name: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "One final answer column explicitly requested by the question. A useful identifier, "
            "ranking value, join key, or verification field is not an answer column."
        ),
    )
    kind: OutputKind = Field(
        description=(
            "direct for a real structured field, derived for a requested calculation over real "
            "inputs, or semantic for a value extracted from narrative evidence."
        )
    )
    source_fields: list[SourceField] = Field(
        default_factory=list,
        max_length=8,
        description="Real inputs that ground this one answer column.",
    )
    operation: str | None = Field(
        default=None,
        max_length=300,
        description=(
            "Required only for a requested derived calculation. Do not concatenate atomic name "
            "fields unless an anchored output-shape rule explicitly requires one string."
        ),
    )
    reason: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "Explain why the question requests this column. 'Useful context', identification, "
            "ranking, joining, or verification alone is not sufficient."
        ),
    )
    requirement_ids: list[str] = Field(default_factory=list, max_length=12)
    evidence_refs: list[str] = Field(default_factory=list, max_length=8)
    status: SourceStatus

    @field_validator("requirement_ids", "evidence_refs")
    @classmethod
    def _unique_refs(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def _validate_kind_contract(self) -> OutputColumn:
        if self.kind == "direct" and len(self.source_fields) != 1:
            raise ValueError("direct output columns require exactly one source field")
        if self.kind == "derived" and not self.source_fields:
            raise ValueError("derived output columns require at least one source field")
        if self.kind == "derived" and not (self.operation or "").strip():
            raise ValueError("derived output columns require an operation")
        if self.kind != "derived" and self.operation is not None:
            raise ValueError("only derived output columns may declare an operation")
        if self.kind == "semantic" and not self.source_fields and not self.evidence_refs:
            raise ValueError("semantic output columns require a source or evidence reference")
        return self


class HelperField(_GuideItem):
    source: SourceField
    role: HelperRole
    reason: str = Field(min_length=1, max_length=500)
    requirement_ids: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("requirement_ids")
    @classmethod
    def _unique_requirement_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


class AnswerProjection(_GuideItem):
    columns: list[OutputColumn] = Field(
        default_factory=list,
        max_length=MAX_OUTPUT_COLUMNS,
        description=(
            "The smallest final table requested by the question. Do not return a whole record "
            "when one content field answers the question."
        ),
    )
    helper_fields: list[HelperField] = Field(default_factory=list, max_length=MAX_HELPER_FIELDS)


class RecommendedSource(_GuideItem):
    path: str = Field(min_length=1, max_length=500)
    table: str | None = Field(default=None, max_length=200)
    fields: list[str] = Field(default_factory=list, max_length=32)
    purpose: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)
    requirement_ids: list[str] = Field(default_factory=list, max_length=12)
    status: SourceStatus

    @field_validator("fields", "requirement_ids")
    @classmethod
    def _unique_strings(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["path"] = (
            normalized.get("path") or normalized.get("source_path") or normalized.get("file")
        )
        raw_fields = normalized.get("fields") or normalized.get("candidate_fields") or []
        normalized["fields"] = [
            str(item.get("field") if isinstance(item, dict) else item)
            for item in raw_fields
            if (isinstance(item, dict) and item.get("field") is not None) or isinstance(item, str)
        ]
        normalized["purpose"] = (
            normalized.get("purpose")
            or normalized.get("role")
            or "Candidate source for the referenced task requirements."
        )
        normalized["reason"] = (
            normalized.get("reason") or normalized.get("rationale") or normalized["purpose"]
        )
        normalized["requirement_ids"] = (
            normalized.get("requirement_ids") or normalized.get("supports") or []
        )
        raw_status = str(normalized.get("status", "candidate")).casefold()
        normalized["status"] = "confirmed" if raw_status == "confirmed" else "candidate"
        return normalized


class KnowledgeRule(_GuideItem):
    source_path: str = Field(min_length=1, max_length=500)
    kind: KnowledgeKind
    rule: str = Field(min_length=1, max_length=1_000)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)
    requirement_ids: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("evidence_refs", "requirement_ids")
    @classmethod
    def _unique_refs(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["source_path"] = (
            normalized.get("source_path") or normalized.get("path") or normalized.get("source")
        )
        raw_kind = str(normalized.get("kind") or normalized.get("type") or "other").casefold()
        normalized["kind"] = (
            raw_kind
            if raw_kind
            in {
                "field_mapping",
                "formula",
                "unit",
                "value_mapping",
                "disambiguation",
                "output_shape",
                "example",
                "other",
            }
            else "other"
        )
        normalized["rule"] = (
            normalized.get("rule") or normalized.get("text") or normalized.get("meaning")
        )
        normalized["evidence_refs"] = (
            normalized.get("evidence_refs")
            or normalized.get("evidence_ids")
            or normalized.get("source_evidence")
            or []
        )
        normalized["requirement_ids"] = (
            normalized.get("requirement_ids") or normalized.get("supports") or []
        )
        return normalized


class Uncertainty(_GuideItem):
    requirement_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    issue: str = Field(min_length=1, max_length=500)
    candidates: list[str] = Field(default_factory=list, max_length=12)
    evidence_refs: list[str] = Field(default_factory=list, max_length=8)
    verification_hint: str = Field(min_length=1, max_length=500)

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["requirement_id"] = normalized.get("requirement_id") or normalized.get("id")
        normalized["issue"] = (
            normalized.get("issue") or normalized.get("description") or normalized.get("note")
        )
        normalized["candidates"] = normalized.get("candidates") or []
        normalized["evidence_refs"] = (
            normalized.get("evidence_refs") or normalized.get("evidence_ids") or []
        )
        normalized["verification_hint"] = (
            normalized.get("verification_hint")
            or normalized.get("how_to_verify")
            or normalized.get("check")
            or "Verify this ambiguity with the normal task tools."
        )
        return normalized


class JoinEndpoint(_GuideItem):
    path: str = Field(min_length=1, max_length=500)
    field: str = Field(min_length=1, max_length=200)
    table: str | None = Field(default=None, max_length=200)


class JoinCandidate(_GuideItem):
    left: JoinEndpoint
    right: JoinEndpoint
    requirement_ids: list[str] = Field(default_factory=list, max_length=12)
    status: Literal["candidate"] = "candidate"


class EtlCandidate(_GuideItem):
    path: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)


class KnowledgeGuide(_GuideItem):
    applicable_rules: list[KnowledgeRule] = Field(
        default_factory=list, max_length=MAX_KNOWLEDGE_RULES
    )


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value}
    return {}


class ExplorerReportInput(BaseModel):
    """Tolerant model input; runtime projects each semantic item independently."""

    model_config = ConfigDict(extra="ignore")

    task_interpretation: str = Field(default="", max_length=1_000)
    task_requirements: list[TaskRequirement] = Field(
        default_factory=list,
        max_length=MAX_REQUIREMENTS,
    )
    answer_projection: AnswerProjection = Field(default_factory=AnswerProjection)
    recommended_sources: list[RecommendedSource] = Field(
        default_factory=list,
        max_length=MAX_RECOMMENDED_SOURCES,
    )
    files: list[Any] = Field(default_factory=list)
    schema_map: dict[str, Any] = Field(default_factory=dict)
    knowledge: KnowledgeGuide = Field(default_factory=KnowledgeGuide)
    etl_candidates: list[EtlCandidate] = Field(
        default_factory=list,
        max_length=MAX_ETL_CANDIDATES,
    )
    join_paths: list[JoinCandidate] = Field(default_factory=list, max_length=MAX_JOIN_PATHS)
    value_samples: dict[str, Any] = Field(default_factory=dict)
    uncertainties: list[Uncertainty] = Field(
        default_factory=list,
        max_length=MAX_UNCERTAINTIES,
    )
    warnings: list[Any] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _keep_valid_semantic_items(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return {}
        normalized = dict(value)
        warnings = normalized.get("warnings", [])
        if not isinstance(warnings, list):
            warnings = [warnings]
        if not isinstance(normalized.get("answer_projection"), dict):
            normalized["answer_projection"] = {}
        item_models: dict[str, type[BaseModel]] = {
            "task_requirements": TaskRequirement,
            "answer_projection.columns": OutputColumn,
            "answer_projection.helper_fields": HelperField,
            "recommended_sources": RecommendedSource,
            "etl_candidates": EtlCandidate,
            "join_paths": JoinCandidate,
            "uncertainties": Uncertainty,
        }
        item_limits = {
            "task_requirements": MAX_REQUIREMENTS,
            "answer_projection.columns": MAX_OUTPUT_COLUMNS,
            "answer_projection.helper_fields": MAX_HELPER_FIELDS,
            "recommended_sources": MAX_RECOMMENDED_SOURCES,
            "etl_candidates": MAX_ETL_CANDIDATES,
            "join_paths": MAX_JOIN_PATHS,
            "uncertainties": MAX_UNCERTAINTIES,
        }
        warning_codes = {
            "task_requirements": "INVALID_TASK_REQUIREMENT",
            "answer_projection.columns": "INVALID_OUTPUT_COLUMN",
            "answer_projection.helper_fields": "INVALID_HELPER_FIELD",
            "recommended_sources": "INVALID_RECOMMENDED_SOURCE",
            "etl_candidates": "INVALID_ETL_CANDIDATE",
            "join_paths": "INVALID_JOIN_CANDIDATE",
            "uncertainties": "INVALID_UNCERTAINTY",
        }
        for field_name, item_model in item_models.items():
            if field_name.startswith("answer_projection."):
                projection = normalized["answer_projection"]
                raw_items = projection.get(field_name.rsplit(".", 1)[-1], [])
            else:
                raw_items = normalized.get(field_name, [])
            if not isinstance(raw_items, list):
                raw_items = [raw_items]
            valid_items = []
            for raw_item in raw_items[: item_limits[field_name]]:
                try:
                    valid_items.append(item_model.model_validate(raw_item).model_dump(mode="json"))
                except ValidationError:
                    warnings.append({"code": warning_codes[field_name]})
            if field_name.startswith("answer_projection."):
                projection = normalized["answer_projection"]
                projection[field_name.rsplit(".", 1)[-1]] = valid_items
            else:
                normalized[field_name] = valid_items

        raw_knowledge = normalized.get("knowledge", {})
        raw_rules = (
            raw_knowledge.get("applicable_rules", []) if isinstance(raw_knowledge, dict) else []
        )
        if not isinstance(raw_rules, list):
            raw_rules = [raw_rules]
        valid_rules = []
        for raw_rule in raw_rules[:MAX_KNOWLEDGE_RULES]:
            try:
                valid_rules.append(KnowledgeRule.model_validate(raw_rule).model_dump(mode="json"))
            except ValidationError:
                warnings.append({"code": "INVALID_KNOWLEDGE_RULE"})
        normalized["knowledge"] = {"applicable_rules": valid_rules}
        interpretation = normalized.get("task_interpretation", "")
        normalized["task_interpretation"] = (
            interpretation if isinstance(interpretation, str) else str(interpretation)
        )
        normalized["warnings"] = warnings
        return normalized

    @field_validator("schema_map", "value_samples", mode="before")
    @classmethod
    def _normalize_mapping_fields(cls, value: Any) -> dict[str, Any]:
        return _coerce_mapping(value)

    @field_validator(
        "warnings",
        mode="before",
    )
    @classmethod
    def _normalize_list_fields(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


@dataclass(frozen=True, slots=True)
class ExplorerConfig:
    enabled: bool = True
    max_steps: int = 2
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


@dataclass(frozen=True, slots=True)
class _FollowupOutcome:
    tool: str
    ok: bool
    conclusive: bool
    error_code: str | None
    path: str | None
    evidence_id: str | None


EXPLORER_SYSTEM_PROMPT = """
You are a Phase 1 data-guide specialist. The runtime has already performed a
bounded scan of every supported context file and extracted source-anchored
knowledge.md evidence. Your job is to turn that context bundle into a compact
reference guide for the main Agent. You never calculate the final answer.

On the first request, either:
- call report immediately; or
- call exactly one targeted preview_file, grep_context, or execute_context_sql
  when a task-critical source, field, value code, or ambiguity cannot be resolved
  from the supplied bundle.

After one targeted call, the next request exposes only report. There is no open
ended exploration loop. If the targeted call fails or returns no material
evidence, explicitly keep the affected source or field unconfirmed and add an
uncertainty. The runtime also enforces this as a deterministic safety net.

The report must:
1. Decompose the question into at most 12 task_requirements covering entities,
   measures, filters, time scope, requested outputs, knowledge rules, and joins.
2. Build answer_projection as the smallest final table requested by the
   question. Classify each output column as direct, derived, or semantic and
   anchor it to real source fields or evidence. Derived outputs require an
   explicit operation and real inputs. Keep filtering, joining, sorting, and
   grouping fields in helper_fields; do not include them as answer columns
   unless the question explicitly requests them. Preserve separate atomic
   source fields as separate answer columns unless the question or anchored
   knowledge explicitly requires concatenation or another representation.
3. Map each requirement to recommended_sources. Mark a source confirmed only
   when the supplied evidence supports it; otherwise mark it candidate.
4. Carry applicable formulas, field mappings, value codes, units, output shapes,
   examples, and disambiguation rules into knowledge.applicable_rules. Every rule
   must cite a supplied knowledge evidence ID and source path.
5. Put unresolved or ambiguous choices in uncertainties with a concrete check
   the main Agent can perform. Do not guess.
6. Report joins only as candidates. Do not join complete datasets, aggregate,
   count, write SQL, execute Python, perform ETL, or submit an answer.

Projection rules:
- Read the requested output from the question, not from every field needed to
  solve it. An entity mentioned in a filter does not make its identifier an
  answer column. Fields used to select a maximum/minimum are sort helpers unless
  the question also asks for that value. Join keys and record IDs are helpers
  unless explicitly requested.
- When a question asks "what is the comment/title/name/description", return the
  canonical content/name field, not the whole record. Return identifiers,
  scores, or metadata only when the question explicitly requests them or asks
  for full record/details.
- A maximum/minimum phrase selects a row. For "what is the comment with the
  highest score", Text is the answer and Score is a sort helper. Score becomes
  an answer only for wording such as "what is the highest score" or "include its
  score". Never add an output merely because it is useful context, identifies a
  record, or helps verification.
- A request such as "for customers matching X, give their consumption" asks for
  consumption; CustomerID remains a filter/join helper unless identification is
  also requested.
- If knowledge says that first_name and last_name together represent a full
  name or that both must be used, emit two separate output columns. That wording
  alone is not evidence for concatenation. Concatenate only when the question or
  anchored knowledge explicitly specifies a single formatted name string.
- For outputs extracted from narrative Markdown/PDF, use semantic columns with
  the real document path and an evidence reference. Do not invent a structured
  schema field merely to mark the output confirmed.

Use only paths and fields present in the supplied Inventory. Observed data wins
when it conflicts with documentation. An incomplete, explicit guide is better
than an unsupported confident claim.
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


def _json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _has_material_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, list):
        return any(_has_material_value(item) for item in value)
    if isinstance(value, dict):
        ignored = {"output_truncated", "truncated", "warnings"}
        return any(key not in ignored and _has_material_value(item) for key, item in value.items())
    return True


def _compact_value(value: Any, budget: int) -> Any:
    if budget <= 0:
        return None
    if _json_chars(value) <= budget:
        return value
    if isinstance(value, str):
        return f"{value[: max(1, budget - 1)]}…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        compacted: list[Any] = []
        for item in value:
            remaining = budget - _json_chars(compacted) - 2
            if remaining < 24:
                break
            candidate = _compact_value(item, remaining)
            trial = [*compacted, candidate]
            if _json_chars(trial) > budget:
                break
            compacted.append(candidate)
        return compacted
    if isinstance(value, dict):
        compacted_dict: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            remaining = budget - _json_chars(compacted_dict) - len(key) - 5
            if remaining < 24:
                break
            candidate = _compact_value(item, remaining)
            trial = {**compacted_dict, key: candidate}
            if _json_chars(trial) > budget:
                break
            compacted_dict[key] = candidate
        return compacted_dict
    return _compact_value(str(value), budget)


def _question_tokens(question: str) -> set[str]:
    tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", question.casefold()))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", question):
        if len(chunk) <= 12:
            tokens.add(chunk)
        for width in (2, 3):
            tokens.update(chunk[index : index + width] for index in range(len(chunk) - width + 1))
    return tokens


def _markdown_sections(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", text))
    if not matches:
        return [("document", text)]
    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0 and text[: matches[0].start()].strip():
        sections.append(("preamble", text[: matches[0].start()].strip()))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(1).strip(), text[match.start() : end].strip()))
    return sections


def _select_knowledge_section(
    *,
    question: str,
    text: str,
    max_chars: int,
) -> tuple[str, str, bool]:
    sections = _markdown_sections(text)
    exact_matches = [item for item in sections if question.strip() and question.strip() in item[1]]
    if exact_matches:
        heading, section = exact_matches[0]
        return heading, section[:max_chars], len(section) > max_chars

    tokens = _question_tokens(question)
    ranked = sorted(
        (
            (len(tokens & _question_tokens(section)), heading, section)
            for heading, section in sections
        ),
        key=lambda item: (item[0], -len(item[2])),
        reverse=True,
    )
    selected = [item for item in ranked[:2] if item[0] > 0]
    if not selected:
        heading, section = sections[0] if sections else ("document", text)
        return heading, section[:max_chars], len(section) > max_chars
    headings = " | ".join(item[1] for item in selected)
    combined = "\n\n".join(item[2] for item in selected)
    return headings, combined[:max_chars], len(combined) > max_chars


def _literal_type(values: set[str]) -> Any:
    return Literal.__getitem__(tuple(sorted(values)))


def _preview_input_model(paths: set[str]) -> type[BaseModel]:
    return create_model(
        "TargetedPreviewInput",
        __base__=_StrictInput,
        path=(
            _literal_type(paths),
            Field(description="One exact non-knowledge path from the supplied Inventory."),
        ),
    )


def _grep_input_model(paths: set[str]) -> type[BaseModel]:
    return create_model(
        "TargetedGrepInput",
        __base__=_StrictInput,
        pattern=(str, Field(min_length=1, max_length=200)),
        path=(
            _literal_type(paths),
            Field(description="One exact path from the supplied Inventory."),
        ),
    )


def _sql_input_model(paths: set[str]) -> type[BaseModel]:
    return create_model(
        "TargetedSqlInput",
        __base__=_StrictInput,
        path=(
            _literal_type(paths),
            Field(description="One exact SQLite path from the supplied Inventory."),
        ),
        sql=(
            str,
            Field(
                min_length=1,
                max_length=4_000,
                description="One read-only SELECT, WITH, PRAGMA, or EXPLAIN query.",
            ),
        ),
        limit=(int, Field(default=200, ge=1, le=200)),
    )


def _summary_fields(summary: dict[str, Any]) -> dict[str | None, set[str]]:
    by_table: dict[str | None, set[str]] = {
        None: {
            str(value)
            for key in ("columns", "field_paths", "keys")
            for value in summary.get(key, [])
            if isinstance(value, (str, int, float))
        }
    }
    for table in summary.get("tables", []):
        if not isinstance(table, dict) or table.get("name") is None:
            continue
        table_name = str(table["name"])
        columns = table.get("columns", [])
        by_table[table_name] = {
            str(item.get("name") if isinstance(item, dict) else item)
            for item in columns
            if (isinstance(item, dict) and item.get("name") is not None) or isinstance(item, str)
        }
    return by_table


class _ExplorerTools:
    def __init__(self, *, config: ExplorerConfig, event_sink: EventSink | None = None) -> None:
        self.config = config
        self.event_sink = event_sink
        self.inventory: dict[str, Any] = {}
        self.discovered_paths: set[str] = set()
        self.sqlite_paths: set[str] = set()
        self.knowledge_paths: set[str] = set()
        self.field_map: dict[str, dict[str | None, set[str]]] = {}
        self.knowledge_evidence: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.prepared = False
        self.followup_used = False
        self.followup_outcome: _FollowupOutcome | None = None

    def _record_observation(
        self,
        *,
        source_tool: str,
        observation: dict[str, Any],
        path: str | None = None,
        ok: bool = True,
    ) -> dict[str, Any]:
        index = sum(item["source_tool"] == source_tool for item in self.observations) + 1
        prefix = {
            "inspect_files": "inspect",
            "knowledge_preview": "knowledge",
            "preview_file": "preview",
            "grep_context": "grep",
            "execute_context_sql": "sql",
        }.get(source_tool, "observation")
        evidence_id = f"{prefix}:{index}"
        recorded = {
            "evidence_id": evidence_id,
            "source_tool": source_tool,
            "path": path,
            "observation": observation,
            "ok": ok,
        }
        self.observations.append(recorded)
        return {"evidence_id": evidence_id, **observation}

    def prepare(self, task: PublicTask) -> None:
        if self.prepared:
            return
        inspection = inspect_context(task, self.config.inventory_limits())
        self.inventory = inspection
        self._record_observation(source_tool="inspect_files", observation=inspection)
        for item in inspection.get("files", []):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            path = str(item["path"])
            self.discovered_paths.add(path)
            if path.casefold().endswith(SQLITE_SUFFIXES):
                self.sqlite_paths.add(path)
            if path.rsplit("/", 1)[-1].casefold() == KNOWLEDGE_NAME:
                self.knowledge_paths.add(path)
            summary = item.get("summary")
            if isinstance(summary, dict):
                self.field_map[path] = _summary_fields(summary)
        emit_event(
            self.event_sink,
            "explorer_inspection_created",
            {
                "file_count": len(self.discovered_paths),
                "knowledge_file_count": len(self.knowledge_paths),
                "sqlite_file_count": len(self.sqlite_paths),
                "warning_count": len(inspection.get("warnings", [])),
                "read_bytes": inspection.get("budget", {}).get("read_bytes", 0),
                "truncated": bool(inspection.get("truncated")),
            },
        )
        for path in sorted(self.knowledge_paths):
            self._prepare_knowledge(task, path)
        self.prepared = True

    def _prepare_knowledge(self, task: PublicTask, path: str) -> None:
        try:
            resolved = resolve_context_path(task, path)
            with resolved.open("rb") as stream:
                payload = stream.read(self.config.max_single_file_bytes + 1)
            truncated_by_bytes = len(payload) > self.config.max_single_file_bytes
            text = payload[: self.config.max_single_file_bytes].decode(
                "utf-8",
                errors="replace",
            )
            section, excerpt, truncated_by_chars = _select_knowledge_section(
                question=task.question,
                text=text,
                max_chars=self.config.max_preview_chars,
            )
            evidence = self._record_observation(
                source_tool="knowledge_preview",
                path=path,
                observation={
                    "path": path,
                    "section": section,
                    "excerpt": excerpt,
                    "truncated": truncated_by_bytes or truncated_by_chars,
                },
            )
            self.knowledge_evidence.append(evidence)
            emit_event(
                self.event_sink,
                "explorer_knowledge_reviewed",
                {
                    "path": path,
                    "ok": True,
                    "evidence_id": evidence["evidence_id"],
                    "section_selected": True,
                    "truncated": bool(evidence["truncated"]),
                },
            )
        except Exception as exc:  # noqa: BLE001
            self._record_observation(
                source_tool="knowledge_preview",
                path=path,
                ok=False,
                observation={
                    "path": path,
                    "warning": {
                        "code": "KNOWLEDGE_PREVIEW_FAILED",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                },
            )
            emit_event(
                self.event_sink,
                "explorer_knowledge_reviewed",
                {"path": path, "ok": False, "error_type": type(exc).__name__},
            )

    def _known_fields(self, path: str, table: str | None) -> set[str]:
        tables = self.field_map.get(path, {})
        if table in tables:
            return tables[table]
        if table is None:
            return set().union(*tables.values()) if tables else set()
        return set()

    @staticmethod
    def _followup_is_conclusive(tool: str, result: ToolExecutionResult) -> bool:
        if not result.ok:
            return False
        if tool == "grep_context":
            return int(result.content.get("match_count", 0)) > 0
        if tool == "execute_context_sql":
            return int(result.content.get("row_count", 0)) > 0
        if tool == "preview_file":
            return _has_material_value(result.content.get("summary"))
        return False

    def record_followup(self, call: ModelToolCall, result: ToolExecutionResult) -> None:
        self.followup_used = True
        action_input = result.action_input if isinstance(result.action_input, dict) else {}
        raw_path = action_input.get("path")
        path = (
            str(raw_path)
            if isinstance(raw_path, str) and raw_path in self.discovered_paths
            else None
        )
        raw_evidence_id = result.content.get("evidence_id")
        evidence_id = str(raw_evidence_id) if isinstance(raw_evidence_id, str) else None
        self.followup_outcome = _FollowupOutcome(
            tool=call.name,
            ok=result.ok,
            conclusive=self._followup_is_conclusive(call.name, result),
            error_code=result.error_code,
            path=path,
            evidence_id=evidence_id,
        )
        emit_event(
            self.event_sink,
            "explorer_followup_assessed",
            {
                "tool": call.name,
                "ok": result.ok,
                "conclusive": self.followup_outcome.conclusive,
                "error_code": result.error_code,
                "path_known": path is not None,
                "evidence_id": evidence_id,
            },
        )

    def preview(self, task: PublicTask, arguments: BaseModel) -> ToolExecutionResult:
        path = str(getattr(arguments, "path"))
        if path not in self.discovered_paths or path in self.knowledge_paths:
            return _error_result(
                "PATH_NOT_INSPECTED",
                "preview_file path must be a non-knowledge path from Inventory.",
            )
        self.followup_used = True
        try:
            observation = preview_context_file(
                task,
                path,
                self.config.inventory_limits(),
                self.config.max_preview_chars,
            )
        except Exception as exc:  # noqa: BLE001
            return _error_result("PREVIEW_FAILED", f"{type(exc).__name__}: {exc}")
        content = self._record_observation(
            source_tool="preview_file",
            path=path,
            observation=observation,
        )
        return ToolExecutionResult(ok=True, content=content)

    def grep(self, task: PublicTask, arguments: BaseModel) -> ToolExecutionResult:
        path = str(getattr(arguments, "path"))
        if path not in self.discovered_paths:
            return _error_result(
                "PATH_NOT_INSPECTED",
                "grep_context path must be an exact path from Inventory.",
            )
        self.followup_used = True
        try:
            observation = grep_context(
                task,
                pattern=str(getattr(arguments, "pattern")),
                path_filter=path,
                max_results=30,
                max_files=self.config.max_files,
                max_total_read_bytes=self.config.max_total_read_bytes,
                max_single_file_bytes=self.config.max_single_file_bytes,
                max_output_chars=self.config.max_inventory_chars,
            )
        except ValueError as exc:
            return _error_result("INVALID_GREP_PATTERN", str(exc))
        content = self._record_observation(
            source_tool="grep_context",
            path=path,
            observation=observation,
        )
        return ToolExecutionResult(ok=True, content=content)

    def sql(self, task: PublicTask, arguments: BaseModel) -> ToolExecutionResult:
        path = str(getattr(arguments, "path"))
        if path not in self.sqlite_paths:
            return _error_result(
                "NOT_SQLITE",
                "execute_context_sql path must be an exact SQLite path from Inventory.",
            )
        self.followup_used = True
        try:
            observation = execute_exploration_sql(
                resolve_context_path(task, path),
                str(getattr(arguments, "sql")),
                limit=int(getattr(arguments, "limit")),
                max_output_chars=self.config.max_inventory_chars,
            )
        except (OSError, ValueError) as exc:
            return _error_result("EXPLORATION_SQL_ERROR", str(exc))
        content = self._record_observation(
            source_tool="execute_context_sql",
            path=path,
            observation=observation,
        )
        return ToolExecutionResult(ok=True, content=content)

    def _base_files(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self.inventory.get("files", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]

    def _base_schema_map(self) -> dict[str, Any]:
        schema_map: dict[str, Any] = {}
        for item in self._base_files():
            summary = item.get("summary")
            if isinstance(summary, dict) and summary:
                schema_map[str(item["path"])] = summary
        return schema_map

    def _valid_source_field(
        self,
        source: SourceField,
        *,
        allow_unstructured_field: bool,
    ) -> bool:
        if source.path not in self.discovered_paths:
            return False
        tables = self.field_map.get(source.path, {})
        if source.table is not None and source.table not in tables:
            return False
        if source.field is None:
            return True
        known_fields = self._known_fields(source.path, source.table)
        if known_fields:
            return source.field in known_fields
        return allow_unstructured_field

    def _project_answer_projection(
        self,
        raw_projection: dict[str, Any],
        requirement_ids: set[str],
        evidence_ids: set[str],
        warnings: list[Any],
    ) -> dict[str, Any]:
        projected_columns: list[dict[str, Any]] = []
        raw_columns = raw_projection.get("columns", [])
        if not isinstance(raw_columns, list):
            raw_columns = []
        for raw in raw_columns[:MAX_OUTPUT_COLUMNS]:
            try:
                column = OutputColumn.model_validate(raw)
            except ValidationError:
                warnings.append({"code": "INVALID_OUTPUT_COLUMN"})
                continue
            refs = [ref for ref in column.evidence_refs if ref in evidence_ids]
            allow_unstructured = column.kind == "semantic" and bool(refs)
            valid_sources = [
                source
                for source in column.source_fields
                if self._valid_source_field(
                    source,
                    allow_unstructured_field=allow_unstructured,
                )
            ]
            if len(valid_sources) != len(column.source_fields):
                warnings.append(
                    {
                        "code": "UNKNOWN_OUTPUT_SOURCE_DROPPED",
                        "output_column": column.name,
                    }
                )

            status = column.status
            if column.kind == "direct":
                confirmable = (
                    len(valid_sources) == 1
                    and valid_sources[0].field is not None
                    and bool(
                        self._known_fields(
                            valid_sources[0].path,
                            valid_sources[0].table,
                        )
                    )
                )
            elif column.kind == "derived":
                confirmable = (
                    len(valid_sources) == len(column.source_fields)
                    and bool(valid_sources)
                    and bool((column.operation or "").strip())
                )
            else:
                confirmable = bool(valid_sources or refs) and bool(refs)
            if status == "confirmed" and not confirmable:
                status = "candidate"
                warnings.append(
                    {
                        "code": "OUTPUT_COLUMN_DOWNGRADED",
                        "output_column": column.name,
                    }
                )
            projected_columns.append(
                {
                    **column.model_dump(mode="json"),
                    "source_fields": [source.model_dump(mode="json") for source in valid_sources],
                    "requirement_ids": [
                        item for item in column.requirement_ids if item in requirement_ids
                    ],
                    "evidence_refs": refs,
                    "status": status,
                }
            )

        projected_helpers: list[dict[str, Any]] = []
        raw_helpers = raw_projection.get("helper_fields", [])
        if not isinstance(raw_helpers, list):
            raw_helpers = []
        for raw in raw_helpers[:MAX_HELPER_FIELDS]:
            try:
                helper = HelperField.model_validate(raw)
            except ValidationError:
                warnings.append({"code": "INVALID_HELPER_FIELD"})
                continue
            if (
                not self._valid_source_field(
                    helper.source,
                    allow_unstructured_field=False,
                )
                or helper.source.field is None
            ):
                warnings.append({"code": "UNKNOWN_HELPER_FIELD_DROPPED"})
                continue
            projected_helpers.append(
                {
                    **helper.model_dump(mode="json"),
                    "requirement_ids": [
                        item for item in helper.requirement_ids if item in requirement_ids
                    ],
                }
            )
        return {
            "columns": projected_columns,
            "helper_fields": projected_helpers,
        }

    def _project_requirements(
        self,
        raw_items: list[dict[str, Any]],
        warnings: list[Any],
    ) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_items[:MAX_REQUIREMENTS]:
            try:
                requirement = TaskRequirement.model_validate(raw)
            except ValidationError:
                warnings.append({"code": "INVALID_TASK_REQUIREMENT"})
                continue
            if requirement.id in seen:
                warnings.append(
                    {"code": "DUPLICATE_REQUIREMENT_ID", "requirement_id": requirement.id}
                )
                continue
            seen.add(requirement.id)
            projected.append(requirement.model_dump(mode="json"))
        return projected

    def _project_sources(
        self,
        raw_items: list[dict[str, Any]],
        requirement_ids: set[str],
        warnings: list[Any],
    ) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for raw in raw_items[:MAX_RECOMMENDED_SOURCES]:
            try:
                source = RecommendedSource.model_validate(raw)
            except ValidationError:
                warnings.append({"code": "INVALID_RECOMMENDED_SOURCE"})
                continue
            if source.path not in self.discovered_paths:
                warnings.append({"code": "UNKNOWN_SOURCE_PATH", "path": source.path})
                continue
            known_fields = self._known_fields(source.path, source.table)
            valid_fields = [
                field for field in source.fields if not known_fields or field in known_fields
            ]
            if source.fields and len(valid_fields) != len(source.fields):
                warnings.append(
                    {
                        "code": "UNKNOWN_SOURCE_FIELD_DROPPED",
                        "path": source.path,
                    }
                )
            status = source.status
            if source.fields and not valid_fields:
                status = "candidate"
            projected.append(
                {
                    **source.model_dump(mode="json"),
                    "fields": valid_fields,
                    "requirement_ids": [
                        item for item in source.requirement_ids if item in requirement_ids
                    ],
                    "status": status,
                }
            )
        return projected

    def _project_knowledge(
        self,
        raw_knowledge: dict[str, Any],
        requirement_ids: set[str],
        warnings: list[Any],
    ) -> dict[str, Any]:
        valid_evidence = {
            str(item["evidence_id"])
            for item in self.knowledge_evidence
            if isinstance(item.get("evidence_id"), str)
        }
        projected_rules: list[dict[str, Any]] = []
        raw_rules = raw_knowledge.get("applicable_rules", [])
        if not isinstance(raw_rules, list):
            raw_rules = []
        for raw in raw_rules[:MAX_KNOWLEDGE_RULES]:
            try:
                rule = KnowledgeRule.model_validate(raw)
            except ValidationError:
                warnings.append({"code": "INVALID_KNOWLEDGE_RULE"})
                continue
            refs = [ref for ref in rule.evidence_refs if ref in valid_evidence]
            if rule.source_path not in self.knowledge_paths or not refs:
                warnings.append(
                    {
                        "code": "UNANCHORED_KNOWLEDGE_RULE",
                        "path": rule.source_path,
                    }
                )
                continue
            projected_rules.append(
                {
                    **rule.model_dump(mode="json"),
                    "evidence_refs": refs,
                    "requirement_ids": [
                        item for item in rule.requirement_ids if item in requirement_ids
                    ],
                }
            )
        return {
            "source_evidence": list(self.knowledge_evidence),
            "applicable_rules": projected_rules,
        }

    def _valid_endpoint(self, endpoint: JoinEndpoint) -> bool:
        return endpoint.path in self.discovered_paths and (
            not self._known_fields(endpoint.path, endpoint.table)
            or endpoint.field in self._known_fields(endpoint.path, endpoint.table)
        )

    def _project_joins(
        self,
        raw_items: list[dict[str, Any]],
        requirement_ids: set[str],
        warnings: list[Any],
    ) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for raw in raw_items[:MAX_JOIN_PATHS]:
            try:
                join = JoinCandidate.model_validate(raw)
            except ValidationError:
                warnings.append({"code": "INVALID_JOIN_CANDIDATE"})
                continue
            if not self._valid_endpoint(join.left) or not self._valid_endpoint(join.right):
                warnings.append({"code": "UNKNOWN_JOIN_ENDPOINT"})
                continue
            projected.append(
                {
                    **join.model_dump(mode="json"),
                    "requirement_ids": [
                        item for item in join.requirement_ids if item in requirement_ids
                    ],
                }
            )
        return projected

    def _project_uncertainties(
        self,
        raw_items: list[dict[str, Any]],
        requirement_ids: set[str],
        evidence_ids: set[str],
        warnings: list[Any],
    ) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for raw in raw_items[:MAX_UNCERTAINTIES]:
            try:
                uncertainty = Uncertainty.model_validate(raw)
            except ValidationError:
                warnings.append({"code": "INVALID_UNCERTAINTY"})
                continue
            if uncertainty.requirement_id not in requirement_ids:
                warnings.append(
                    {
                        "code": "UNKNOWN_UNCERTAINTY_REQUIREMENT",
                        "requirement_id": uncertainty.requirement_id,
                    }
                )
                continue
            projected.append(
                {
                    **uncertainty.model_dump(mode="json"),
                    "evidence_refs": [
                        ref for ref in uncertainty.evidence_refs if ref in evidence_ids
                    ],
                }
            )
        return projected

    def _apply_followup_guard(
        self,
        requirements: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        uncertainties: list[dict[str, Any]],
        warnings: list[Any],
    ) -> None:
        outcome = self.followup_outcome
        if outcome is None or outcome.conclusive:
            return

        requirement_id = "followup_verification"
        existing_ids = {str(item["id"]) for item in requirements}
        suffix = 2
        while requirement_id in existing_ids:
            requirement_id = f"followup_verification_{suffix}"
            suffix += 1
        if len(requirements) < MAX_REQUIREMENTS:
            requirements.append(
                {
                    "id": requirement_id,
                    "kind": "other",
                    "description": (
                        f"Verify the evidence requested by the targeted {outcome.tool} follow-up."
                    ),
                    "status": "unresolved",
                }
            )
        else:
            target = next(
                (item for item in requirements if item.get("status") == "unresolved"),
                requirements[0],
            )
            target["status"] = "unresolved"
            requirement_id = str(target["id"])

        if outcome.path is not None:
            for source in sources:
                if source.get("path") != outcome.path:
                    continue
                source["status"] = "candidate"
                linked = list(source.get("requirement_ids", []))
                if requirement_id not in linked and len(linked) < MAX_REQUIREMENTS:
                    linked.append(requirement_id)
                source["requirement_ids"] = linked

        warning_code = (
            "EXPLORER_FOLLOWUP_FAILED" if not outcome.ok else "EXPLORER_FOLLOWUP_INCONCLUSIVE"
        )
        warnings.insert(
            0,
            {
                "code": warning_code,
                "tool": outcome.tool,
                "error_code": outcome.error_code,
            },
        )
        if len(uncertainties) >= MAX_UNCERTAINTIES:
            uncertainties.pop()
            warnings.insert(1, {"code": "UNCERTAINTY_REPLACED_FOR_FOLLOWUP_GUARD"})
        issue = (
            f"Targeted {outcome.tool} follow-up failed"
            + (f" ({outcome.error_code})" if outcome.error_code else "")
            + "; its intended source or field was not confirmed."
            if not outcome.ok
            else (
                f"Targeted {outcome.tool} follow-up returned no material evidence; "
                "its intended source or field remains unconfirmed."
            )
        )
        uncertainties.append(
            {
                "requirement_id": requirement_id,
                "issue": issue,
                "candidates": [outcome.path] if outcome.path is not None else [],
                "evidence_refs": ([outcome.evidence_id] if outcome.evidence_id is not None else []),
                "verification_hint": (
                    f"Re-check {outcome.path} with the normal task tools before using it."
                    if outcome.path is not None
                    else (
                        "Select a valid source from files/schema_map and verify the intended "
                        "field with the normal task tools."
                    )
                ),
            }
        )

    def _project_etl(
        self,
        raw_items: list[dict[str, Any]],
        warnings: list[Any],
    ) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for raw in raw_items[:MAX_ETL_CANDIDATES]:
            try:
                candidate = EtlCandidate.model_validate(raw)
            except ValidationError:
                warnings.append({"code": "INVALID_ETL_CANDIDATE"})
                continue
            if candidate.path not in self.discovered_paths:
                warnings.append({"code": "UNKNOWN_ETL_PATH", "path": candidate.path})
                continue
            projected.append(candidate.model_dump(mode="json"))
        return projected

    def _project_values(
        self,
        raw_values: dict[str, Any],
        warnings: list[Any],
    ) -> dict[str, Any]:
        projected: dict[str, Any] = {}
        for key, value in list(raw_values.items())[:MAX_VALUE_SAMPLES]:
            if not isinstance(key, str) or not any(
                key == path or key.startswith(f"{path}.") for path in self.discovered_paths
            ):
                warnings.append({"code": "UNKNOWN_VALUE_SAMPLE_SOURCE"})
                continue
            if not isinstance(value, list):
                warnings.append({"code": "INVALID_VALUE_SAMPLE"})
                continue
            projected[key] = value[:20]
        return projected

    def _bound_report(self, report: dict[str, Any]) -> dict[str, Any]:
        hard_limit = max(self.config.max_report_chars, self.config.max_inventory_chars * 2)
        if _json_chars(report) <= hard_limit:
            return report
        emit_event(
            self.event_sink,
            "explorer_report_budget_exceeded",
            {
                "semantic_report_chars": _json_chars(report),
                "hard_report_chars": hard_limit,
            },
        )
        fields = (
            ("task_interpretation", 0.04),
            ("task_requirements", 0.08),
            ("answer_projection", 0.10),
            ("recommended_sources", 0.14),
            ("files", 0.14),
            ("schema_map", 0.16),
            ("knowledge", 0.16),
            ("etl_candidates", 0.03),
            ("join_paths", 0.05),
            ("value_samples", 0.03),
            ("uncertainties", 0.05),
            ("warnings", 0.02),
        )
        bounded = {
            name: _compact_value(report.get(name), max(80, int(hard_limit * ratio)))
            for name, ratio in fields
        }
        warnings = bounded.get("warnings")
        if isinstance(warnings, list):
            warnings.append({"code": "EXPLORER_REPORT_COMPACTED"})
        return bounded

    def report(self, _: PublicTask, arguments: ExplorerReportInput) -> ToolExecutionResult:
        warnings = list(arguments.warnings[:MAX_WARNINGS])
        requirements = self._project_requirements(arguments.task_requirements, warnings)
        requirement_ids = {str(item["id"]) for item in requirements}
        evidence_ids = {
            str(item["evidence_id"])
            for item in self.observations
            if isinstance(item.get("evidence_id"), str)
        }
        sources = self._project_sources(
            arguments.recommended_sources,
            requirement_ids,
            warnings,
        )
        answer_projection = self._project_answer_projection(
            arguments.answer_projection.model_dump(mode="json"),
            requirement_ids,
            evidence_ids,
            warnings,
        )
        output_requirements = {
            str(item["id"])
            for item in requirements
            if item.get("kind") == "output" and item.get("status") == "resolved"
        }
        unresolved_output = any(
            item.get("kind") == "output" and item.get("status") != "resolved"
            for item in requirements
        )
        covered_output_requirements = {
            str(requirement_id)
            for column in answer_projection["columns"]
            for requirement_id in column.get("requirement_ids", [])
        }
        every_column_is_requested = all(
            output_requirements
            & {str(requirement_id) for requirement_id in column.get("requirement_ids", [])}
            for column in answer_projection["columns"]
        )
        answer_projection["enforceable"] = (
            bool(output_requirements)
            and not unresolved_output
            and output_requirements <= covered_output_requirements
            and bool(answer_projection["columns"])
            and every_column_is_requested
            and all(column.get("status") == "confirmed" for column in answer_projection["columns"])
        )
        if not answer_projection["enforceable"]:
            warnings.append({"code": "ANSWER_PROJECTION_NOT_ENFORCEABLE"})
        uncertainties = self._project_uncertainties(
            arguments.uncertainties,
            requirement_ids,
            evidence_ids,
            warnings,
        )
        self._apply_followup_guard(requirements, sources, uncertainties, warnings)
        report = {
            "task_interpretation": arguments.task_interpretation,
            "task_requirements": requirements,
            "answer_projection": answer_projection,
            "recommended_sources": sources,
            "files": self._base_files(),
            "schema_map": self._base_schema_map(),
            "knowledge": self._project_knowledge(
                arguments.knowledge.model_dump(mode="json"),
                requirement_ids,
                warnings,
            ),
            "etl_candidates": self._project_etl(arguments.etl_candidates, warnings),
            "join_paths": self._project_joins(
                arguments.join_paths,
                requirement_ids,
                warnings,
            ),
            "value_samples": self._project_values(arguments.value_samples, warnings),
            "uncertainties": uncertainties,
            "warnings": warnings[:MAX_WARNINGS],
        }
        return ToolExecutionResult(
            ok=True,
            content={"report": self._bound_report(report), "evidence": list(self.observations)},
            is_terminal=True,
        )

    def fallback(
        self,
        task: PublicTask,
        reason: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        requirement = {
            "id": "task_goal",
            "kind": "other",
            "description": task.question[:300] or "Complete the requested data task.",
            "status": "unresolved",
        }
        recommended = []
        for item in self._base_files()[:MAX_RECOMMENDED_SOURCES]:
            path = str(item["path"])
            if path in self.knowledge_paths:
                continue
            recommended.append(
                {
                    "path": path,
                    "table": None,
                    "fields": [],
                    "purpose": "Candidate task source from deterministic Inventory.",
                    "reason": "Semantic synthesis did not complete; inspect this source if relevant.",
                    "requirement_ids": ["task_goal"],
                    "status": "candidate",
                }
            )
        report = {
            "task_interpretation": "Explorer semantic synthesis did not complete.",
            "task_requirements": [requirement],
            "answer_projection": {
                "columns": [],
                "helper_fields": [],
                "enforceable": False,
            },
            "recommended_sources": recommended,
            "files": self._base_files(),
            "schema_map": self._base_schema_map(),
            "knowledge": {
                "source_evidence": list(self.knowledge_evidence),
                "applicable_rules": [],
            },
            "etl_candidates": [],
            "join_paths": [],
            "value_samples": {},
            "uncertainties": [
                {
                    "requirement_id": "task_goal",
                    "issue": reason,
                    "candidates": [item["path"] for item in recommended],
                    "evidence_refs": [
                        item["evidence_id"]
                        for item in self.knowledge_evidence
                        if isinstance(item.get("evidence_id"), str)
                    ],
                    "verification_hint": (
                        "Use the normal task tools to verify the candidate source and fields."
                    ),
                }
            ],
            "warnings": [{"code": "EXPLORER_FALLBACK", "message": reason}],
        }
        self._apply_followup_guard(
            report["task_requirements"],
            report["recommended_sources"],
            report["uncertainties"],
            report["warnings"],
        )
        return self._bound_report(report), list(self.observations)

    def context_bundle(self, task: PublicTask) -> dict[str, Any]:
        return {
            "question": task.question,
            "inventory": self.inventory,
            "knowledge_evidence": self.knowledge_evidence,
            "constraints": {
                "maximum_targeted_followups": 1,
                "sqlite_paths": sorted(self.sqlite_paths),
                "all_paths": sorted(self.discovered_paths),
            },
        }

    def registry(self, *, report_only: bool) -> ToolRegistry:
        report_spec = ToolSpec(
            name="report",
            description=(
                "Submit the structured reference guide. Include task_interpretation, "
                "task_requirements, answer_projection, recommended_sources, "
                "knowledge.applicable_rules, candidate join_paths, uncertainties, "
                "and objective warnings. The "
                "runtime supplies files, schema_map, and knowledge source evidence."
            ),
            input_model=ExplorerReportInput,
            handler=self.report,
            is_terminal=True,
        )
        if report_only:
            return ToolRegistry(specs={"report": report_spec})

        specs: dict[str, ToolSpec] = {"report": report_spec}
        preview_paths = self.discovered_paths - self.knowledge_paths
        if self.config.max_preview_calls > 0 and preview_paths:
            specs["preview_file"] = ToolSpec(
                name="preview_file",
                description=(
                    "Use at most once for a task-critical non-knowledge file whose "
                    "bounded Inventory summary is insufficient."
                ),
                input_model=_preview_input_model(preview_paths),
                handler=self.preview,
            )
        if self.discovered_paths:
            specs["grep_context"] = ToolSpec(
                name="grep_context",
                description=(
                    "Use at most once to locate a specific term in one exact Inventory path."
                ),
                input_model=_grep_input_model(self.discovered_paths),
                handler=self.grep,
            )
        if self.sqlite_paths:
            specs["execute_context_sql"] = ToolSpec(
                name="execute_context_sql",
                description=(
                    "Use at most once for a bounded read-only discovery query on one "
                    "exact SQLite path from Inventory."
                ),
                input_model=_sql_input_model(self.sqlite_paths),
                handler=self.sql,
            )
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
        task: PublicTask,
        tools: _ExplorerTools,
        steps_used: int,
        reason: str,
        reason_code: str,
    ) -> ExplorerResult:
        report, evidence = tools.fallback(task, reason)
        emit_event(
            self.event_sink,
            "explorer_fallback_used",
            {
                "steps_used": steps_used,
                "reason_code": reason_code,
                "observation_count": len(evidence),
                "report_chars": _json_chars(report),
            },
        )
        emit_event(
            self.event_sink,
            "explorer_completed",
            {
                "steps_used": steps_used,
                "fallback_used": True,
                "success": bool(evidence),
                "observation_count": len(evidence),
                "report_chars": _json_chars(report),
                "knowledge_evidence_count": len(tools.knowledge_evidence),
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

    def _complete(
        self,
        *,
        task: PublicTask,
        tools: _ExplorerTools,
        report: dict[str, Any],
        evidence: list[dict[str, Any]],
        steps_used: int,
    ) -> ExplorerResult:
        emit_event(
            self.event_sink,
            "explorer_completed",
            {
                "steps_used": steps_used,
                "fallback_used": False,
                "file_count": len(report.get("files", [])),
                "schema_count": len(report.get("schema_map", {})),
                "requirement_count": len(report.get("task_requirements", [])),
                "output_column_count": len(report.get("answer_projection", {}).get("columns", [])),
                "helper_field_count": len(
                    report.get("answer_projection", {}).get("helper_fields", [])
                ),
                "recommended_source_count": len(report.get("recommended_sources", [])),
                "knowledge_evidence_count": len(
                    report.get("knowledge", {}).get("source_evidence", [])
                ),
                "knowledge_rule_count": len(
                    report.get("knowledge", {}).get("applicable_rules", [])
                ),
                "uncertainty_count": len(report.get("uncertainties", [])),
                "observation_count": len(evidence),
                "report_chars": _json_chars(report),
            },
        )
        return ExplorerResult(
            success=True,
            report=report,
            evidence=evidence,
            steps_used=steps_used,
        )

    def run(self, task: PublicTask, _: ExploreInput) -> ExplorerResult:
        started_at = monotonic()
        tools = _ExplorerTools(config=self.config, event_sink=self.event_sink)
        effective_requests = min(max(1, self.config.max_steps), MAX_MODEL_REQUESTS)
        emit_event(
            self.event_sink,
            "explorer_started",
            {
                "max_steps": self.config.max_steps,
                "max_model_requests": effective_requests,
                "max_duration_seconds": self.config.max_duration_seconds,
            },
        )
        if self.config.max_duration_seconds <= 0:
            return self._fallback_result(
                task=task,
                tools=tools,
                steps_used=0,
                reason="Explorer exceeded its soft time budget.",
                reason_code="SOFT_TIMEOUT",
            )
        try:
            tools.prepare(task)
        except Exception as exc:  # noqa: BLE001
            return self._fallback_result(
                task=task,
                tools=tools,
                steps_used=0,
                reason=f"Explorer could not scan context: {type(exc).__name__}.",
                reason_code="INSPECT_FAILED",
            )
        if monotonic() - started_at >= self.config.max_duration_seconds:
            return self._fallback_result(
                task=task,
                tools=tools,
                steps_used=0,
                reason="Explorer exceeded its soft time budget.",
                reason_code="SOFT_TIMEOUT",
            )

        messages = [
            ModelMessage(role="system", content=EXPLORER_SYSTEM_PROMPT),
            ModelMessage(
                role="user",
                content=json.dumps(
                    tools.context_bundle(task),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            ),
        ]

        for request_index in range(1, effective_requests + 1):
            if monotonic() - started_at >= self.config.max_duration_seconds:
                return self._fallback_result(
                    task=task,
                    tools=tools,
                    steps_used=request_index - 1,
                    reason="Explorer exceeded its soft time budget.",
                    reason_code="SOFT_TIMEOUT",
                )
            report_only = request_index == 2
            registry = tools.registry(report_only=report_only)
            try:
                response = self.model.complete(
                    messages,
                    tools=registry,
                    request_context={
                        "task_id": task.task_id,
                        "agent_scope": "explorer",
                        "explorer_step_index": request_index,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                return self._fallback_result(
                    task=task,
                    tools=tools,
                    steps_used=request_index,
                    reason=f"Explorer model request failed: {type(exc).__name__}.",
                    reason_code="MODEL_FAILURE",
                )

            calls = response.tool_calls
            if len(calls) != 1 or not calls[0].id or not calls[0].name:
                emit_event(
                    self.event_sink,
                    "explorer_step_completed",
                    {
                        "explorer_step_index": request_index,
                        "ok": False,
                        "error_code": "ONE_TOOL_CALL_REQUIRED",
                        "tool_call_count": len(calls),
                    },
                )
                if request_index < effective_requests:
                    messages.append(
                        ModelMessage(
                            role="user",
                            content=(
                                "Call report as the only tool now. A compact incomplete "
                                "guide with explicit uncertainties is acceptable."
                            ),
                        )
                    )
                    continue
                return self._fallback_result(
                    task=task,
                    tools=tools,
                    steps_used=request_index,
                    reason="Explorer did not submit one valid report tool call.",
                    reason_code="REPORT_REQUIRED",
                )

            call = calls[0]
            messages.append(_assistant_message(response))
            result = registry.execute(task, call)
            if call.name != "report":
                tools.record_followup(call, result)
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
                    "explorer_step_index": request_index,
                    "tool_call_id": call.id,
                    "tool": call.name,
                    "ok": result.ok,
                    "error_code": result.error_code,
                },
            )
            if call.name == "report" and result.is_terminal and result.ok:
                return self._complete(
                    task=task,
                    tools=tools,
                    report=result.content["report"],
                    evidence=result.content["evidence"],
                    steps_used=request_index,
                )
            if request_index < effective_requests:
                messages.append(
                    ModelMessage(
                        role="user",
                        content=(
                            "Use the supplied Inventory, knowledge evidence, and the one "
                            "targeted observation above. If it failed or returned no material "
                            "evidence, keep the affected fact unconfirmed and add an explicit "
                            "uncertainty. Call report as the only tool now."
                        ),
                    )
                )
                continue
            return self._fallback_result(
                task=task,
                tools=tools,
                steps_used=request_index,
                reason="Explorer targeted follow-up did not produce a valid report.",
                reason_code="REPORT_REQUIRED",
            )

        return self._fallback_result(
            task=task,
            tools=tools,
            steps_used=effective_requests,
            reason="Explorer exhausted its bounded model requests.",
            reason_code="REQUEST_BUDGET_EXHAUSTED",
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
            "Use first as explore({}). It deterministically scans all supported "
            "Phase 1 context files, extracts source-anchored knowledge.md evidence, "
            "and uses at most two model requests to return task requirements, "
            "a source-grounded answer projection, recommended files and fields, "
            "knowledge rules, candidate joins, and explicit uncertainties."
        ),
        input_model=ExploreInput,
        handler=ExplorerToolHandler(
            model=model,
            config=config,
            event_sink=event_sink,
        ),
    )
