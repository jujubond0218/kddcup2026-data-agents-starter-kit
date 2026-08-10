import json

import pytest
from pydantic import ValidationError

from data_agent_baseline.agents.evidence_plan import (
    CommitEvidencePlanInput,
    EvidenceCandidate,
    EvidenceItem,
    EvidencePlanController,
    EvidenceSourceField,
    EvidenceVerification,
)


def _tabular_file():
    return {
        "path": "expenses.csv",
        "kind": "tabular",
        "summary": {"columns": ["event_name", "amount"]},
    }


def _sqlite_file():
    return {
        "path": "store.db",
        "kind": "sqlite",
        "summary": {
            "tables": [
                {
                    "name": "sales",
                    "columns": [{"name": "id"}, {"name": "amount"}],
                }
            ]
        },
    }


def _narrative_file():
    return {
        "path": "patient.md",
        "kind": "markdown",
        "summary": {"headings": ["Patient"]},
    }


def _report(*, requirements=None, uncertainties=None, files=None, schema_map=None):
    return {
        "task_requirements": requirements
        if requirements is not None
        else [
            {
                "id": "r_measure",
                "kind": "measure",
                "status": "unresolved",
                "description": "resolve the total",
            }
        ],
        "uncertainties": uncertainties
        if uncertainties is not None
        else [
            {
                "requirement_id": "r_measure",
                "issue": "ambiguous total",
                "candidates": [],
                "evidence_refs": [],
                "verification_hint": "check the amount",
            }
        ],
        "files": files if files is not None else [_tabular_file(), _sqlite_file()],
        "schema_map": schema_map
        if schema_map is not None
        else {
            "expenses.csv": {"columns": ["event_name", "amount"]},
            "store.db": {
                "tables": [
                    {
                        "name": "sales",
                        "columns": [{"name": "id"}, {"name": "amount"}],
                    }
                ]
            },
        },
    }


def _valid_item(requirement_id: str = "r_measure") -> EvidenceItem:
    return EvidenceItem(
        requirement_id=requirement_id,
        candidates=[
            EvidenceCandidate(
                claim="sum the amount over matched events",
                source_fields=[
                    EvidenceSourceField(path="expenses.csv", field="amount"),
                    EvidenceSourceField(path="store.db", table="sales", field="amount"),
                ],
                operation="measure",
            )
        ],
        verifications=[
            EvidenceVerification(
                tool="read_csv",
                path="expenses.csv",
                purpose="Inspect amount values.",
            ),
            EvidenceVerification(
                tool="execute_context_sql",
                path="store.db",
                purpose="Check sales amount.",
            ),
        ],
    )


def _commit(controller, items):
    return controller.commit(None, CommitEvidencePlanInput(items=items))


def _error_code(result):
    assert result.ok is False
    return result.content["error"]["code"]


def test_schema_forbids_extra_fields():
    payload = {
        "items": [
            {
                "requirement_id": "r_measure",
                "candidates": [
                    {
                        "claim": "sum",
                        "source_fields": [{"path": "expenses.csv", "field": "amount"}],
                        "operation": "measure",
                    }
                ],
                "verifications": [{"tool": "read_csv", "path": "expenses.csv", "purpose": "check"}],
                "extra": True,
            }
        ]
    }
    with pytest.raises(ValidationError):
        CommitEvidencePlanInput.model_validate(payload)


def test_schema_enforces_bounds_and_operation_kind():
    with pytest.raises(ValidationError):
        CommitEvidencePlanInput.model_validate({"items": []})
    with pytest.raises(ValidationError):
        CommitEvidencePlanInput.model_validate(
            {
                "items": [
                    {
                        "requirement_id": "r_measure",
                        "candidates": [
                            {
                                "claim": "sum",
                                "source_fields": [{"path": "expenses.csv", "field": "amount"}],
                                "operation": "aggregate",
                            }
                        ],
                        "verifications": [
                            {"tool": "read_csv", "path": "expenses.csv", "purpose": "check"}
                        ],
                    }
                ]
            }
        )


def test_valid_multi_source_plan_commits():
    controller = EvidencePlanController(report=_report())
    result = _commit(controller, [_valid_item()])

    assert result.ok is True
    assert result.content["status"] == "committed"
    assert result.content["item_count"] == 1
    assert result.content["candidate_count"] == 1
    assert result.content["verification_count"] == 2
    assert controller.committed_items is not None
    assert controller.committed_verifications == [
        ("read_csv", "expenses.csv"),
        ("execute_context_sql", "store.db"),
    ]
    assert controller.matches_verification(tool="read_csv", path="expenses.csv") is True
    assert controller.matches_verification(tool="read_doc", path="expenses.csv") is False


def test_coverage_rejects_missing_requirement():
    report = _report()
    report["task_requirements"].append(
        {
            "id": "r_filter",
            "kind": "filter",
            "status": "unresolved",
            "description": "filter the rows",
        }
    )
    controller = EvidencePlanController(report=report)
    result = _commit(controller, [_valid_item()])

    assert _error_code(result) == "EVIDENCE_PLAN_REQUIREMENT_COVERAGE"
    assert "r_filter" in result.content["error"]["message"]


