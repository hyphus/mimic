"""Persist HTTP request/response exchanges for the admin activity log."""

from __future__ import annotations

import json
import time

from dataclasses import dataclass
from datetime import datetime, UTC

from mimic.db import execute, query, query_one
from mimic.events import publish


@dataclass
class HttpLogRow:
    id: int
    ts: float
    method: str
    path: str
    query_string: str | None
    module_id: str | None
    request_headers: dict[str, str]
    request_body: str | None
    response_status: int | None
    response_headers: dict[str, str]
    response_body: str | None
    error: str | None


def _row_to_log(r) -> HttpLogRow:
    return HttpLogRow(
        id=r["id"],
        ts=r["ts"],
        method=r["method"],
        path=r["path"],
        query_string=r["query_string"],
        module_id=r["module_id"],
        request_headers=json.loads(r["request_headers"] or "{}"),
        request_body=r["request_body"],
        response_status=r["response_status"],
        response_headers=json.loads(r["response_headers"] or "{}"),
        response_body=r["response_body"],
        error=r["error"],
    )


def log_row_to_json(row: HttpLogRow) -> dict[str, object]:
    return {
        "id": row.id,
        "ts": row.ts,
        "ts_display": datetime.fromtimestamp(row.ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "method": row.method,
        "path": row.path,
        "query_string": row.query_string,
        "module_id": row.module_id,
        "request_headers": row.request_headers,
        "request_body": row.request_body,
        "response_status": row.response_status,
        "response_headers": row.response_headers,
        "response_body": row.response_body,
        "error": row.error,
    }


def insert_log(
    *,
    method: str,
    path: str,
    query_string: str | None,
    module_id: str | None,
    request_headers: dict[str, str],
    request_body: str | None,
    response_status: int | None,
    response_headers: dict[str, str],
    response_body: str | None,
    error: str | None = None,
) -> int:
    ts = time.time()
    cur = execute(
        """
        INSERT INTO http_log (
            ts, method, path, query_string, module_id,
            request_headers, request_body, response_status, response_headers, response_body, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts,
            method,
            path,
            query_string,
            module_id,
            json.dumps(request_headers),
            request_body,
            response_status,
            json.dumps(response_headers),
            response_body,
            error,
        ),
    )
    rid = int(cur.lastrowid)

    publish("httplog", log_row_to_json(HttpLogRow(
        id=rid,
        ts=ts,
        method=method,
        path=path,
        query_string=query_string,
        module_id=module_id,
        request_headers=request_headers,
        request_body=request_body,
        response_status=response_status,
        response_headers=response_headers,
        response_body=response_body,
        error=error,
    )))
    return rid


_SELECT_COLUMNS = """
    SELECT id, ts, method, path, query_string, module_id,
           request_headers, request_body, response_status, response_headers, response_body, error
    FROM http_log
"""


def fetch_logs(
    *,
    limit: int = 50,
    offset: int = 0,
    module_id: str | None = None,
) -> tuple[list[HttpLogRow], int]:
    """One page of logs, newest first, plus the total matching the same filter."""
    where = "WHERE module_id = ?" if module_id else ""
    scope = (module_id,) if module_id else ()
    total = query_one(f"SELECT COUNT(*) FROM http_log {where}", scope)[0]
    rows = query(
        f"{_SELECT_COLUMNS} {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        scope + (limit, offset),
    )
    return [_row_to_log(r) for r in rows], int(total)


def fetch_logs_after(*, after_id: int, limit: int = 50) -> list[HttpLogRow]:
    """Rows with id strictly greater than after_id, newest first."""
    if after_id < 0:
        after_id = 0
    if limit < 1:
        limit = 1
    rows = query(f"{_SELECT_COLUMNS} WHERE id > ? ORDER BY id DESC LIMIT ?", (after_id, limit))
    return [_row_to_log(r) for r in rows]


def count_logs() -> int:
    return int(query_one("SELECT COUNT(*) FROM http_log")[0])


def clear_logs() -> int:
    n = count_logs()
    execute("DELETE FROM http_log")
    return n
