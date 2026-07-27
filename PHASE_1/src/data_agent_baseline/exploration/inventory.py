from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import ijson
from ijson.common import IncompleteJSONError, ObjectBuilder

from data_agent_baseline.benchmark.schema import PublicTask

MAX_PROFILE_FIELDS = 32
MAX_PROFILE_VALUES = 20
MAX_JSON_FIELD_PATHS = 48
MAX_JSON_SAMPLES = 3
MAX_SQLITE_TABLES = 8
MAX_RELATION_CANDIDATES = 12
MAX_EXPLORATION_PATHS = 8


@dataclass(frozen=True, slots=True)
class InventoryLimits:
    max_files: int
    max_inventory_chars: int
    max_total_read_bytes: int
    max_single_file_bytes: int
    max_pdf_bytes: int
    max_pdf_pages: int


def _warning(code: str, message: str, path: str | None = None) -> dict[str, str]:
    warning = {"code": code, "message": message}
    if path is not None:
        warning["path"] = path
    return warning


def _file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        return "tabular"
    if suffix == ".json":
        return "json"
    if suffix in {".sqlite", ".db", ".sqlite3"}:
        return "sqlite"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".txt", ".text", ".log"}:
        return "text"
    return "unsupported"


def _read_prefix(path: Path, limit: int) -> tuple[bytes, bool]:
    with path.open("rb") as handle:
        payload = handle.read(limit + 1)
    return payload[:limit], len(payload) > limit


def _text_preview(payload: bytes, max_chars: int = 400) -> str:
    return payload.decode("utf-8", errors="replace")[:max_chars]


def _infer_scalar_type(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return "null"
    try:
        int(normalized)
    except ValueError:
        pass
    else:
        return "integer"
    try:
        float(normalized)
    except ValueError:
        return "string"
    return "number"


def _infer_column_types(columns: list[str], rows: list[list[str]]) -> dict[str, list[str]]:
    inferred: dict[str, set[str]] = {column: set() for column in columns}
    for row in rows[:20]:
        for index, column in enumerate(columns):
            if index < len(row):
                inferred[column].add(_infer_scalar_type(row[index]))
    return {column: sorted(types) for column, types in inferred.items()}


def _normalize_sample(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    normalized = str(value).strip().casefold()
    if not normalized or len(normalized) > 200:
        return None
    return normalized


def _bounded_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 3:
        return {"truncated": True, "type": type(value).__name__}
    if isinstance(value, dict):
        keys = list(value)[:8]
        bounded = {str(key): _bounded_json_value(value[key], depth=depth + 1) for key in keys}
        if len(value) > len(keys):
            bounded["_truncated"] = True
        return bounded
    if isinstance(value, list):
        bounded_items = [_bounded_json_value(item, depth=depth + 1) for item in value[:3]]
        if len(value) > len(bounded_items):
            bounded_items.append({"_truncated": True})
        return bounded_items
    if isinstance(value, str):
        return value[:200]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:200]


def _profile(
    *,
    field: str,
    types: list[str],
    values: list[Any],
    table: str | None = None,
) -> dict[str, Any]:
    normalized_values = []
    for value in values:
        normalized = _normalize_sample(value)
        if normalized is not None and normalized not in normalized_values:
            normalized_values.append(normalized)
        if len(normalized_values) >= MAX_PROFILE_VALUES:
            break
    return {
        "field": field,
        "table": table,
        "types": sorted(set(types)),
        "values": normalized_values,
    }


def _json_scalar_type(event: str, value: Any) -> str:
    if event == "null":
        return "null"
    if event == "boolean":
        return "boolean"
    if event == "number":
        return "integer" if isinstance(value, int) else "number"
    return "string"


def _json_field_path(prefix: str, key: str | None = None) -> str:
    parts = [part for part in prefix.split(".") if part and part != "item"]
    if key:
        parts.append(key)
    return ".".join(parts[:3])


def _flatten_json_value(
    value: Any,
    *,
    prefix: str = "",
    depth: int = 0,
    field_types: dict[str, set[str]] | None = None,
    field_values: dict[str, list[Any]] | None = None,
) -> tuple[dict[str, set[str]], dict[str, list[Any]]]:
    types = field_types if field_types is not None else {}
    values = field_values if field_values is not None else {}
    if depth >= 3:
        return types, values
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, (dict, list)):
                _flatten_json_value(
                    item,
                    prefix=path,
                    depth=depth + 1,
                    field_types=types,
                    field_values=values,
                )
            else:
                event = (
                    "null"
                    if item is None
                    else "boolean"
                    if isinstance(item, bool)
                    else "number"
                    if isinstance(item, (int, float))
                    else "string"
                )
                types.setdefault(path, set()).add(_json_scalar_type(event, item))
                values.setdefault(path, []).append(item)
    elif isinstance(value, list):
        for item in value[:MAX_PROFILE_VALUES]:
            _flatten_json_value(
                item,
                prefix=prefix,
                depth=depth + 1,
                field_types=types,
                field_values=values,
            )
    return types, values


