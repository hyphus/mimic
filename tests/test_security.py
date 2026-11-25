from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

from helpers import csrf_token
from mimic.httplog.middleware import _is_filtered_route

REPO_ROOT = Path(__file__).resolve().parents[1]


def _login(client):
    return client.post(
        "/admin/login",
        data={"password": "hunter2", "csrf_token": csrf_token(client)},
    )


def test_post_without_csrf_token_is_rejected(client):
    _login(client)
    assert client.post("/admin/logout").status_code == 400


def test_post_with_csrf_token_succeeds(client):
    _login(client)
    resp = client.post("/admin/logout", data={"csrf_token": csrf_token(client)})
    assert resp.status_code == 302


def test_wrong_csrf_token_is_rejected(client):
    _login(client)
    resp = client.post("/admin/logout", data={"csrf_token": "not-the-token"})
    assert resp.status_code == 400


def test_csrf_input_is_rendered_into_the_login_form(client):
    body = client.get("/admin/login").get_data(as_text=True)
    assert 'name="csrf_token"' in body


def test_session_cookie_flags(app):
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_the_admin_session_cookie_ends_with_the_browser_session(client):
    cookies = _login(client).headers.getlist("Set-Cookie")
    session_cookie = next(c for c in cookies if c.startswith("session="))
    assert "Secure" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "Expires" not in session_cookie
    assert "Max-Age" not in session_cookie


def test_oversized_body_is_rejected_by_a_module_sub_app(app, client):
    body = b"x" * (app.config["MAX_CONTENT_LENGTH"] + 1)
    assert client.post("/httpbin/post", data=body).status_code == 413


def test_oversized_body_is_rejected_by_the_main_app(app, client):
    body = b"x" * (app.config["MAX_CONTENT_LENGTH"] + 1)
    assert client.post("/admin/login", data=body).status_code == 413


def test_sqlite_file_is_owner_only(app, data_dir):
    mode = stat.S_IMODE((data_dir / "mimic.sqlite3").stat().st_mode)
    assert mode == 0o600


def test_env_file_is_owner_only(app, data_dir):
    env_file = data_dir / ".env"
    if env_file.exists():
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_existing_world_readable_env_is_repaired_on_boot(data_dir, monkeypatch):
    from mimic.settings import ensure_secret_key

    monkeypatch.delenv("MIMIC_SECRET_KEY", raising=False)
    env_file = data_dir / ".env"
    env_file.write_text("MIMIC_SECRET_KEY='restored-key'\n")
    env_file.chmod(0o644)

    assert ensure_secret_key() == "restored-key"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_world_readable_env_without_the_key_is_tightened_before_writing(data_dir, monkeypatch):
    from mimic.settings import ensure_secret_key

    monkeypatch.delenv("MIMIC_SECRET_KEY", raising=False)
    env_file = data_dir / ".env"
    env_file.write_text("OTHER=1\n")
    env_file.chmod(0o644)

    assert ensure_secret_key()
    assert "MIMIC_SECRET_KEY" in env_file.read_text()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_existing_world_readable_certificate_key_is_repaired_on_boot(data_dir):
    from mimic.tls import cert_paths, ensure_certificate

    ensure_certificate()
    _, key_path = cert_paths()
    key_path.chmod(0o644)

    ensure_certificate()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_acme_account_key_is_owner_only(data_dir):
    from mimic.acme import _get_account_key

    _get_account_key()
    account_key = data_dir / "certs" / "account.key"
    assert stat.S_IMODE(account_key.stat().st_mode) == 0o600


def test_acme_key_writer_matches_the_self_signed_writer(data_dir):
    from cryptography.hazmat.primitives.asymmetric import rsa

    from mimic.acme import _get_account_key
    from mimic.tls import cert_paths, write_private_key

    _get_account_key()
    account_mode = stat.S_IMODE((data_dir / "certs" / "account.key").stat().st_mode)

    _, key_path = cert_paths()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    write_private_key(key_path, key)

    assert stat.S_IMODE(key_path.stat().st_mode) == account_mode == 0o600


def test_existing_world_readable_account_key_is_repaired_on_load(data_dir):
    from mimic.acme import _get_account_key

    _get_account_key()
    account_key = data_dir / "certs" / "account.key"
    account_key.chmod(0o644)

    _get_account_key()
    assert stat.S_IMODE(account_key.stat().st_mode) == 0o600


def test_cert_directory_is_owner_only(data_dir):
    from mimic.acme import _get_account_key

    (data_dir / "certs").mkdir()
    (data_dir / "certs").chmod(0o755)

    _get_account_key()
    assert stat.S_IMODE((data_dir / "certs").stat().st_mode) == 0o700


def test_environment_is_not_mirrored_into_env_file(monkeypatch, data_dir):
    monkeypatch.setenv("MIMIC_LOG_LEVEL", "DEBUG")
    from mimic.app import create_app

    create_app()
    env_file = data_dir / ".env"
    contents = env_file.read_text() if env_file.exists() else ""
    assert "MIMIC_LOG_LEVEL" not in contents


