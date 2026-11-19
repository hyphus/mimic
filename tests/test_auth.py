from __future__ import annotations

import os

import pytest

from helpers import csrf_token


def _login(client, password):
    return client.post(
        "/admin/login",
        data={"password": password, "csrf_token": csrf_token(client)},
    )


def test_login_succeeds(client):
    resp = _login(client, "hunter2")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/admin/")


def test_wrong_password_rejected(client):
    resp = _login(client, "nope")
    assert resp.status_code == 200
    assert b"Invalid password" in resp.data


def test_missing_password_fails_startup(monkeypatch):
    from mimic.app import create_app

    monkeypatch.delenv("MIMIC_ADMIN_PASSWORD")
    with pytest.raises(RuntimeError):
        create_app()


def test_login_survives_restart(app, monkeypatch):
    from mimic.app import create_app

    assert _login(app.test_client(), "hunter2").status_code == 302

    # Simulate a restart: the bootstrap variable is no longer in the environment.
    monkeypatch.delenv("MIMIC_ADMIN_PASSWORD", raising=False)
    restarted = create_app()
    assert _login(restarted.test_client(), "hunter2").status_code == 302


def test_non_ascii_password_does_not_500(monkeypatch, client):
    from mimic.auth import verify_admin_password

    assert verify_admin_password("pässwörd") is False


def test_bootstrap_password_not_written_to_env_file(app, data_dir):
    env_file = data_dir / ".env"
    contents = env_file.read_text() if env_file.exists() else ""
    assert "MIMIC_ADMIN_PASSWORD" not in contents
    assert os.environ["MIMIC_ADMIN_PASSWORD"] == "hunter2"


@pytest.mark.parametrize("password", ["a$b$c", "pa$$word$", "scrypt:but$not$hex!"])
def test_a_plaintext_password_with_dollar_signs_is_hashed(monkeypatch, password):
    from mimic.app import create_app
    from mimic.auth import _get_password_hash

    monkeypatch.setenv("MIMIC_ADMIN_PASSWORD", password)
    app = create_app()
    assert _get_password_hash() != password
    assert _login(app.test_client(), password).status_code == 302


@pytest.mark.parametrize("method", ["scrypt", "pbkdf2:sha256"])
def test_a_real_werkzeug_hash_is_adopted_as_is(monkeypatch, method):
    from werkzeug.security import generate_password_hash

    from mimic.app import create_app
    from mimic.auth import _get_password_hash

    stored = generate_password_hash("s3cret", method=method)
    monkeypatch.setenv("MIMIC_ADMIN_PASSWORD", stored)
    app = create_app()
    assert _get_password_hash() == stored
    assert _login(app.test_client(), "s3cret").status_code == 302
