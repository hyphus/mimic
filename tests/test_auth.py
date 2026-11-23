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


def _login_from(client, password, addr):
    return client.post(
        "/admin/login",
        data={"password": password, "csrf_token": csrf_token(client)},
        environ_base={"REMOTE_ADDR": addr},
    )


def test_repeated_failed_logins_are_throttled_and_locked_out(client):
    addr = "203.0.113.7"
    codes = [_login_from(client, f"wrong-{i}", addr).status_code for i in range(10)]
    assert 200 in codes
    assert 429 in codes, codes
    first_lockout = codes.index(429)
    assert first_lockout >= 1

    blocked = _login_from(client, "wrong-again", addr)
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_lockout_refuses_the_correct_password_too(client):
    addr = "203.0.113.7"
    for i in range(10):
        _login_from(client, f"wrong-{i}", addr)
    assert _login_from(client, "hunter2", addr).status_code == 429


def test_throttle_is_per_client(client):
    for i in range(10):
        _login_from(client, f"wrong-{i}", "203.0.113.7")
    assert _login_from(client, "hunter2", "203.0.113.7").status_code == 429
    # A different client address is unaffected.
    assert _login_from(client, "hunter2", "198.51.100.9").status_code == 302


def test_success_below_the_threshold_still_works(client):
    addr = "203.0.113.7"
    assert _login_from(client, "wrong", addr).status_code == 200
    resp = _login_from(client, "hunter2", addr)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/admin/"


def test_success_resets_the_failure_budget(client):
    from mimic import auth

    addr = "203.0.113.7"
    auth.register_login_success(addr)
    for _ in range(auth.LOGIN_MAX_FAILURES - 1):
        auth.register_login_failure(addr)
    assert auth.login_retry_after(addr) == 0
    auth.register_login_success(addr)
    assert auth.login_retry_after(addr) == 0
    # After a reset, the full budget is available again.
    for _ in range(auth.LOGIN_MAX_FAILURES - 1):
        auth.register_login_failure(addr)
    assert auth.login_retry_after(addr) == 0


def test_failures_outside_the_window_are_forgotten():
    from mimic import auth

    addr = "203.0.113.7"
    auth.register_login_success(addr)
    for _ in range(auth.LOGIN_MAX_FAILURES - 1):
        auth.register_login_failure(addr)
    future = 10_000_000.0  # far past LOGIN_FAILURE_WINDOW_SECONDS
    assert auth.login_retry_after(addr, now=future) == 0


def test_lockout_backoff_escalates_across_cycles():
    from mimic import auth

    addr = "203.0.113.7"
    delays = []
    t = 0.0
    for _ in range(4):
        for _ in range(auth.LOGIN_MAX_FAILURES):
            auth.register_login_failure(addr, now=t)
            t += 0.01
        delay = auth.login_retry_after(addr, now=t)
        delays.append(delay)
        t += delay + 0.01  # wait the lockout out before the next cycle

    assert delays[0] > 0
    assert delays[1] > delays[0]
    assert delays[2] > delays[1]


def test_global_cap_stops_relentless_ip_rotation():
    from mimic import auth

    attempts = 0
    locked = False
    while attempts < 500:
        addr = "10.0.%d.%d" % (attempts // 256, attempts % 256)
        if auth.login_retry_after(addr) > 0:
            locked = True
            break
        auth.register_login_failure(addr)
        attempts += 1

    assert locked, "rotating source addresses bypassed the account-wide cap"
    assert attempts <= auth.LOGIN_GLOBAL_MAX_FAILURES