class _BoundedReader:
    def __init__(self, handle: Any, limit: int) -> None:
        self.handle = handle
        self.remaining = limit
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        effective_size = self.remaining if size < 0 else min(size, self.remaining)
        payload = self.handle.read(effective_size)
        self.remaining -= len(payload)
        self.bytes_read += len(payload)
        return payload


def _stream_json_summary(
    path: Path,
    size: int,
    limit: int,
) -> tuple[dict[str, Any], int, list[dict[str, str]]]:
    field_types: dict[str, set[str]] = {}
    field_values: dict[str, list[Any]] = {}
    field_paths: list[str] = []
    samples: list[Any] = []
    top_level = "unknown"
    item_count_observed = 0
    builder: ObjectBuilder | None = None
    builder_depth = 0
    stopped_early = False

    with path.open("rb") as handle:
        reader = _BoundedReader(handle, limit)
        try:
            for prefix, event, value in ijson.parse(reader):
                if top_level == "unknown":
                    top_level = {
                        "start_map": "object",
                        "start_array": "array",
                        "string": "string",
                        "number": "number",
                        "boolean": "boolean",
                        "null": "null",
                    }.get(event, "unknown")

                if event == "map_key":
                    path_value = _json_field_path(prefix, str(value))
                    if path_value and path_value not in field_paths:
                        field_paths.append(path_value)
                elif event in {"string", "number", "boolean", "null"}:
                    path_value = _json_field_path(prefix)
                    if path_value:
                        field_types.setdefault(path_value, set()).add(
                            _json_scalar_type(event, value)
                        )
                        field_values.setdefault(path_value, []).append(value)

                if top_level == "array" and len(samples) < MAX_JSON_SAMPLES:
                    if (
                        builder is None
                        and prefix == "item"
                        and event
                        in {
                            "start_map",
                            "start_array",
                        }
                    ):
                        builder = ObjectBuilder()
                        builder_depth = 0
                    if builder is not None:
                        builder.event(event, value)
                        if event in {"start_map", "start_array"}:
                            builder_depth += 1
                        elif event in {"end_map", "end_array"}:
                            builder_depth -= 1
                            if builder_depth == 0:
                                samples.append(_bounded_json_value(builder.value))
                                item_count_observed += 1
                                builder = None
                    elif prefix == "item" and event in {
                        "string",
                        "number",
                        "boolean",
                        "null",
                    }:
                        samples.append(_bounded_json_value(value))
                        item_count_observed += 1
                elif (
                    top_level == "array"
                    and prefix == "item"
                    and event in {"end_map", "end_array", "string", "number", "boolean", "null"}
                ):
                    item_count_observed += 1

                if (
                    len(field_paths) >= MAX_JSON_FIELD_PATHS
                    and len(samples) >= MAX_JSON_SAMPLES
                    and item_count_observed >= MAX_PROFILE_VALUES
                ):
                    stopped_early = True
                    break
        except (IncompleteJSONError, UnicodeDecodeError) as exc:
            if reader.remaining > 0:
                return (
                    {},
                    reader.bytes_read,
                    [_warning("JSON_PARSE_ERROR", f"{type(exc).__name__}: {exc}")],
                )
        consumed = reader.bytes_read

    profiles = [
        _profile(
            field=field,
            types=sorted(types),
            values=field_values.get(field, []),
        )
        for field, types in list(field_types.items())[:MAX_PROFILE_FIELDS]
    ]
    truncated = size > consumed or stopped_early
    summary = {
        "top_level": top_level,
        "field_paths": field_paths[:MAX_JSON_FIELD_PATHS],
        "inferred_types": {
            field: sorted(types)
            for field, types in list(field_types.items())[:MAX_JSON_FIELD_PATHS]
        },
        "sample_objects": samples[:MAX_JSON_SAMPLES],
        "items_observed": item_count_observed,
        "truncated": truncated,
        "_field_profiles": profiles,
    }
    warnings = (
        [_warning("JSON_STREAM_TRUNCATED", "JSON structure was sampled within the byte budget.")]
        if truncated
        else []
    )
    return summary, consumed, warnings


