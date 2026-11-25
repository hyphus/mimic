"""Phase 5: settings precedence, purity, and the runtime/startup split."""

from __future__ import annotations

import logging

from dataclasses import FrozenInstanceError

import pytest

from mimic import settings
from mimic.settings import (
    AwsSettings,
    FeatureSettings,
    RuntimeSettings,
    ensure_secret_key,
    http_log_retention,
    load_bind_settings,
    load_logging_settings,
    load_runtime_settings,
    load_server_settings,
    load_settings,
    save_runtime_settings,
    split_bind,
)
from tests.helpers import csrf_token


def _runtime(**over) -> RuntimeSettings:
    aws = dict(
        use_iam_role=False,
        access_key_id="AKIA",
        secret_access_key="s3cret",
        target_host="example.com",
    )
    features = dict(create_dns_records=True, create_certificates=False)
    aws.update(over.pop("aws", {}))
    features.update(over.pop("features", {}))
    return RuntimeSettings(aws=AwsSettings(**aws), features=FeatureSettings(**features))


def test_data_dir_is_created(tmp_path, monkeypatch):
    target = tmp_path / "fresh" / "data"
    monkeypatch.setenv("MIMIC_DATA_DIR", str(target))
    assert settings.data_dir() == target.resolve()
    assert target.is_dir()


def test_process_environment_beats_dotenv(data_dir, monkeypatch):
    (data_dir / ".env").write_text("MIMIC_LOG_LEVEL=DEBUG\n")
    monkeypatch.setenv("MIMIC_LOG_LEVEL", "WARNING")
    assert load_logging_settings().level == "WARNING"


def test_dotenv_beats_the_builtin_default(data_dir, monkeypatch):
    monkeypatch.delenv("MIMIC_LOG_LEVEL", raising=False)
    (data_dir / ".env").write_text("MIMIC_LOG_LEVEL=DEBUG\n")
    assert load_logging_settings().level == "DEBUG"


def test_builtin_default_when_nothing_sets_it(data_dir, monkeypatch):
    monkeypatch.delenv("MIMIC_LOG_LEVEL", raising=False)
    assert load_logging_settings().level == "INFO"


def test_invalid_log_level_warns_and_falls_back(data_dir, monkeypatch, caplog):
    monkeypatch.setenv("MIMIC_LOG_LEVEL", "chatty")
    with caplog.at_level(logging.WARNING, logger="mimic.settings"):
        assert load_logging_settings().level == "INFO"
    assert "chatty" in caplog.text.lower()


def test_loading_never_mutates_the_environment_or_writes_to_disk(data_dir, monkeypatch):
    monkeypatch.delenv("MIMIC_LOG_LEVEL", raising=False)
    env_file = data_dir / ".env"
    env_file.write_text("MIMIC_LOG_LEVEL=DEBUG\n")
    before = env_file.read_text()

    first = load_settings()
    second = load_settings()

    assert first == second
    assert env_file.read_text() == before
    # dotenv values are read, never exported.
    assert "MIMIC_LOG_LEVEL" not in __import__("os").environ


def test_ensure_secret_key_persists_once(data_dir, monkeypatch):
    monkeypatch.delenv("MIMIC_SECRET_KEY", raising=False)
    env_file = data_dir / ".env"

    key = ensure_secret_key()
    assert key
    assert key in env_file.read_text()
    assert env_file.stat().st_mode & 0o777 == 0o600

    # Second call is a read, not a rewrite.
    assert ensure_secret_key() == key
    assert env_file.read_text().count("MIMIC_SECRET_KEY") == 1
    assert load_server_settings().secret_key == key


def test_runtime_settings_default_when_no_row():
    assert load_runtime_settings() == RuntimeSettings()


def test_runtime_settings_round_trip():
    save_runtime_settings(_runtime())
    assert load_runtime_settings() == _runtime()


def test_runtime_settings_upsert_a_single_row():
    save_runtime_settings(_runtime())
    save_runtime_settings(_runtime(aws={"target_host": "other.example.com"}))
    assert load_runtime_settings().aws.target_host == "other.example.com"


def test_target_host_is_lowercased():
    save_runtime_settings(_runtime(aws={"target_host": "EXAMPLE.COM"}))
    assert load_runtime_settings().aws.target_host == "example.com"


