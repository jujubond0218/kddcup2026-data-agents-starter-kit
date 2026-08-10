from __future__ import annotations

from keyword import iskeyword
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from data_agent_baseline.benchmark.schema import PublicTask
from data_agent_baseline.tools.registry import ToolExecutionResult, ToolRegistry, ToolSpec

# Narrative (unstructured) files may only be referenced by path; they carry no
# verifiable structured fields and must never be quoted as a fabricated schema.
NARRATIVE_KINDS = frozenset({"markdown", "text", "pdf"})
STRUCTURED_KINDS = frozenset({"tabular", "json", "sqlite"})

# Deterministic verification actions. Python execution and directory listing are
# deliberately excluded: the plan must be checkable purely with bounded reads.
_VERIFICATION_TOOL = Literal[
    "read_csv",
    "read_json",
    "read_doc",
    "inspect_sqlite_schema",
    "execute_context_sql",
]

# File kind -> compatible verification tools.
_TOOLS_FOR_KIND: dict[str, frozenset[str]] = {
    "tabular": frozenset({"read_csv"}),
    "json": frozenset({"read_json"}),
    "sqlite": frozenset({"inspect_sqlite_schema", "execute_context_sql"}),
    "markdown": frozenset({"read_doc"}),
    "text": frozenset({"read_doc"}),
    "pdf": frozenset({"read_doc"}),
}

MAX_ITEMS = 12
MAX_CANDIDATES = 4
MAX_SOURCE_FIELDS = 4
MAX_VERIFICATIONS = 3

COVERAGE_ERROR = "EVIDENCE_PLAN_REQUIREMENT_COVERAGE"
INVALID_SOURCE_ERROR = "EVIDENCE_PLAN_INVALID_SOURCE"
INVALID_VERIFICATION_ERROR = "EVIDENCE_PLAN_INVALID_VERIFICATION"


class EvidencePlanInput(BaseModel):
    """Strict input model for the dynamic commit_evidence_plan tool."""

    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceSourceField(EvidencePlanInput):
    path: str = Field(min_length=1, max_length=500)
    table: str | None = Field(default=None, max_length=200)
    field: str | None = Field(default=None, max_length=200)


class EvidenceCandidate(EvidencePlanInput):
    claim: str = Field(min_length=1, max_length=300)
    source_fields: list[EvidenceSourceField] = Field(
        min_length=1,
        max_length=MAX_SOURCE_FIELDS,
    )
    operation: Literal["measure", "filter", "time_scope", "join"]


class EvidenceVerification(EvidencePlanInput):
    tool: _VERIFICATION_TOOL
    path: str = Field(min_length=1, max_length=500)
    purpose: str = Field(min_length=1, max_length=500)


class EvidenceItem(EvidencePlanInput):
    requirement_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    candidates: list[EvidenceCandidate] = Field(min_length=1, max_length=MAX_CANDIDATES)
    verifications: list[EvidenceVerification] = Field(
        min_length=1,
        max_length=MAX_VERIFICATIONS,
    )


class CommitEvidencePlanInput(EvidencePlanInput):
    items: list[EvidenceItem] = Field(min_length=1, max_length=MAX_ITEMS)


class _StrictPlanItem(EvidencePlanInput):
    """One requirement's plan body when the coverage key set is schema-enforced.

    The model no longer emits a `requirement_id`; the ID is the `items` key.
    """

    candidates: list[EvidenceCandidate] = Field(min_length=1, max_length=MAX_CANDIDATES)
    verifications: list[EvidenceVerification] = Field(
        min_length=1,
        max_length=MAX_VERIFICATIONS,
    )


# Reserved Pydantic/BaseModel names that must never be used as a dynamic field name.
_RESERVED_FIELD_NAMES = frozenset(
    {
        "model_config",
        "model_fields",
        "model_computed_fields",
        "model_extra",
        "model_validate",
        "model_dump",
        "model_json_schema",
        "copy",
        "dict",
        "json",
        "parse_obj",
    }
)