def test_coverage_rejects_duplicate_requirement():
    controller = EvidencePlanController(report=_report())
    result = _commit(controller, [_valid_item(), _valid_item()])

    assert _error_code(result) == "EVIDENCE_PLAN_REQUIREMENT_COVERAGE"


def test_coverage_rejects_unknown_requirement():
    controller = EvidencePlanController(report=_report())
    result = _commit(controller, [_valid_item(requirement_id="not_pending")])

    assert _error_code(result) == "EVIDENCE_PLAN_REQUIREMENT_COVERAGE"


def test_rejects_unknown_source_path():
    controller = EvidencePlanController(report=_report())
    item = _valid_item()
    item.candidates[0].source_fields[0] = EvidenceSourceField(path="missing.csv", field="amount")
    result = _commit(controller, [item])

    assert _error_code(result) == "EVIDENCE_PLAN_INVALID_SOURCE"


def test_rejects_missing_field_on_structured_source():
    controller = EvidencePlanController(report=_report())
    item = _valid_item()
    item.candidates[0].source_fields[0] = EvidenceSourceField(path="expenses.csv")
    result = _commit(controller, [item])

    assert _error_code(result) == "EVIDENCE_PLAN_INVALID_SOURCE"


def test_rejects_fabricated_field_on_structured_source():
    controller = EvidencePlanController(report=_report())
    item = _valid_item()
    item.candidates[0].source_fields[0] = EvidenceSourceField(
        path="expenses.csv", field="does_not_exist"
    )
    result = _commit(controller, [item])

    assert _error_code(result) == "EVIDENCE_PLAN_INVALID_SOURCE"


def test_rejects_fabricated_field_on_narrative_source():
    report = _report(
        files=[_narrative_file()], schema_map={"patient.md": {"headings": ["Patient"]}}
    )
    controller = EvidencePlanController(report=report)
    item = _valid_item()
    item.candidates[0].source_fields = [
        EvidenceSourceField(path="patient.md", field="gender"),
    ]
    item.verifications = [
        EvidenceVerification(tool="read_doc", path="patient.md", purpose="check text")
    ]
    result = _commit(controller, [item])

    assert _error_code(result) == "EVIDENCE_PLAN_INVALID_SOURCE"


def test_narrative_source_allows_path_only():
    report = _report(
        requirements=[
            {
                "id": "r_doc",
                "kind": "other",
                "status": "unresolved",
                "description": "extract from document",
            }
        ],
        uncertainties=[],
        files=[_narrative_file()],
        schema_map={"patient.md": {"headings": ["Patient"]}},
    )
    controller = EvidencePlanController(report=report)
    item = EvidenceItem(
        requirement_id="r_doc",
        candidates=[
            EvidenceCandidate(
                claim="read the patient document",
                source_fields=[EvidenceSourceField(path="patient.md")],
                operation="join",
            )
        ],
        verifications=[
            EvidenceVerification(tool="read_doc", path="patient.md", purpose="check text")
        ],
    )
    result = _commit(controller, [item])

    assert result.ok is True


def test_rejects_verification_path_not_in_candidates():
    controller = EvidencePlanController(report=_report())
    item = _valid_item()
    item.verifications = [
        EvidenceVerification(tool="read_csv", path="expenses.csv", purpose="check")
    ]
    # force the candidate sources to not include the verification path
    item.candidates[0].source_fields = [
        EvidenceSourceField(path="store.db", table="sales", field="amount")
    ]
    result = _commit(controller, [item])

    assert _error_code(result) == "EVIDENCE_PLAN_INVALID_VERIFICATION"


def test_rejects_tool_file_type_mismatch():
    controller = EvidencePlanController(report=_report())
    item = _valid_item()
    item.verifications = [
        EvidenceVerification(tool="read_json", path="store.db", purpose="wrong reader")
    ]
    result = _commit(controller, [item])

    assert _error_code(result) == "EVIDENCE_PLAN_INVALID_VERIFICATION"


def test_rejects_python_verification_tool():
    item = _valid_item()
    item.verifications = [
        EvidenceVerification(tool="execute_context_sql", path="store.db", purpose="check")
    ]
    # execute_context_sql is compatible with sqlite; use a fabricated tool instead
    payload = json.loads(CommitEvidencePlanInput(items=[item]).model_dump_json())
    payload["items"][0]["verifications"][0]["tool"] = "execute_python"
    with pytest.raises(ValidationError):
        CommitEvidencePlanInput.model_validate(payload)


def test_pending_ids_union_from_unresolved_and_uncertainty():
    report = _report()
    report["task_requirements"] = [
        {
            "id": "r_resolved",
            "kind": "measure",
            "status": "resolved",
            "description": "done",
        },
        {
            "id": "r_open",
            "kind": "filter",
            "status": "unresolved",
            "description": "open",
        },
    ]
    report["uncertainties"] = [
        {
            "requirement_id": "r_extra",
            "issue": "extra uncertainty",
            "candidates": [],
            "evidence_refs": [],
            "verification_hint": "check",
        }
    ]
    controller = EvidencePlanController(report=report)

    assert controller.pending_ids == {"r_open", "r_extra"}
