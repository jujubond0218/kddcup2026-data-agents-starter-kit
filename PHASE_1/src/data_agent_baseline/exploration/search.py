from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from data_agent_baseline.benchmark.schema import PublicTask

TEXT_SUFFIXES = {".csv", ".tsv", ".json", ".md", ".markdown", ".txt"}
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
MAX_PATTERN_CHARS = 200
MAX_LINE_CHARS = 500
MAX_SQLITE_TABLES = 8
MAX_SQLITE_COLUMNS = 32
MAX_SQLITE_ROWS_PER_TABLE = 200


def _safe_candidates(task: PublicTask, *, path_filter: str | None) -> list[tuple[str, Path]]:
    root = task.context_dir.resolve()
    candidates: list[tuple[str, Path]] = []
    for path in sorted(task.context_dir.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        try:
            path.resolve().relative_to(root)
        except ValueError:
            continue
        relative = path.relative_to(task.context_dir).as_posix()
        if path_filter is not None:
            normalized_filter = path_filter.rstrip("/")
            if relative != normalized_filter and not relative.startswith(f"{normalized_filter}/"):
                continue
        if path.suffix.casefold() in TEXT_SUFFIXES | SQLITE_SUFFIXES:
            candidates.append((relative, path))
    return candidates


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ValueError(f"Regex pattern exceeds {MAX_PATTERN_CHARS} characters.")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern: {exc}") from exc


def _grep_text(
    path: Path,
    relative_path: str,
    pattern: re.Pattern[str],
    *,
    byte_limit: int,
    result_limit: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    with path.open("rb") as stream:
        payload = stream.read(byte_limit)
    text = payload.decode("utf-8", errors="replace")
    matches: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if pattern.search(line):
            matches.append(
                {
                    "path": relative_path,
                    "line": line_number,
                    "text": line[:MAX_LINE_CHARS],
                }
            )
            if len(matches) >= result_limit:
                break
    return matches, len(payload), path.stat().st_size > len(payload)


def _grep_sqlite(
    path: Path,
    relative_path: str,
    pattern: re.Pattern[str],
    *,
    result_limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    matches: list[dict[str, Any]] = []
    truncated = False
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if len(tables) > MAX_SQLITE_TABLES:
            truncated = True
        for (table_name,) in tables[:MAX_SQLITE_TABLES]:
            quoted_table = str(table_name).replace('"', '""')
            columns = connection.execute(f'PRAGMA table_info("{quoted_table}")').fetchall()
            if len(columns) > MAX_SQLITE_COLUMNS:
                truncated = True
            column_names = [str(row[1]) for row in columns[:MAX_SQLITE_COLUMNS]]
            if not column_names:
                continue
            projection = ", ".join(
                f'"{name.replace(chr(34), chr(34) * 2)}"' for name in column_names
            )
            rows = connection.execute(
                f'SELECT {projection} FROM "{quoted_table}" LIMIT ?',
                (MAX_SQLITE_ROWS_PER_TABLE + 1,),
            ).fetchall()
            if len(rows) > MAX_SQLITE_ROWS_PER_TABLE:
                truncated = True
            for row in rows[:MAX_SQLITE_ROWS_PER_TABLE]:
                for column_name, value in zip(column_names, row, strict=True):
                    if value is None or not pattern.search(str(value)):
                        continue
                    matches.append(
                        {
                            "path": relative_path,
                            "table": str(table_name),
                            "column": column_name,
                            "value": str(value)[:MAX_LINE_CHARS],
                        }
                    )
                    if len(matches) >= result_limit:
                        return matches, True
    return matches, truncated


def grep_context(
    task: PublicTask,
    *,
    pattern: str,
    path_filter: str | None,
    max_results: int,
    max_files: int,
    max_total_read_bytes: int,
    max_single_file_bytes: int,
    max_output_chars: int,
) -> dict[str, Any]:
    compiled = _compile_pattern(pattern)
    candidates = _safe_candidates(task, path_filter=path_filter)
    warnings: list[dict[str, str]] = []
    matches: list[dict[str, Any]] = []
    read_bytes = 0
    searched_files = 0
    truncated = len(candidates) > max_files

    for relative_path, path in candidates[:max_files]:
        if len(matches) >= max_results or read_bytes >= max_total_read_bytes:
            truncated = True
            break
        remaining_results = max_results - len(matches)
        suffix = path.suffix.casefold()
        try:
            if suffix in TEXT_SUFFIXES:
                remaining_bytes = max_total_read_bytes - read_bytes
                byte_limit = min(max_single_file_bytes, remaining_bytes)
                found, consumed, file_truncated = _grep_text(
                    path,
                    relative_path,
                    compiled,
                    byte_limit=byte_limit,
                    result_limit=remaining_results,
                )
                read_bytes += consumed
                truncated = truncated or file_truncated
            else:
                file_size = path.stat().st_size
                remaining_bytes = max_total_read_bytes - read_bytes
                if file_size > max_single_file_bytes or file_size > remaining_bytes:
                    warnings.append(
                        {
                            "code": "GREP_FILE_TOO_LARGE",
                            "path": relative_path,
                            "message": "SQLite file exceeds the remaining bounded grep budget.",
                        }
                    )
                    truncated = True
                    continue
                found, file_truncated = _grep_sqlite(
                    path,
                    relative_path,
                    compiled,
                    result_limit=remaining_results,
                )
                read_bytes += file_size
                truncated = truncated or file_truncated
            matches.extend(found)
            searched_files += 1
        except (OSError, sqlite3.Error) as exc:
            warnings.append(
                {
                    "code": "GREP_FILE_ERROR",
                    "path": relative_path,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )

    result = {
        "pattern": pattern,
        "path_filter": path_filter,
        "files_searched": searched_files,
        "read_bytes": read_bytes,
        "match_count": len(matches),
        "matches": matches,
        "warnings": warnings,
        "truncated": truncated or len(matches) >= max_results,
    }
    while (
        len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))
        > max_output_chars
        and result["matches"]
    ):
        result["matches"].pop()
        result["match_count"] = len(result["matches"])
        result["truncated"] = True
    return result