@pytest.mark.parametrize("var", ["MIMIC_USE_IAM_ROLE", "MIMIC_TARGET_HOST", "MIMIC_CREATE_DNS_RECORDS"])
def test_runtime_settings_are_not_overridable_from_the_environment(monkeypatch, var):
    save_runtime_settings(_runtime())
    monkeypatch.setenv(var, "true")
    assert load_runtime_settings() == _runtime()


def test_startup_settings_are_frozen():
    with pytest.raises(FrozenInstanceError):
        load_settings().server.data_dir = "/tmp"


def test_admin_form_keeps_the_stored_secret_when_left_blank(client):
    client.post("/admin/login", data={"password": "hunter2", "csrf_token": csrf_token(client)})
    save_runtime_settings(_runtime())

    resp = client.post(
        "/admin/config",
        data={
            "csrf_token": csrf_token(client),
            "target_host": "example.com",
            "aws_secret_access_key": "",
        },
    )
    assert resp.status_code == 302
    assert load_runtime_settings().aws.secret_access_key == "s3cret"


def test_switching_to_iam_role_clears_the_stored_secret(client, monkeypatch):
    monkeypatch.setattr(
        "mimic.admin.routes.validate_aws_credentials", lambda **_: True
    )
    client.post("/admin/login", data={"password": "hunter2", "csrf_token": csrf_token(client)})
    save_runtime_settings(_runtime())

    resp = client.post(
        "/admin/config",
        data={
            "csrf_token": csrf_token(client),
            "use_iam_role": "on",
            "target_host": "example.com",
            "aws_secret_access_key": "",
        },
    )
    assert resp.status_code == 302
    saved = load_runtime_settings().aws
    assert saved.use_iam_role is True
    assert saved.secret_access_key is None
    assert saved.access_key_id is None


def test_save_runtime_settings_drops_the_secret_under_iam_role():
    save_runtime_settings(_runtime())
    save_runtime_settings(_runtime(aws={"use_iam_role": True, "access_key_id": None}))
    saved = load_runtime_settings().aws
    assert saved.use_iam_role is True
    assert saved.secret_access_key is None
    assert saved.access_key_id is None


def test_le_staging_is_driven_by_the_environment(monkeypatch):
    assert settings.le_staging() is False
    monkeypatch.setenv("MIMIC_USE_LE_STAGING", "1")
    assert settings.le_staging() is True


def test_http_log_retention_defaults_are_generous():
    rows, max_bytes = http_log_retention()
    assert rows == 1_000_000
    assert max_bytes == 4 * 1024 ** 3


def test_http_log_retention_reads_the_environment(monkeypatch):
    monkeypatch.setenv("MIMIC_HTTP_LOG_MAX_ROWS", "50")
    monkeypatch.setenv("MIMIC_HTTP_LOG_MAX_BYTES", str(64 * 1024))
    assert http_log_retention() == (50, 64 * 1024)


def test_http_log_retention_falls_back_on_invalid_values(monkeypatch):
    monkeypatch.setenv("MIMIC_HTTP_LOG_MAX_ROWS", "not-a-number")
    monkeypatch.setenv("MIMIC_HTTP_LOG_MAX_BYTES", "-5")
    rows, max_bytes = http_log_retention()
    assert rows == 1_000_000
    assert max_bytes == 4 * 1024 ** 3


def test_http_log_retention_is_cached_until_reset(monkeypatch):
    monkeypatch.setenv("MIMIC_HTTP_LOG_MAX_ROWS", "5")
    http_log_retention()
    # A later environment change must not leak into a running process.
    monkeypatch.setenv("MIMIC_HTTP_LOG_MAX_ROWS", "7")
    assert http_log_retention()[0] == 5
    settings.reset_http_log_retention_cache()
    assert http_log_retention()[0] == 7


def test_bind_settings_default_to_a_single_public_socket(data_dir):
    binds = load_bind_settings()
    assert binds.public == "127.0.0.1:8080"
    assert binds.admin is None


def test_bind_settings_read_the_environment(data_dir, monkeypatch):
    monkeypatch.setenv("MIMIC_PUBLIC_BIND", "0.0.0.0:9000")
    monkeypatch.setenv("MIMIC_ADMIN_BIND", "127.0.0.1:9001")
    binds = load_bind_settings()
    assert (binds.public, binds.admin) == ("0.0.0.0:9000", "127.0.0.1:9001")


def test_split_bind_separates_host_and_port():
    assert split_bind("127.0.0.1:8080") == ("127.0.0.1", 8080)
    assert split_bind("[::1]:8001") == ("[::1]", 8001)
    assert split_bind("9000") == ("0.0.0.0", 9000)