def _scan_tabular(
    path: Path, size: int, limits: InventoryLimits
) -> tuple[dict[str, Any], int, list[dict[str, str]]]:
    payload, truncated = _read_prefix(path, min(size, limits.max_single_file_bytes))
    text = payload.decode("utf-8", errors="replace")
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return ({"columns": [], "sample_rows": [], "truncated": truncated}, len(payload), [])
    columns = rows[0]
    data_rows = rows[1:21]
    inferred_types = _infer_column_types(columns, data_rows)
    profiles = [
        _profile(
            field=column,
            types=inferred_types.get(column, []),
            values=[row[index] for row in data_rows if index < len(row)],
        )
        for index, column in enumerate(columns[:MAX_PROFILE_FIELDS])
    ]
    return (
        {
            "columns": columns,
            "sample_rows": rows[1:3],
            "inferred_types": inferred_types,
            "row_count": len(rows) - 1 if not truncated and size <= len(payload) else None,
            "truncated": truncated or size > len(payload),
            "_field_profiles": profiles,
        },
        len(payload),
        [],
    )


def _scan_json(
    path: Path, size: int, limits: InventoryLimits
) -> tuple[dict[str, Any], int, list[dict[str, str]]]:
    if size > limits.max_single_file_bytes:
        return _stream_json_summary(
            path,
            size,
            limits.max_single_file_bytes,
        )
    payload, _ = _read_prefix(path, size)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ({}, len(payload), [_warning("JSON_PARSE_ERROR", str(exc))])
    field_types, field_values = _flatten_json_value(value)
    profiles = [
        _profile(
            field=field,
            types=sorted(types),
            values=field_values.get(field, []),
        )
        for field, types in list(field_types.items())[:MAX_PROFILE_FIELDS]
    ]
    summary: dict[str, Any] = {
        "top_level": (
            "object"
            if isinstance(value, dict)
            else "array"
            if isinstance(value, list)
            else type(value).__name__
        ),
        "field_paths": list(field_types)[:MAX_JSON_FIELD_PATHS],
        "inferred_types": {
            field: sorted(types)
            for field, types in list(field_types.items())[:MAX_JSON_FIELD_PATHS]
        },
        "truncated": False,
        "_field_profiles": profiles,
    }
    if isinstance(value, dict):
        summary["keys"] = sorted(value)[:24]
        summary["value_sample"] = {
            key: _bounded_json_value(value[key]) for key in sorted(value)[:3]
        }
    elif isinstance(value, list):
        summary["item_count"] = len(value)
        summary["sample"] = [_bounded_json_value(item) for item in value[:2]]
        summary["sample_objects"] = [_bounded_json_value(item) for item in value[:MAX_JSON_SAMPLES]]
    else:
        summary["value"] = value
    return summary, len(payload), []


