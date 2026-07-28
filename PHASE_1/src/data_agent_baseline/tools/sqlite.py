from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from time import monotonic

READ_ONLY_PRAGMAS = {
    "compile_options",
    "database_list",
    "foreign_key_list",
    "index_info",
    "index_list",
    "table_info",
    "table_xinfo",
}
FORBIDDEN_SQL_TOKENS = {
    "alter",
    "attach",
    "create",
    "delete",
    "detach",
    "drop",
    "insert",
    "reindex",
    "replace",
    "update",
    "vacuum",
}
SQL_TIMEOUT_SECONDS = 5.0
MAX_EXPLORATION_SQL_COLUMNS = 64
MAX_EXPLORATION_SQL_CELL_CHARS = 500


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def inspect_sqlite_schema(path: Path) -> dict[str, object]:
    with _connect_read_only(path) as conn:
        rows = conn.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        tables: list[dict[str, object]] = []
        for name, create_sql in rows:
            tables.append(
                {
                    "name": name,
                    "create_sql": create_sql,
                }
            )
    return {
        "path": str(path),
        "tables": tables,
    }


def execute_read_only_sql(path: Path, sql: str, *, limit: int = 200) -> dict[str, object]:
    normalized_sql = sql.lstrip().lower()
    if not normalized_sql.startswith(("select", "with", "pragma")):
        raise ValueError("Only read-only SQL statements are allowed.")

    with _connect_read_only(path) as conn:
        cursor = conn.execute(sql)
        column_names = [item[0] for item in cursor.description or []]
        rows = cursor.fetchmany(limit + 1)

    truncated = len(rows) > limit
    limited_rows = rows[:limit]
    return {
        "path": str(path),
        "columns": column_names,
        "rows": [list(row) for row in limited_rows],
        "row_count": len(limited_rows),
        "truncated": truncated,
    }


def _bounded_sql_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        rendered = value.hex()
    else:
        rendered = str(value)
    if len(rendered) <= MAX_EXPLORATION_SQL_CELL_CHARS:
        return rendered
    return f"{rendered[: MAX_EXPLORATION_SQL_CELL_CHARS - 1]}…"


def execute_exploration_sql(
    path: Path,
    sql: str,
    *,
    limit: int = 200,
    max_output_chars: int = 12_000,
) -> dict[str, object]:
    normalized_sql = sql.strip()
    if normalized_sql.endswith(";"):
        normalized_sql = normalized_sql[:-1].rstrip()
    if ";" in normalized_sql:
        raise ValueError("Only one read-only SQL statement is allowed.")
    lowered = normalized_sql.casefold()
    if not lowered.startswith(("select", "with", "pragma", "explain")):
        raise ValueError("Only SELECT, WITH, read-only PRAGMA, or EXPLAIN is allowed.")
    tokens = {
        token.casefold()
        for token in normalized_sql.replace("(", " ").replace(")", " ").replace(",", " ").split()
    }
    forbidden = sorted(tokens & FORBIDDEN_SQL_TOKENS)
    if forbidden:
        raise ValueError(f"SQL contains forbidden write operation(s): {forbidden}.")
    if lowered.startswith("pragma"):
        pragma_name = lowered.removeprefix("pragma").strip().split("(", 1)[0].split("=", 1)[0]
        pragma_name = pragma_name.strip().split(".", 1)[-1]
        if "=" in lowered or pragma_name not in READ_ONLY_PRAGMAS:
            raise ValueError(f"PRAGMA {pragma_name or '<missing>'} is not allowed.")
    if not 1 <= limit <= 200:
        raise ValueError("SQL result limit must be between 1 and 200.")

    with _connect_read_only(path) as conn:
        conn.execute("PRAGMA query_only = ON")
        deadline = monotonic() + SQL_TIMEOUT_SECONDS
        conn.set_progress_handler(lambda: int(monotonic() >= deadline), 10_000)
        try:
            cursor = conn.execute(normalized_sql)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).casefold():
                raise ValueError(
                    f"Read-only SQL exceeded {SQL_TIMEOUT_SECONDS:g} seconds."
                ) from exc
            raise
        all_column_names = [item[0] for item in cursor.description or []]
        rows = cursor.fetchmany(limit + 1)

    column_names = all_column_names[:MAX_EXPLORATION_SQL_COLUMNS]
    truncated = len(rows) > limit or len(all_column_names) > len(column_names)
    result: dict[str, object] = {
        "path": str(path),
        "columns": column_names,
        "rows": [],
        "row_count": 0,
        "truncated": False,
    }
    output_rows: list[list[object]] = []
    for row in rows[:limit]:
        bounded_row = [_bounded_sql_value(value) for value in row[: len(column_names)]]
        output_rows.append(bounded_row)
        result["rows"] = output_rows
        result["row_count"] = len(output_rows)
        if (
            len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))
            > max_output_chars
        ):
            output_rows.pop()
            result["row_count"] = len(output_rows)
            truncated = True
            break
    result["truncated"] = truncated
    return result