def test_dashboard_log_output_is_escaped():
    js = (REPO_ROOT / "mimic/admin/static/js/dashboard.js").read_text()
    assert "escapeHtml(content)" in js
    assert "${formattedContent}" in js


# --- login redirect --------------------------------------------------------------


@pytest.mark.parametrize(
    "nxt",
    ["https://evil.com", "//evil.com", "/\\evil.com", "/adminx", "/admin.evil.com", "javascript:alert(1)", ""],
)
def test_login_never_redirects_off_the_admin_ui(client, nxt):
    resp = client.post(
        "/admin/login",
        query_string={"next": nxt},
        data={"password": "hunter2", "csrf_token": csrf_token(client)},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/admin/"


@pytest.mark.parametrize("nxt", ["/admin", "/admin/logs?page=2", "/admin/?expand=httpbin"])
def test_login_follows_next_within_the_admin_ui(client, nxt):
    resp = client.post(
        "/admin/login",
        query_string={"next": nxt},
        data={"password": "hunter2", "csrf_token": csrf_token(client)},
    )
    assert resp.headers["Location"] == nxt


def test_the_login_required_redirect_round_trips_through_next(client):
    to_login = client.get("/admin/logs?page=2")
    assert to_login.status_code == 302
    resp = client.post(to_login.headers["Location"], data={"password": "hunter2", "csrf_token": csrf_token(client)})
    assert resp.headers["Location"] == "/admin/logs?page=2"


# --- session hygiene ----------------------------------------------------------------


def _session_token(client) -> str | None:
    with client.session_transaction() as sess:
        return sess.get("csrf_token")


def test_login_rotates_the_csrf_token(client):
    client.get("/admin/login")
    before = _session_token(client)
    assert before

    _login(client)
    client.get("/admin/")
    after = _session_token(client)
    assert after and after != before

    assert client.post("/admin/logout", data={"csrf_token": before}).status_code == 400
    assert client.post("/admin/logout", data={"csrf_token": after}).status_code == 302


@pytest.mark.parametrize(
    ("path", "logged_in"),
    [("/admin/login", False), ("/admin/", True), ("/admin/logs", True), ("/admin/static/css/style.css", False)],
)
def test_admin_pages_cannot_be_framed(client, path, logged_in):
    if logged_in:
        _login(client)
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


@pytest.mark.parametrize(
    ("path", "logged_in"),
    [("/admin/login", False), ("/admin/", True), ("/admin/logs", True), ("/admin/static/css/style.css", False)],
)
def test_admin_pages_carry_standard_security_headers(client, path, logged_in):
    if logged_in:
        _login(client)
    resp = client.get(path)
    assert resp.status_code == 200
    # No HSTS: Mimic serves self-signed certificates by default and may run
    # without TLS behind a proxy that terminates it.
    assert "Strict-Transport-Security" not in resp.headers
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_admin_csp_restricts_scripts_and_objects(client):
    resp = client.get("/admin/login")
    csp = resp.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "default-src 'self'" in csp


def test_admin_templates_have_no_inline_scripts_or_event_handlers():
    # A script-src 'self' CSP only holds if nothing relies on inline script.
    for name in ("base.html", "login.html", "dashboard.html", "logs.html"):
        html = (REPO_ROOT / "mimic/admin/templates/admin" / name).read_text()
        assert "<script>" not in html, name
        assert not re.search(r"\son(click|change|submit|load|input|error)=", html), name


# --- request log filter -------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "filtered"),
    [
        ("/admin", True),
        ("/admin/logs", True),
        ("/healthz", True),
        ("/favicon.ico", True),
        ("/administrator", False),
        ("/admin-api/x", False),
        ("/healthzX", False),
        ("/favicon.icon", False),
    ],
)
def test_only_admin_health_and_favicon_are_left_out_of_the_request_log(path, filtered):
    assert _is_filtered_route(path) is filtered


# --- every admin route, including ones added later -----------------------------------


def _admin_rules(app, method: str):
    adapter = app.url_map.bind("localhost")
    rules = [
        rule
        for rule in app.url_map.iter_rules()
        if rule.endpoint.startswith("admin.") and method in rule.methods
    ]
    return [(rule.endpoint, adapter.build(rule.endpoint, {arg: "httpbin" for arg in rule.arguments})) for rule in rules]


def test_every_admin_page_requires_login(app):
    rules = [
        (endpoint, url)
        for endpoint, url in _admin_rules(app, "GET")
        if endpoint not in ("admin.login", "admin.static")
    ]
    assert len(rules) >= 4
    for endpoint, url in rules:
        resp = app.test_client().get(url)
        assert resp.status_code == 302, endpoint
        assert resp.headers["Location"].startswith("/admin/login"), endpoint


def test_every_admin_post_requires_a_csrf_token(app):
    client = app.test_client()
    _login(client)
    rules = _admin_rules(app, "POST")
    assert len(rules) >= 6
    for endpoint, url in rules:
        assert client.post(url).status_code == 400, endpoint
        assert client.post(url, data={"csrf_token": "wrong"}).status_code == 400, endpoint
