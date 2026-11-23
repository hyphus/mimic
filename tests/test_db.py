"""Phase 4: the single SQLite access layer."""

from __future__ import annotations

import sqlite3

import pytest

from mimic import db
from mimic.httplog.store import clear_logs, count_logs, fetch_logs, fetch_logs_after, insert_log
from mimic.settings import (
    AwsSettings,
    FeatureSettings,
    RuntimeSettings,
    database_path,
    load_runtime_settings,
    save_runtime_settings,
)
from tests.helpers import csrf_token


def _insert(module_id: str = "httpbin", status: int = 200) -> int:
    return insert_log(
        method="GET",
        path=f"/{module_id}/get",
        query_string=None,
        module_id=module_id,
        request_headers={},
        request_body=None,
        response_status=status,
        response_headers={},
        response_body="{}",
    )


def test_connect_uses_wal_and_row_access_by_name():
    with db.connect() as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert c.execute("SELECT 1 AS n").fetchone()["n"] == 1


def test_connect_rolls_back_and_closes_on_error():
    with pytest.raises(RuntimeError):
        with db.connect() as c:
            c.execute("INSERT OR REPLACE INTO admin (id, password_hash) VALUES (1, 'x')")
            raise RuntimeError("boom")

    assert db.query_one("SELECT password_hash FROM admin WHERE id = 1") is None

    with db.connect() as c:
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")


def test_init_schema_is_idempotent():
    db.init_schema()
    db.init_schema()
    names = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"admin", "settings", "module_overrides", "http_log"} <= names
    assert "module_config" not in names


def test_execute_exposes_rowcount_and_lastrowid_after_close():
    cur = db.execute("INSERT OR REPLACE INTO admin (id, password_hash) VALUES (1, 'hash')")
    assert cur.rowcount == 1
    assert cur.lastrowid == 1


def test_a_database_written_by_the_old_layer_still_reads(tmp_path, monkeypatch):
    """The refactor changed no schema, so a pre-existing file must open unchanged."""
    old_data = tmp_path / "legacy"
    old_data.mkdir()
    monkeypatch.setenv("MIMIC_DATA_DIR", str(old_data))

    legacy = sqlite3.connect(old_data / "mimic.sqlite3")
    legacy.executescript(
        """
        CREATE TABLE settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            use_iam_role INTEGER NOT NULL DEFAULT 0,
            aws_access_key_id TEXT,
            aws_secret_access_key TEXT,
            target_host TEXT NOT NULL DEFAULT '',
            create_dns_records INTEGER NOT NULL DEFAULT 0,
            create_certificates INTEGER NOT NULL DEFAULT 0,
            use_le_staging INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO settings VALUES (1, 0, 'AKIA', 's3cret', 'example.com', 1, 0, 0);
        """
    )
    legacy.commit()
    legacy.close()

    assert database_path().parent == old_data.resolve()
    assert load_runtime_settings() == RuntimeSettings(
        aws=AwsSettings(
            use_iam_role=False,
            access_key_id="AKIA",
            secret_access_key="s3cret",
            target_host="example.com",
        ),
        features=FeatureSettings(create_dns_records=True),
    )

    # The tables the legacy file is missing are added without disturbing it.
    db.init_schema()
    save_runtime_settings(RuntimeSettings(aws=AwsSettings(target_host="other.example")))
    assert load_runtime_settings().aws.target_host == "other.example"


def test_migration_purges_the_secret_from_a_role_based_deployment(tmp_path, monkeypatch):
    """An older build could leave a static secret behind under an IAM role."""
    old_data = tmp_path / "legacy"
    old_data.mkdir()
    monkeypatch.setenv("MIMIC_DATA_DIR", str(old_data))

    legacy = sqlite3.connect(old_data / "mimic.sqlite3")
    legacy.executescript(
        """
        CREATE TABLE settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            use_iam_role INTEGER NOT NULL DEFAULT 0,
            aws_access_key_id TEXT,
            aws_secret_access_key TEXT,
            target_host TEXT NOT NULL DEFAULT '',
            create_dns_records INTEGER NOT NULL DEFAULT 0,
            create_certificates INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO settings VALUES (1, 1, 'AKIA', 's3cret', 'example.com', 0, 0);
        """
    )
    legacy.commit()
    legacy.close()

    db.init_schema()
    saved = load_runtime_settings().aws
    assert saved.use_iam_role is True
    assert saved.secret_access_key is None
    assert saved.access_key_id is None


