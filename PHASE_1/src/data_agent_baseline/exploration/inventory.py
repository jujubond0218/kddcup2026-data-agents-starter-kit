from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from data_agent_baseline.benchmark.schema import PublicTask


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
    return (
        {
            "columns": rows[0],
            "sample_rows": rows[1:3],
            "inferred_types": _infer_column_types(rows[0], rows[1:21]),
            "row_count": len(rows) - 1 if not truncated and size <= len(payload) else None,
            "truncated": truncated or size > len(payload),
        },
        len(payload),
        [],
    )


def _scan_json(
    path: Path, size: int, limits: InventoryLimits
) -> tuple[dict[str, Any], int, list[dict[str, str]]]:
    if size > limits.max_single_file_bytes:
        payload, _ = _read_prefix(path, limits.max_single_file_bytes)
        return (
            {"top_level": _text_preview(payload, 1).strip() or "unknown", "truncated": True},
            len(payload),
            [_warning("JSON_PARSE_SKIPPED", "JSON exceeds the bounded parse limit.")],
        )
    payload, _ = _read_prefix(path, size)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ({}, len(payload), [_warning("JSON_PARSE_ERROR", str(exc))])
    if isinstance(value, dict):
        return (
            {"top_level": "object", "keys": sorted(value)[:24], "truncated": False},
            len(payload),
            [],
        )
    if isinstance(value, list):
        sample = value[:2]
        return (
            {"top_level": "array", "item_count": len(value), "sample": sample, "truncated": False},
            len(payload),
            [],
        )
    return ({"top_level": type(value).__name__, "truncated": False}, len(payload), [])


def _scan_sqlite(path: Path) -> tuple[dict[str, Any], int, list[dict[str, str]]]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            names = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            tables = []
            for (name,) in names[:32]:
                escaped_name = str(name).replace('"', '""')
                columns = connection.execute(f'PRAGMA table_info("{escaped_name}")').fetchall()
                tables.append(
                    {
                        "name": name,
                        "columns": [{"name": item[1], "type": item[2]} for item in columns],
                    }
                )
    except sqlite3.Error as exc:
        return ({}, 0, [_warning("SQLITE_PARSE_ERROR", str(exc))])
    return ({"tables": tables, "truncated": len(names) > 32}, 0, [])


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


def _shorten_inventory(report: dict[str, Any], max_chars: int) -> dict[str, Any]:
    def fits(candidate: dict[str, Any]) -> bool:
        return len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) <= max_chars

    if fits(report):
        return report
    shortened = json.loads(json.dumps(report))
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
    shortened["truncated"] = True
    if fits(shortened):
        return shortened
    return {
        "schema_version": 1,
        "status": "partial",
        "files": [],
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
        for bulky_key in ("sample_rows", "sample", "preview", "value_sample"):
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

    report = {
        "schema_version": 1,
        "status": "partial" if truncated else "ok",
        "files": entries,
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
    payload, truncated = _read_prefix(path, min(size, limits.max_single_file_bytes))
    if truncated or size > len(payload):
        return (
            {
                "top_level": _text_preview(payload, 1).strip() or "unknown",
                "preview": _text_preview(payload, 1_600),
                "truncated": True,
            },
            len(payload),
        )
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
