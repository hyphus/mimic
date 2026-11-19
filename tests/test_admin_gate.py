"""The port gate that keeps /admin off the public socket and modules off the admin one."""

from __future__ import annotations

import json

import pytest
from werkzeug.test import EnvironBuilder, run_wsgi_app

from mimic.admin_gate import PortGateMiddleware

PUBLIC, ADMIN = 8080, 8443


def _inner_app():
    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    return app


def _request(gate, port, path):
    environ = {"PATH_INFO": path, "SERVER_PORT": str(port), "REQUEST_METHOD": "GET"}
    started: dict = {}

    def start_response(status, headers):
        started["status"] = status

    body = b"".join(gate(environ, start_response))
    return started.get("status", ""), body


def test_admin_is_hidden_on_the_public_port():
    gate = PortGateMiddleware(_inner_app(), PUBLIC, ADMIN)
    for path in ("/admin", "/admin/", "/admin/logs", "/admin/static/css/style.css"):
        status, body = _request(gate, PUBLIC, path)
        assert status.startswith("404")
        assert json.loads(body)["status"] == "Not Found"


def test_everything_else_passes_on_the_public_port():
    gate = PortGateMiddleware(_inner_app(), PUBLIC, ADMIN)
    for path in ("/", "/healthz", "/httpbin/get", "/v2/_catalog"):
        status, body = _request(gate, PUBLIC, path)
        assert (status, body) == ("200 OK", b"ok")


def test_admin_and_healthz_pass_on_the_admin_port():
    gate = PortGateMiddleware(_inner_app(), PUBLIC, ADMIN)
    for path in ("/admin", "/admin/logs", "/healthz"):
        status, body = _request(gate, ADMIN, path)
        assert (status, body) == ("200 OK", b"ok")


def test_modules_are_hidden_on_the_admin_port():
    gate = PortGateMiddleware(_inner_app(), PUBLIC, ADMIN)
    for path in ("/", "/httpbin/get", "/v2/_catalog"):
        status, _ = _request(gate, ADMIN, path)
        assert status.startswith("404")


def test_unknown_ports_are_refused():
    gate = PortGateMiddleware(_inner_app(), PUBLIC, ADMIN)
    status, _ = _request(gate, 9999, "/admin")
    assert status.startswith("404")


def test_leading_slashes_cannot_smuggle_admin_onto_the_public_port():
    gate = PortGateMiddleware(_inner_app(), PUBLIC, ADMIN)
    for path in ("//admin", "//admin/login", "///admin/", "//admin/static/css/style.css"):
        status, _ = _request(gate, PUBLIC, path)
        assert status.startswith("404"), path


# --- the gate in front of the real app --------------------------------------------
#
# The unit tests above feed a stub app. These run requests through load_app(), so the
# gate's idea of "an admin path" is checked against what Flask actually routes.


@pytest.fixture
def gated_app(monkeypatch):
    monkeypatch.setenv("MIMIC_PUBLIC_BIND", f"127.0.0.1:{PUBLIC}")
    monkeypatch.setenv("MIMIC_ADMIN_BIND", f"127.0.0.1:{ADMIN}")
    from mimic.server import load_app

    return load_app()


def _status(app, port: int, path: str) -> int:
    # Built by hand: the test client would read "//admin" as a URL with host "admin".
    environ = EnvironBuilder(path="/").get_environ()
    environ.update(PATH_INFO=path, SERVER_PORT=str(port))
    _, status, _ = run_wsgi_app(app, environ, buffered=True)
    return int(status.split()[0])


@pytest.mark.parametrize("path", ["/admin/login", "//admin/login", "///admin/login", "//admin/", "//admin"])
def test_the_real_admin_ui_is_unreachable_on_the_public_port(gated_app, path):
    assert _status(gated_app, PUBLIC, path) == 404


@pytest.mark.parametrize("path", ["/admin/login", "//admin/login", "/healthz"])
def test_the_real_admin_ui_is_served_on_the_admin_port(gated_app, path):
    assert _status(gated_app, ADMIN, path) == 200


@pytest.mark.parametrize("path", ["/httpbin/get", "//httpbin/get", "/healthz"])
def test_modules_and_healthz_are_served_on_the_public_port(gated_app, path):
    expected = 404 if path.startswith("//") else 200  # "//httpbin" matches no module prefix
    assert _status(gated_app, PUBLIC, path) == expected


@pytest.mark.parametrize("path", ["/httpbin/get", "//httpbin/get", "/"])
def test_modules_are_unreachable_on_the_admin_port(gated_app, path):
    assert _status(gated_app, ADMIN, path) == 404


def test_the_real_app_refuses_unknown_ports(gated_app):
    assert _status(gated_app, 9999, "/admin/login") == 404
    assert _status(gated_app, 9999, "/httpbin/get") == 404


@pytest.mark.parametrize(
    ("public", "admin"),
    [("127.0.0.1:8080", "0.0.0.0:8080"), ("127.0.0.1:0", "127.0.0.1:8443"), ("127.0.0.1:8080", "127.0.0.1:0")],
)
def test_load_app_refuses_binds_the_gate_cannot_tell_apart(monkeypatch, public, admin):
    monkeypatch.setenv("MIMIC_PUBLIC_BIND", public)
    monkeypatch.setenv("MIMIC_ADMIN_BIND", admin)
    from mimic.server import load_app

    with pytest.raises(RuntimeError, match="distinct, non-zero ports"):
        load_app()


def test_load_app_without_an_admin_bind_is_ungated(monkeypatch):
    monkeypatch.setenv("MIMIC_PUBLIC_BIND", f"127.0.0.1:{PUBLIC}")
    from mimic.server import load_app

    assert _status(load_app(), PUBLIC, "/admin/login") == 200