def _scan_sqlite(path: Path) -> tuple[dict[str, Any], int, list[dict[str, str]]]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            names = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            tables = []
            profiles: list[dict[str, Any]] = []
            for (name,) in names[:MAX_SQLITE_TABLES]:
                escaped_name = str(name).replace('"', '""')
                columns = connection.execute(f'PRAGMA table_info("{escaped_name}")').fetchall()
                row_count = connection.execute(f'SELECT COUNT(*) FROM "{escaped_name}"').fetchone()[
                    0
                ]
                sample_rows = connection.execute(
                    f'SELECT * FROM "{escaped_name}" LIMIT {MAX_PROFILE_VALUES}'
                ).fetchall()
                rendered_columns = [{"name": item[1], "type": item[2]} for item in columns]
                tables.append(
                    {
                        "name": name,
                        "columns": rendered_columns,
                        "row_count": row_count,
                        "sample_rows": [list(row) for row in sample_rows[:3]],
                    }
                )
                for index, column in enumerate(rendered_columns[:MAX_PROFILE_FIELDS]):
                    profiles.append(
                        _profile(
                            field=str(column["name"]),
                            table=str(name),
                            types=[str(column["type"]).casefold() or "unknown"],
                            values=[row[index] for row in sample_rows if index < len(row)],
                        )
                    )
    except sqlite3.Error as exc:
        return ({}, 0, [_warning("SQLITE_PARSE_ERROR", str(exc))])
    return (
        {
            "tables": tables,
            "truncated": len(names) > MAX_SQLITE_TABLES,
            "_field_profiles": profiles[:MAX_PROFILE_FIELDS],
        },
        0,
        [],
    )


def _scan_markdown_or_text(
    path: Path, size: int, limits: InventoryLimits
) -> tuple[dict[str, Any], int, list[dict[str, str]]]:
    payload, truncated = _read_prefix(path, min(size, limits.max_single_file_bytes))
    text = payload.decode("utf-8", errors="replace")
    headings = [line.lstrip("#").strip() for line in text.splitlines() if line.startswith("#")][:12]
    return (
        {
            "headings": headings,
            "preview": text[:400],
            "truncated": truncated or size > len(payload),
        },
        len(payload),
        [],
    )


def _scan_pdf(
    path: Path,
    size: int,
    limits: InventoryLimits,
    *,
    preview_chars: int = 400,
) -> tuple[dict[str, Any], int, list[dict[str, str]]]:
    if size > limits.max_pdf_bytes:
        return (
            {"truncated": True, "text_extractable": False},
            0,
            [_warning("PDF_PARSE_SKIPPED", "PDF exceeds the bounded parse limit.")],
        )
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = reader.pages[: limits.max_pdf_pages]
        text = "\n".join((page.extract_text() or "") for page in pages).strip()
        title = reader.metadata.title if reader.metadata is not None else None
        return (
            {
                "page_count": len(reader.pages),
                "title": title,
                "text_extractable": bool(text),
                "preview": text[:preview_chars],
                "truncated": len(reader.pages) > limits.max_pdf_pages,
            },
            size,
            [] if text else [_warning("PDF_TEXT_UNAVAILABLE", "No extractable text was found.")],
        )
    except Exception as exc:  # noqa: BLE001
        return ({}, 0, [_warning("PDF_PARSE_ERROR", f"{type(exc).__name__}: {exc}")])


def _scan_file(
    path: Path, size: int, limits: InventoryLimits
) -> tuple[dict[str, Any], int, list[dict[str, str]]]:
    kind = _file_kind(path)
    if kind == "tabular":
        return _scan_tabular(path, size, limits)
    if kind == "json":
        return _scan_json(path, size, limits)
    if kind == "sqlite":
        return _scan_sqlite(path)
    if kind in {"markdown", "text"}:
        return _scan_markdown_or_text(path, size, limits)
    if kind == "pdf":
        return _scan_pdf(path, size, limits)
    return ({}, 0, [_warning("UNSUPPORTED_FILE", "Unsupported file type.")])


def _normalized_field_name(field: str) -> str:
    leaf = field.rsplit(".", 1)[-1]
    return re.sub(r"[^a-z0-9]", "", leaf.casefold())


def _type_family(types: list[str]) -> set[str]:
    families: set[str] = set()
    for raw_type in types:
        normalized = raw_type.casefold()
        if any(token in normalized for token in ("int", "real", "float", "double", "number")):
            families.add("number")
        elif "bool" in normalized:
            families.add("boolean")
        elif "null" in normalized:
            families.add("null")
        else:
            families.add("string")
    return families