def _safe_field_name(requirement_id: str) -> str | None:
    """Return a usable Pydantic field name for an ID, or None to fall back to an alias."""
    if not requirement_id.isidentifier() or iskeyword(requirement_id):
        return None
    if requirement_id in _RESERVED_FIELD_NAMES:
        return None
    return requirement_id


def _strict_items_model(pending_ids: set[str]) -> type[BaseModel]:
    """Build a dynamic strict `items` schema: one required key per pending ID.

    Each pending requirement ID becomes a required key; unknown keys are forbidden
    (`extra="forbid"`). Coverage is therefore structurally enforced: the model cannot
    omit a pending ID or invent a new one. Runtime recovers the ID from the key.
    """
    fields: dict[str, Any] = {}
    for index, requirement_id in enumerate(sorted(pending_ids)):
        field_name = _safe_field_name(requirement_id) or f"item_{index}"
        if field_name == requirement_id:
            fields[field_name] = (_StrictPlanItem, ...)
        else:
            fields[field_name] = (_StrictPlanItem, Field(alias=requirement_id))
    return create_model(
        "StrictPlanItems",
        __base__=EvidencePlanInput,
        **fields,
    )


def _strict_plan_input_model(pending_ids: set[str]) -> type[BaseModel]:
    """Strict commit tool input whose `items` object keys are the pending IDs."""
    return create_model(
        "CommitEvidencePlanStrictInput",
        __base__=EvidencePlanInput,
        items=(_strict_items_model(pending_ids), ...),
    )


_STRICT_TOOL_DESCRIPTION = (
    "Commit a deterministic evidence plan resolving every pending requirement and "
    "uncertainty from the explore report. The `items` object has exactly one required "
    "key per pending requirement/uncertainty ID -- fill candidates and verifications "
    "for every key and never add or omit a key. For each key, declare 1-4 candidate "
    "interpretations (a claim, real source_fields taken from the report files/"
    "schema_map, and one operation of measure/filter/time_scope/join) and 1-3 "
    "deterministic verifications (tool in read_csv/read_json/read_doc/"
    "inspect_sqlite_schema/execute_context_sql on an exact context path referenced by "
    "that key's candidate source_fields). Narrative markdown/text/PDF sources may be "
    "referenced only by path without invented fields. The plan is validated "
    "deterministically; it does not read gold, execute code, or verify answer "
    "semantics."
)


def pending_requirement_ids(report: dict[str, Any]) -> set[str]:
    """Union of unresolved task_requirements and uncertainty requirement IDs."""
    pending: set[str] = set()
    for item in report.get("task_requirements", []):
        if (
            isinstance(item, dict)
            and item.get("status") != "resolved"
            and isinstance(item.get("id"), str)
        ):
            pending.add(item["id"])
    for item in report.get("uncertainties", []):
        if isinstance(item, dict) and isinstance(item.get("requirement_id"), str):
            pending.add(item["requirement_id"])
    return pending


def _path_kind(path: str, report: dict[str, Any]) -> str | None:
    for entry in report.get("files", []):
        if isinstance(entry, dict) and entry.get("path") == path:
            kind = entry.get("kind")
            return str(kind) if isinstance(kind, str) else None
    return None


def _summary_for_path(path: str, report: dict[str, Any]) -> dict[str, Any] | None:
    schema_map = report.get("schema_map")
    if isinstance(schema_map, dict):
        raw = schema_map.get(path)
        if isinstance(raw, dict):
            return raw
    for entry in report.get("files", []):
        if isinstance(entry, dict) and entry.get("path") == path:
            summary = entry.get("summary")
            return summary if isinstance(summary, dict) else None
    return None


def _summary_fields(summary: dict[str, Any]) -> dict[str | None, set[str]]:
    """Mirror the Explorer's field map: top-level fields plus per-table columns."""
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


def _structured_field_exists(
    *,
    path: str,
    table: str | None,
    field: str,
    report: dict[str, Any],
) -> bool:
    summary = _summary_for_path(path, report)
    if not isinstance(summary, dict):
        return False
    fields_by_table = _summary_fields(summary)
    if table is not None:
        if table not in fields_by_table:
            return False
        return field in fields_by_table[table]
    if not fields_by_table:
        return False
    return field in set().union(*fields_by_table.values())


