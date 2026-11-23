"""Persist HTTP request/response exchanges for the admin activity log."""

from __future__ import annotations

import json
import time

from dataclasses import dataclass
from datetime import datetime, UTC

from mimic.db import execute, query, query_one
from mimic.events import publish
from mimic.settings import (
    HTTP_LOG_MAX_BYTES,
    HTTP_LOG_MAX_ROWS,
    HTTP_LOG_PRUNE_BATCH,
)


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
    req_headers_json = json.dumps(request_headers)
    resp_headers_json = json.dumps(response_headers)
    stored_bytes = (
        len(req_headers_json.encode("utf-8"))
        + len((request_body or "").encode("utf-8"))
        + len(resp_headers_json.encode("utf-8"))
        + len((response_body or "").encode("utf-8"))
    )
    cur = execute(
        """
        INSERT INTO http_log (
            ts, method, path, query_string, module_id,
            request_headers, request_body, response_status, response_headers, response_body, bytes, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts,
            method,
            path,
            query_string,
            module_id,
            req_headers_json,
            request_body,
            response_status,
            resp_headers_json,
            response_body,
            stored_bytes,
            error,
        ),
    )
    rid = int(cur.lastrowid)

    _prune_http_log()

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


def _prune_http_log() -> None:
    """Drop the oldest rows beyond the retention bound, in bounded batches.

    `http_log` is append-only and `insert_log` is reachable from every module
    request, so without this an unauthenticated client could grow the SQLite
    file without limit. The table is kept under both a row cap and a total
    stored-bytes cap (the `bytes` column summed on insert); deletion is
    oldest-first so recent activity survives.
    """
    # Walk newest-first, keeping rows while they fit under both caps. The
    # smallest kept id is the boundary; everything older is dropped. A single
    # DELETE per call keeps the cost bounded even when the table is far over.
    totals = query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS total FROM http_log"
    )
    if totals["n"] <= HTTP_LOG_MAX_ROWS and totals["total"] <= HTTP_LOG_MAX_BYTES:
        return
    boundary = query_one(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (ORDER BY id DESC) AS rownum,
                   SUM(bytes) OVER (ORDER BY id DESC) AS running
            FROM http_log
        )
        SELECT MIN(id) AS keep_id FROM ranked
        WHERE rownum <= ? AND running <= ?
        """,
        (HTTP_LOG_MAX_ROWS, HTTP_LOG_MAX_BYTES),
    )
    keep_id = boundary["keep_id"]
    if keep_id is None:
        # Nothing is within the cap (a single oversized row); clear it out.
        execute(
            "DELETE FROM http_log WHERE id IN "
            "(SELECT id FROM http_log ORDER BY id ASC LIMIT ?)",
            (HTTP_LOG_PRUNE_BATCH,),
        )
        return
    execute(
        "DELETE FROM http_log WHERE id IN "
        "(SELECT id FROM http_log WHERE id < ? ORDER BY id ASC LIMIT ?)",
        (keep_id, HTTP_LOG_PRUNE_BATCH),
    )


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