def _field_ref(path: str, profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": path,
        "table": profile.get("table"),
        "field": profile["field"],
    }


def _relation_candidates(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    structured_entries = [
        entry for entry in entries if entry.get("kind") in {"tabular", "json", "sqlite"}
    ][:MAX_EXPLORATION_PATHS]
    for left_index, left_entry in enumerate(structured_entries):
        left_profiles = left_entry.get("summary", {}).get("_field_profiles", [])
        for right_entry in structured_entries[left_index + 1 :]:
            right_profiles = right_entry.get("summary", {}).get("_field_profiles", [])
            for left_profile in left_profiles[:MAX_PROFILE_FIELDS]:
                left_name = _normalized_field_name(str(left_profile["field"]))
                left_values = set(left_profile.get("values", []))
                for right_profile in right_profiles[:MAX_PROFILE_FIELDS]:
                    right_name = _normalized_field_name(str(right_profile["field"]))
                    right_values = set(right_profile.get("values", []))
                    name_equal = bool(left_name and left_name == right_name)
                    overlap_count = len(left_values & right_values)
                    type_compatible = bool(
                        _type_family(left_profile.get("types", []))
                        & _type_family(right_profile.get("types", []))
                    )
                    if not name_equal and overlap_count < 2:
                        continue
                    candidate = {
                        "status": "candidate",
                        "left": _field_ref(str(left_entry["path"]), left_profile),
                        "right": _field_ref(str(right_entry["path"]), right_profile),
                        "signals": {
                            "normalized_name_match": name_equal,
                            "type_compatible": type_compatible,
                            "sample_overlap_count": overlap_count,
                            "left_sample_count": len(left_values),
                            "right_sample_count": len(right_values),
                        },
                    }
                    score = (int(overlap_count >= 2), int(name_equal), overlap_count)
                    candidates.append((score, candidate))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in candidates[:MAX_RELATION_CANDIDATES]]


def _question_tokens(question: str) -> set[str]:
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", question)
    return {token for token in re.findall(r"[a-z0-9]+", expanded.casefold()) if len(token) >= 3}


def _field_tokens(field: str) -> set[str]:
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", field)
    return {token for token in re.findall(r"[a-z0-9]+", expanded.casefold()) if len(token) >= 3}