def test_migration_keeps_the_secret_for_a_static_key_deployment(tmp_path, monkeypatch):
    """Clearing is role-specific; a static-key deployment keeps its stored secret."""
    old_data = tmp_path / "legacy"
    old_data.mkdir()
    monkeypatch.setenv("MIMIC_DATA_DIR", str(old_data))

    legacy = sqlite3.connect(old_data / "mimic.sqlite3")
    legacy.executescript(
        """
        CREATE TABLE settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            use_iam_role INTEGER NOT NULL DEFAULT 0,
            aws_access_key_id TEXT,
            aws_secret_access_key TEXT,
            target_host TEXT NOT NULL DEFAULT '',
            create_dns_records INTEGER NOT NULL DEFAULT 0,
            create_certificates INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO settings VALUES (1, 0, 'AKIA', 's3cret', 'example.com', 0, 0);
        """
    )
    legacy.commit()
    legacy.close()

    db.init_schema()
    saved = load_runtime_settings().aws
    assert saved.use_iam_role is False
    assert saved.secret_access_key == "s3cret"
    assert saved.access_key_id == "AKIA"


def test_fetch_logs_paginates_and_filters():
    for _ in range(3):
        _insert("httpbin")
    _insert("webhook")

    rows, total = fetch_logs(limit=50, offset=0)
    assert total == 4
    assert len(rows) == 4
    assert [r.id for r in rows] == sorted((r.id for r in rows), reverse=True)

    page1, total = fetch_logs(limit=2, offset=0)
    page2, _ = fetch_logs(limit=2, offset=2)
    assert total == 4
    assert [r.id for r in page1 + page2] == [r.id for r in rows]

    filtered, filtered_total = fetch_logs(limit=50, offset=0, module_id="httpbin")
    assert filtered_total == 3
    assert {r.module_id for r in filtered} == {"httpbin"}


def test_fetch_logs_after_and_clear():
    first = _insert()
    second = _insert()
    assert [r.id for r in fetch_logs_after(after_id=first)] == [second]
    assert fetch_logs_after(after_id=second) == []
    assert clear_logs() == 2
    assert count_logs() == 0


def test_http_log_prunes_to_the_row_cap(monkeypatch):
    """Unauthenticated module traffic must not grow the table without bound."""
    from mimic.httplog import store

    monkeypatch.setattr(store, "HTTP_LOG_MAX_ROWS", 5)
    monkeypatch.setattr(store, "HTTP_LOG_PRUNE_BATCH", 2)
    ids = [_insert("httpbin") for _ in range(12)]

    assert count_logs() == 5
    # The newest rows are the ones kept.
    kept = {r.id for r in fetch_logs(limit=50)[0]}
    assert kept == set(ids[-5:])


def test_http_log_prunes_to_the_byte_cap_and_keeps_recent_rows(monkeypatch):
    from mimic.httplog import store

    monkeypatch.setattr(store, "HTTP_LOG_MAX_ROWS", 10_000)
    monkeypatch.setattr(store, "HTTP_LOG_MAX_BYTES", 5_000)
    monkeypatch.setattr(store, "HTTP_LOG_PRUNE_BATCH", 2)
    for _ in range(10):
        insert_log(
            method="POST",
            path="/httpbin/post",
            query_string=None,
            module_id="httpbin",
            request_headers={},
            request_body="x" * 2_000,
            response_status=200,
            response_headers={},
            response_body="y" * 2_000,
        )

    stored = db.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM http_log")
    assert stored["b"] <= 5_000
    assert stored["n"] >= 1
    # Recent activity survives; the oldest rows are dropped first.
    assert fetch_logs(limit=1)[0][0].path == "/httpbin/post"


def test_log_page_and_resync_endpoint_still_work(client):
    client.post("/admin/login", data={"password": "hunter2", "csrf_token": csrf_token(client)})
    httpbin_id = _insert("httpbin")
    webhook_id = _insert("webhook")

    # Rows are rendered client-side: the page carries the config, not the rows.
    page = client.get("/admin/logs?per=10")
    assert page.status_code == 200
    assert b"log-config" in page.data
    assert b"/webhook/get" not in page.data

    rows = client.get("/admin/logs/rows?per=10").get_json()
    assert rows["total"] == 2
    # Newest first, the order main.js appends them to the table.
    assert [r["id"] for r in rows["rows"]] == [webhook_id, httpbin_id]

    filtered = client.get("/admin/logs/rows?module=webhook").get_json()
    assert filtered["total"] == 1
    assert {r["module_id"] for r in filtered["rows"]} == {"webhook"}

    resync = client.get("/admin/logs/poll?after_id=0")
    body = resync.get_json()
    assert body["total"] == 2
    assert body["rows"][0]["id"] == webhook_id


def test_logs_rows_requires_login(client):
    resp = client.get("/admin/logs/rows")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]