def _error_result(code: str, message: str, guidance: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=False,
        content={
            "error": {
                "code": code,
                "message": message,
                "guidance": guidance,
                "recoverable": True,
            }
        },
        error_code=code,
        recoverable=True,
    )


class EvidencePlanController:
    """Deterministic guard for one committed evidence plan over an explore report.

    It only re-validates the plan against the report the main Agent already saw. It
    does not read gold, run the verifications, block a later answer, or judge answer
    semantics.
    """

    def __init__(self, *, report: dict[str, Any], strict_keys: bool = False) -> None:
        self.report = report
        self.strict_keys = strict_keys
        self.pending_ids = pending_requirement_ids(report)
        self.committed_items: list[dict[str, Any]] | None = None
        self.committed_verifications: list[tuple[str, str]] = []
        self._verification_set: set[tuple[str, str]] = set()

    def triggered(self) -> bool:
        return bool(self.pending_ids)

    def registry(self) -> ToolRegistry:
        return ToolRegistry(specs={"commit_evidence_plan": self.tool_spec()})

    def tool_spec(self) -> ToolSpec:
        if self.strict_keys:
            return ToolSpec(
                name="commit_evidence_plan",
                description=_STRICT_TOOL_DESCRIPTION,
                input_model=_strict_plan_input_model(self.pending_ids),
                handler=self.commit,
            )
        return ToolSpec(
            name="commit_evidence_plan",
            description=(
                "Commit a deterministic evidence plan resolving every pending "
                "requirement and uncertainty from the explore report. Each item must "
                "target exactly one pending requirement_id (the union of unresolved "
                "task_requirements and uncertainty requirement_ids), declare 1-4 "
                "candidate interpretations (a claim, real source_fields taken from the "
                "report files/schema_map, and one operation of measure/filter/"
                "time_scope/join), and 1-3 deterministic verifications (tool in "
                "read_csv/read_json/read_doc/inspect_sqlite_schema/execute_context_sql "
                "on an exact context path referenced by that item's candidate "
                "source_fields). Narrative markdown/text/PDF sources may be referenced "
                "only by path without invented fields. The plan is validated "
                "deterministically; it does not read gold, execute code, or verify "
                "answer semantics."
            ),
            input_model=CommitEvidencePlanInput,
            handler=self.commit,
        )

    def commit(
        self,
        _: PublicTask,
        arguments: CommitEvidencePlanInput,
    ) -> ToolExecutionResult:
        items = self._normalize_items(arguments)
        error = self._validate(items)
        if error is not None:
            code, message, guidance = error
            return _error_result(code, message, guidance)

        self.committed_items = [item.model_dump(mode="json") for item in items]
        self.committed_verifications = [
            (verification.tool, verification.path)
            for item in items
            for verification in item.verifications
        ]
        self._verification_set = set(self.committed_verifications)
        return ToolExecutionResult(
            ok=True,
            content={
                "status": "committed",
                "item_count": len(items),
                "candidate_count": sum(len(item.candidates) for item in items),
                "verification_count": len(self.committed_verifications),
                "requirement_ids": sorted(self.pending_ids),
            },
        )

    def _normalize_items(self, arguments: CommitEvidencePlanInput) -> list[EvidenceItem]:
        """Reconstruct the canonical item list for either input shape.

        In strict mode the `items` object is keyed by requirement ID, so the ID is
        recovered from the key instead of being generated by the model.
        """
        if not self.strict_keys:
            return list(arguments.items)
        raw_items = arguments.items.model_dump(mode="json", by_alias=True)
        return [
            EvidenceItem(requirement_id=requirement_id, **value)
            for requirement_id, value in raw_items.items()
        ]

    def matches_verification(self, *, tool: str, path: str) -> bool:
        return (tool, path) in self._verification_set

    def _validate(
        self,
        items: list[EvidenceItem],
    ) -> tuple[str, str, str] | None:
        coverage = self._validate_coverage(items)
        if coverage is not None:
            return coverage
        for item in items:
            source_error = self._validate_sources(item)
            if source_error is not None:
                return source_error
            verification_error = self._validate_verifications(item)
            if verification_error is not None:
                return verification_error
        return None

    def _validate_coverage(
        self,
        items: list[EvidenceItem],
    ) -> tuple[str, str, str] | None:
        item_ids = [item.requirement_id for item in items]
        if len(set(item_ids)) != len(item_ids):
            return (
                COVERAGE_ERROR,
                "The plan declares the same requirement_id more than once.",
                "Each pending requirement/uncertainty must be covered by exactly one "
                "plan item; remove duplicate requirement_id entries.",
            )
        declared = set(item_ids)
        if declared != self.pending_ids:
            missing = sorted(self.pending_ids - declared)
            extra = sorted(declared - self.pending_ids)
            message_parts = []
            if missing:
                message_parts.append(f"missing requirement IDs: {', '.join(missing)}")
            if extra:
                message_parts.append(f"undeclared requirement IDs: {', '.join(extra)}")
            return (
                COVERAGE_ERROR,
                "The plan does not match the pending requirements exactly; "
                + "; ".join(message_parts)
                + ".",
                "Cover exactly the union of unresolved task_requirements and "
                "uncertainty requirement IDs, with no omissions, duplicates, or extras.",
            )
        return None

    def _validate_sources(
        self,
        item: EvidenceItem,
    ) -> tuple[str, str, str] | None:
        for candidate in item.candidates:
            for source in candidate.source_fields:
                kind = _path_kind(source.path, self.report)
                if kind is None:
                    return (
                        INVALID_SOURCE_ERROR,
                        f"Source path '{source.path}' is not a context file in the explore report.",
                        "Reference only exact paths listed in the explore report files.",
                    )
                if kind in NARRATIVE_KINDS:
                    if source.field is not None:
                        return (
                            INVALID_SOURCE_ERROR,
                            f"Narrative file '{source.path}' may only be referenced by "
                            f"path; the invented field '{source.field}' is not allowed.",
                            "For markdown/text/PDF sources, omit table and field and "
                            "reference the path only.",
                        )
                    if source.table is not None:
                        return (
                            INVALID_SOURCE_ERROR,
                            f"Narrative file '{source.path}' has no structured table.",
                            "Omit the table field for narrative sources.",
                        )
                    continue
                if source.field is None:
                    return (
                        INVALID_SOURCE_ERROR,
                        f"Structured file '{source.path}' requires a real field.",
                        "Name the exact field that exists in the file's schema.",
                    )
                if not _structured_field_exists(
                    path=source.path,
                    table=source.table,
                    field=source.field,
                    report=self.report,
                ):
                    table_suffix = f" in table '{source.table}'" if source.table is not None else ""
                    return (
                        INVALID_SOURCE_ERROR,
                        f"Field '{source.field}' does not exist in '{source.path}'{table_suffix}.",
                        "Use only fields visible in the explore report schema_map.",
                    )
        return None

    def _validate_verifications(
        self,
        item: EvidenceItem,
    ) -> tuple[str, str, str] | None:
        candidate_paths = {
            source.path for candidate in item.candidates for source in candidate.source_fields
        }
        for verification in item.verifications:
            if verification.path not in candidate_paths:
                return (
                    INVALID_VERIFICATION_ERROR,
                    f"Verification path '{verification.path}' for requirement "
                    f"'{item.requirement_id}' is not referenced by any candidate source "
                    "field.",
                    "Each verification path must belong to that item's candidate source_fields.",
                )
            kind = _path_kind(verification.path, self.report)
            if kind is None:
                return (
                    INVALID_VERIFICATION_ERROR,
                    f"Verification path '{verification.path}' is not a context file in "
                    "the explore report.",
                    "Reference only exact paths listed in the explore report files.",
                )
            compatible = _TOOLS_FOR_KIND.get(kind, frozenset())
            if verification.tool not in compatible:
                return (
                    INVALID_VERIFICATION_ERROR,
                    f"Verification tool '{verification.tool}' is incompatible with the "
                    f"'{kind}' file '{verification.path}'.",
                    "Choose read_csv for tabular files, read_json for JSON, "
                    "inspect_sqlite_schema or execute_context_sql for SQLite, and "
                    "read_doc for narrative documents.",
                )
        return None
