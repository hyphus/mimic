"""The application's only SQLite access layer. Not an ORM: callers write SQL."""

from __future__ import annotations

import sqlite3

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Per-call connection: commits on success, rolls back on error, always closes.

    gthread means concurrent readers, so WAL plus a bounded wait beats SQLITE_BUSY.
    """
    # Local import: mimic.settings reads its own table through this module.
    from mimic.settings import load_database_settings

    db = load_database_settings()
    conn = sqlite3.connect(db.path, timeout=db.busy_timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """The returned cursor is closed; only `rowcount` and `lastrowid` remain readable."""
    with connect() as c:
        return c.execute(sql, params)


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS admin (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        password_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        use_iam_role INTEGER NOT NULL DEFAULT 0,
        aws_access_key_id TEXT,
        aws_secret_access_key TEXT,
        target_host TEXT NOT NULL DEFAULT '',
        create_dns_records INTEGER NOT NULL DEFAULT 0,
        create_certificates INTEGER NOT NULL DEFAULT 0
    )
    """,
    # module.json on disk is the source of truth; this table holds nothing else.
    "DROP TABLE IF EXISTS module_config",
    """
    CREATE TABLE IF NOT EXISTS module_overrides (
        module_id TEXT PRIMARY KEY,
        enabled INTEGER,
        path_prefix TEXT,
        host TEXT,
        routes_json TEXT,
        error_pages_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS http_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        method TEXT NOT NULL,
        path TEXT NOT NULL,
        query_string TEXT,
        module_id TEXT,
        request_headers TEXT,
        request_body TEXT,
        response_status INTEGER,
        response_headers TEXT,
        response_body TEXT,
        error TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_http_log_ts ON http_log(ts DESC)",
]


def init_schema() -> None:
    """Every DDL statement in the app, run once from create_app()."""
    with connect() as c:
        for statement in SCHEMA:
            c.execute(statement)
        # CREATE TABLE IF NOT EXISTS leaves an existing table untouched, so a
        # database predating a column must have it added explicitly.
        _ensure_column(c, "module_overrides", "error_pages_json", "TEXT")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    """Add `column` to `table` if a pre-existing database does not have it yet."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
