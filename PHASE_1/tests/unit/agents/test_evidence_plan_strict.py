"""Option B: schema-enforced coverage for commit_evidence_plan.

The controller builds a dynamic strict `items` schema whose required keys are exactly
the pending requirement IDs, so the model can no longer omit or invent an ID. These
tests lock the dynamic schema, the missing/extra-key behavior, the preserved
source/verification validation, and the legacy list-shaped Option A schema."""

import json

from data_agent_baseline.agents.evidence_plan import (
    COVERAGE_ERROR,
    EvidencePlanController,
    INVALID_SOURCE_ERROR,
    INVALID_VERIFICATION_ERROR,
)

_TABULAR_FILES = [
    {"path": "data.csv", "kind": "tabular", "summary": {"columns": ["value", "rate"]}},
]


def _report(*pending_ids: str, files: list[dict] | None = None) -> dict:
    return {
        "task_requirements": [
            {
                "id": rid,
                "kind": "measure",
                "status": "unresolved",
                "description": f"resolve {rid}",
            }
            for rid in pending_ids
        ],
        "answer_projection": {"columns": [], "helper_fields": [], "enforceable": False},
        "recommended_sources": [],
        "files": files if files is not None else _TABULAR_FILES,
        "schema_map": {
            entry["path"]: {"columns": entry["summary"]["columns"]}
            for entry in (files if files is not None else _TABULAR_FILES)
        },
        "knowledge": {"applicable_rules": []},
        "etl_candidates": [],
        "join_paths": [],
        "value_samples": {},
        "uncertainties": [],
        "warnings": [],
    }


class _Call:
    def __init__(self, arguments: dict) -> None:
        self.name = "commit_evidence_plan"
        self.arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))

    @property
    def id(self) -> str:
        return "call_x"


def _execute(controller: EvidencePlanController, arguments: dict):
    registry = controller.registry()
    return registry.execute(registry.specs["commit_evidence_plan"], _Call(arguments))


def _valid_item(*, field: str = "value") -> dict:
    return {
        "candidates": [
            {
                "claim": "sum the value",
                "source_fields": [{"path": "data.csv", "field": field}],
                "operation": "measure",
            }
        ],
        "verifications": [
            {"tool": "read_csv", "path": "data.csv", "purpose": "check values"},
        ],
    }


def _strict_schema(controller: EvidencePlanController) -> dict:
    parameters = controller.tool_spec().to_openai_tool()["function"]["parameters"]
    return parameters["$defs"]["StrictPlanItems"]


def test_strict_schema_keys_and_required_match_pending_ids():
    controller = EvidencePlanController(
        report=_report("req_total", "req_rate"),
        strict_keys=True,
    )
    items_schema = _strict_schema(controller)

    assert sorted(items_schema["properties"]) == ["req_rate", "req_total"]
    assert sorted(items_schema["required"]) == ["req_rate", "req_total"]
    assert items_schema["additionalProperties"] is False


def test_strict_item_has_no_requirement_id_field():
    controller = EvidencePlanController(report=_report("req_total"), strict_keys=True)
    items_schema = _strict_schema(controller)
    item_ref = items_schema["properties"]["req_total"]["$ref"]
    item_schema = controller.tool_spec().to_openai_tool()["function"]["parameters"]["$defs"][
        item_ref.rsplit("/", 1)[-1]
    ]

    assert set(item_schema["properties"]) == {"candidates", "verifications"}
    assert item_schema["additionalProperties"] is False


def test_strict_schema_handles_keyword_and_reserved_ids():
    controller = EvidencePlanController(
        report=_report("if", "model_config"),
        strict_keys=True,
    )
    items_schema = _strict_schema(controller)

    assert sorted(items_schema["properties"]) == ["if", "model_config"]
    assert sorted(items_schema["required"]) == ["if", "model_config"]


def test_strict_missing_key_is_argument_validation_error_not_coverage():
    controller = EvidencePlanController(report=_report("req_total"), strict_keys=True)
    result = _execute(controller, {"items": {}})

    assert result.ok is False
    assert result.recoverable is True
    assert result.error_code == "ARGUMENT_VALIDATION_ERROR"
    message = str(result.content["error"]["message"])
    assert COVERAGE_ERROR not in message
    assert "req_total" in message
    assert controller.committed_items is None


def test_strict_extra_key_is_argument_validation_error():
    controller = EvidencePlanController(report=_report("req_total"), strict_keys=True)
    result = _execute(
        controller,
        {"items": {"req_total": _valid_item(), "bogus_id": _valid_item()}},
    )

    assert result.error_code == "ARGUMENT_VALIDATION_ERROR"
    assert controller.committed_items is None


def test_strict_valid_commit_succeeds_and_recovers_ids():
    controller = EvidencePlanController(
        report=_report("req_rate", "req_total"),
        strict_keys=True,
    )
    result = _execute(
        controller,
        {"items": {"req_total": _valid_item(), "req_rate": _valid_item(field="rate")}},
    )

    assert result.ok is True
    assert result.content["status"] == "committed"
    assert result.content["item_count"] == 2
    assert controller.committed_items is not None
    assert [item["requirement_id"] for item in controller.committed_items] == [
        "req_rate",
        "req_total",
    ]


def test_strict_source_validation_still_applies():
    controller = EvidencePlanController(report=_report("req_total"), strict_keys=True)
    invalid = _valid_item()
    invalid["candidates"][0]["source_fields"] = [{"path": "data.csv", "field": "does_not_exist"}]
    result = _execute(controller, {"items": {"req_total": invalid}})

    assert result.error_code == INVALID_SOURCE_ERROR
    assert controller.committed_items is None


def test_strict_verification_validation_still_applies():
    controller = EvidencePlanController(report=_report("req_total"), strict_keys=True)
    invalid = _valid_item()
    invalid["verifications"] = [{"tool": "read_csv", "path": "other.csv", "purpose": "check"}]
    result = _execute(controller, {"items": {"req_total": invalid}})

    assert result.error_code == INVALID_VERIFICATION_ERROR
    assert controller.committed_items is None


def test_non_strict_controller_keeps_legacy_list_schema():
    controller = EvidencePlanController(report=_report("req_total"), strict_keys=False)
    parameters = controller.tool_spec().to_openai_tool()["function"]["parameters"]

    assert parameters["properties"]["items"]["type"] == "array"
    item_schema = parameters["$defs"]["EvidenceItem"]
    assert "requirement_id" in item_schema["properties"]
    assert COVERAGE_ERROR  # legacy path still defines the coverage error code