def _build_exploration(
    task: PublicTask,
    entries: list[dict[str, Any]],
    relation_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    structured = [entry for entry in entries if entry.get("kind") in {"tabular", "json", "sqlite"}]
    codes: list[str] = []
    candidate_paths: list[str] = []
    triggered = False

    def add_code(code: str, paths: list[str], *, triggers_exploration: bool = False) -> None:
        nonlocal triggered
        if code not in codes:
            codes.append(code)
        triggered = triggered or triggers_exploration
        for path in paths:
            if path not in candidate_paths and len(candidate_paths) < MAX_EXPLORATION_PATHS:
                candidate_paths.append(path)

    partial_json_paths = [
        str(entry["path"])
        for entry in structured
        if entry.get("kind") == "json"
        and entry.get("summary", {}).get("truncated")
        and entry.get("summary", {}).get("field_paths")
    ]
    if partial_json_paths:
        peer_paths = [
            str(entry["path"]) for entry in structured if entry["path"] not in partial_json_paths
        ]
        add_code(
            "PARTIAL_JSON_STRUCTURE",
            partial_json_paths + peer_paths,
            triggers_exploration=True,
        )

    if relation_candidates:
        relation_paths: list[str] = []
        for candidate in relation_candidates:
            relation_paths.extend([candidate["left"]["path"], candidate["right"]["path"]])
        add_code("MULTI_SOURCE_RELATION_CANDIDATES", relation_paths)

    wide_paths = [
        str(entry["path"])
        for entry in structured
        if len(entry.get("summary", {}).get("columns", [])) >= 20
    ]
    if wide_paths and len(structured) > 1:
        add_code(
            "WIDE_SOURCE_WITH_PEERS",
            wide_paths + [str(entry["path"]) for entry in structured],
            triggers_exploration=True,
        )

    question_tokens = _question_tokens(task.question)
    matched_paths = []
    for entry in structured:
        profiles = entry.get("summary", {}).get("_field_profiles", [])
        if any(_field_tokens(str(profile["field"])) & question_tokens for profile in profiles):
            matched_paths.append(str(entry["path"]))
    if len(matched_paths) >= 2:
        add_code(
            "MULTI_SOURCE_QUESTION_FIELDS",
            matched_paths,
            triggers_exploration=len(relation_candidates) >= 3,
        )

    recommended = bool(triggered and candidate_paths)
    if not recommended:
        candidate_paths = []
    focus = ""
    if recommended:
        focus = (
            "Resolve the flagged source, field, and relationship ambiguities using only "
            "the supplied candidates and evidence; return concrete checks for the main agent."
        )
    return {
        "recommended": recommended,
        "focus": focus,
        "candidate_paths": candidate_paths,
        "ambiguity_codes": codes,
    }


def _remove_private_profiles(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        summary = entry.get("summary")
        if isinstance(summary, dict):
            summary.pop("_field_profiles", None)


def _reconcile_exploration(report: dict[str, Any]) -> None:
    retained_paths = {
        str(entry["path"])
        for entry in report.get("files", [])
        if isinstance(entry, dict) and "path" in entry
    }
    report["relation_candidates"] = [
        candidate
        for candidate in report.get("relation_candidates", [])
        if candidate.get("left", {}).get("path") in retained_paths
        and candidate.get("right", {}).get("path") in retained_paths
    ]
    exploration = report.get("exploration")
    if not isinstance(exploration, dict):
        return
    exploration["candidate_paths"] = [
        path for path in exploration.get("candidate_paths", []) if path in retained_paths
    ]
    if not exploration["candidate_paths"]:
        exploration.update(
            {
                "recommended": False,
                "focus": "",
                "ambiguity_codes": [],
            }
        )


def _shorten_inventory(report: dict[str, Any], max_chars: int) -> dict[str, Any]:
    def fits(candidate: dict[str, Any]) -> bool:
        return len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) <= max_chars

    if fits(report):
        return report
    shortened = json.loads(json.dumps(report))
    for entry in shortened["files"]:
        summary = entry.get("summary")
        if not isinstance(summary, dict):
            continue
        for bulky_key in (
            "sample_rows",
            "sample",
            "sample_objects",
            "preview",
            "value_sample",
        ):
            summary.pop(bulky_key, None)
    while shortened.get("relation_candidates") and not fits(shortened):
        shortened["relation_candidates"].pop()
    for entry in shortened["files"]:
        summary = entry.get("summary")
        if isinstance(summary, dict):
            summary.pop("inferred_types", None)
            summary.pop("field_paths", None)
    if fits(shortened):
        return shortened
    for entry in shortened["files"]:
        entry.pop("summary", None)
    shortened["warnings"].append(
        _warning(
            "INVENTORY_OUTPUT_TRUNCATED",
            "Detailed summaries were removed to fit the output budget.",
        )
    )
    if fits(shortened):
        return shortened
    while shortened["files"] and not fits(shortened):
        shortened["files"].pop()
    _reconcile_exploration(shortened)
    shortened["truncated"] = True
    if fits(shortened):
        return shortened
    return {
        "schema_version": 2,
        "status": "partial",
        "files": [],
        "exploration": {
            "recommended": False,
            "focus": "",
            "candidate_paths": [],
            "ambiguity_codes": [],
        },
        "relation_candidates": [],
        "warnings": [
            _warning("INVENTORY_OUTPUT_TRUNCATED", "Inventory exceeded the output budget.")
        ],
        "budget": {
            "files_scanned": report["budget"]["files_scanned"],
            "read_bytes": report["budget"]["read_bytes"],
        },
        "truncated": True,
    }


def compact_inventory(report: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Return a detached inventory view bounded for repeated prompt inclusion."""

    detached = json.loads(json.dumps(report))
    rendered = json.dumps(detached, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= max_chars:
        return detached
    for entry in detached["files"]:
        summary = entry.get("summary")
        if not isinstance(summary, dict):
            continue
        for bulky_key in (
            "sample_rows",
            "sample",
            "sample_objects",
            "preview",
            "value_sample",
        ):
            summary.pop(bulky_key, None)
    detached["warnings"].append(
        _warning(
            "PROMPT_INVENTORY_COMPACTED",
            "Samples and previews were removed from the prompt view.",
        )
    )
    rendered = json.dumps(detached, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= max_chars:
        return detached
    return _shorten_inventory(detached, max_chars)


def inspect_context(task: PublicTask, limits: InventoryLimits) -> dict[str, Any]:
    root = task.context_dir.resolve()
    files: list[Path] = []
    warnings: list[dict[str, str]] = []
    for candidate in sorted(task.context_dir.rglob("*"), key=lambda item: item.as_posix()):
        if not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except ValueError:
            warnings.append(
                _warning("PATH_ESCAPES_CONTEXT", "Skipped a path outside context.", candidate.name)
            )
            continue
        files.append(candidate)

    truncated = len(files) > limits.max_files
    if truncated:
        warnings.append(_warning("FILE_LIMIT_REACHED", "Some context files were not scanned."))
    read_bytes = 0
    entries: list[dict[str, Any]] = []
    for path in files[: limits.max_files]:
        relative_path = path.relative_to(task.context_dir).as_posix()
        size = path.stat().st_size
        if read_bytes >= limits.max_total_read_bytes:
            warnings.append(
                _warning("READ_BUDGET_REACHED", "No remaining scan byte budget.", relative_path)
            )
            truncated = True
            break
        remaining_bytes = limits.max_total_read_bytes - read_bytes
        file_limits = replace(
            limits,
            max_single_file_bytes=min(limits.max_single_file_bytes, remaining_bytes),
            max_pdf_bytes=min(limits.max_pdf_bytes, remaining_bytes),
        )
        try:
            summary, consumed, file_warnings = _scan_file(path, size, file_limits)
        except OSError as exc:
            summary = {}
            consumed = 0
            file_warnings = [_warning("FILE_READ_ERROR", str(exc))]
        read_bytes += consumed
        entries.append(
            {
                "path": relative_path,
                "kind": _file_kind(path),
                "size_bytes": size,
                "summary": summary,
            }
        )
        for item in file_warnings:
            warnings.append({**item, "path": relative_path})

    relation_candidates = _relation_candidates(entries)
    exploration = _build_exploration(task, entries, relation_candidates)
    _remove_private_profiles(entries)
    report = {
        "schema_version": 2,
        "status": "partial" if truncated else "ok",
        "files": entries,
        "exploration": exploration,
        "relation_candidates": relation_candidates,
        "warnings": warnings,
        "budget": {
            "files_scanned": len(entries),
            "read_bytes": read_bytes,
            "max_files": limits.max_files,
            "max_total_read_bytes": limits.max_total_read_bytes,
        },
        "truncated": truncated,
    }
    return _shorten_inventory(report, limits.max_inventory_chars)


def _preview_tabular(path: Path, size: int, limits: InventoryLimits) -> tuple[dict[str, Any], int]:
    payload, truncated = _read_prefix(path, min(size, limits.max_single_file_bytes))
    text = payload.decode("utf-8", errors="replace")
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return ({"columns": [], "sample_rows": [], "truncated": truncated}, len(payload))
    sample_rows = rows[1:11]
    return (
        {
            "columns": rows[0],
            "inferred_types": _infer_column_types(rows[0], sample_rows),
            "sample_rows": sample_rows,
            "sample_row_count": len(sample_rows),
            "row_count": len(rows) - 1 if not truncated and size <= len(payload) else None,
            "truncated": truncated or size > len(payload),
        },
        len(payload),
    )


def _preview_json(path: Path, size: int, limits: InventoryLimits) -> tuple[dict[str, Any], int]:
    if size > limits.max_single_file_bytes:
        summary, consumed, warnings = _stream_json_summary(
            path,
            size,
            limits.max_single_file_bytes,
        )
        summary.pop("_field_profiles", None)
        if warnings:
            summary["warnings"] = warnings
        return summary, consumed
    payload, _ = _read_prefix(path, min(size, limits.max_single_file_bytes))
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ({"parse_error": str(exc), "truncated": False}, len(payload))
    if isinstance(value, dict):
        keys = sorted(value)
        return (
            {
                "top_level": "object",
                "keys": keys[:48],
                "value_sample": {key: value[key] for key in keys[:5]},
                "truncated": len(keys) > 48,
            },
            len(payload),
        )
    if isinstance(value, list):
        return (
            {
                "top_level": "array",
                "item_count": len(value),
                "sample": value[:5],
                "truncated": False,
            },
            len(payload),
        )
    return ({"top_level": type(value).__name__, "value": value, "truncated": False}, len(payload))


def _preview_sqlite(path: Path) -> dict[str, Any]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        names = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables = []
        for (name,) in names[:8]:
            escaped_name = str(name).replace('"', '""')
            columns = connection.execute(f'PRAGMA table_info("{escaped_name}")').fetchall()
            row_count = connection.execute(f'SELECT COUNT(*) FROM "{escaped_name}"').fetchone()[0]
            sample_rows = connection.execute(f'SELECT * FROM "{escaped_name}" LIMIT 3').fetchall()
            tables.append(
                {
                    "name": name,
                    "columns": [{"name": item[1], "type": item[2]} for item in columns],
                    "row_count": row_count,
                    "sample_rows": [list(row) for row in sample_rows],
                }
            )
    return {"tables": tables, "truncated": len(names) > 8}


def _bound_preview(summary: dict[str, Any], max_chars: int) -> dict[str, Any]:
    rendered = json.dumps(summary, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(rendered) <= max_chars:
        return summary
    bounded = json.loads(json.dumps(summary, ensure_ascii=False, default=str))
    for table in bounded.get("tables", []):
        table.pop("sample_rows", None)
    bounded.pop("sample_rows", None)
    bounded.pop("sample", None)
    bounded.pop("sample_objects", None)
    bounded.pop("value_sample", None)
    bounded["output_truncated"] = True
    rendered = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= max_chars:
        return bounded
    wrapper = {"preview": "", "output_truncated": True}
    available = max(
        0,
        max_chars - len(json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"))),
    )
    wrapper["preview"] = rendered[:available]
    while (
        wrapper["preview"]
        and len(json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"))) > max_chars
    ):
        wrapper["preview"] = wrapper["preview"][:-1]
    return wrapper


def preview_context_file(
    task: PublicTask, relative_path: str, limits: InventoryLimits, max_chars: int
) -> dict[str, Any]:
    candidate = (task.context_dir / relative_path).resolve()
    root = task.context_dir.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path escapes context dir: {relative_path}") from exc
    if not candidate.is_file():
        raise FileNotFoundError(f"Missing context asset: {relative_path}")
    size = candidate.stat().st_size
    kind = _file_kind(candidate)
    warnings: list[dict[str, str]] = []
    try:
        if kind == "tabular":
            summary, _ = _preview_tabular(candidate, size, limits)
        elif kind == "json":
            summary, _ = _preview_json(candidate, size, limits)
        elif kind == "sqlite":
            summary = _preview_sqlite(candidate)
        elif kind in {"markdown", "text"}:
            payload, truncated = _read_prefix(candidate, min(size, limits.max_single_file_bytes))
            text = payload.decode("utf-8", errors="replace")
            summary = {
                "headings": [
                    line.lstrip("#").strip() for line in text.splitlines() if line.startswith("#")
                ][:24],
                "preview": text[:max_chars],
                "truncated": truncated or size > len(payload),
            }
        elif kind == "pdf":
            summary, _, warnings = _scan_pdf(
                candidate,
                size,
                limits,
                preview_chars=max_chars,
            )
        else:
            summary = {}
            warnings = [_warning("UNSUPPORTED_FILE", "Unsupported file type.")]
    except (OSError, sqlite3.Error) as exc:
        summary = {}
        warnings = [_warning("FILE_PREVIEW_ERROR", f"{type(exc).__name__}: {exc}")]
    return {
        "path": relative_path,
        "kind": kind,
        "summary": _bound_preview(summary, max_chars),
        "warnings": warnings,
    }
