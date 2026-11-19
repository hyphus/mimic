from __future__ import annotations

from helpers import csrf_token


def _login(client):
    return client.post(
        "/admin/login",
        data={"password": "hunter2", "csrf_token": csrf_token(client)},
    )


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_http_exception_json_shape(client):
    resp = client.get("/definitely-not-a-route")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["status_code"] == 404
    assert body["status"] == "Not Found"
    assert "message" in body


def test_admin_requires_login(client):
    resp = client.get("/admin/")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_header_logo_is_rendered_and_served(client):
    _login(client)
    resp = client.get("/admin/")
    assert b'<img src="/admin/static/img/mimic.png"' in resp.data

    img = client.get("/admin/static/img/mimic.png")
    assert img.status_code == 200
    assert img.content_type.startswith("image/png")
