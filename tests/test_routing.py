from __future__ import annotations

import json

import pytest

from mimic.dispatch import PrefixDispatcherMiddleware


def _stub(name, seen):
    def app(environ, start_response):
        seen.append(name)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [name.encode()]

    return app


def test_dispatch_falls_through_to_main_app():
    seen: list[str] = []
    mw = PrefixDispatcherMiddleware(_stub("main", seen), lambda: ())
    mw({"PATH_INFO": "/healthz"}, lambda *a: None)
    assert seen == ["main"]


def test_dispatch_picks_first_matching_pair():
    seen: list[str] = []
    pairs = (("/v2/sub", _stub("sub", seen), 4), ("/v2", _stub("v2", seen), 4))
    mw = PrefixDispatcherMiddleware(_stub("main", seen), lambda: pairs)
    mw({"PATH_INFO": "/v2/sub/thing"}, lambda *a: None)
    mw({"PATH_INFO": "/v2/other"}, lambda *a: None)
    assert seen == ["sub", "v2"]


def test_registry_orders_pairs_longest_prefix_first(app):
    registry = app.extensions["mimic_module_registry"]
    lengths = [len(prefix.rstrip("/")) for prefix, _, _ in registry.get_pairs()]
    assert lengths == sorted(lengths, reverse=True)


@pytest.mark.parametrize("path", ["/v2/", "/v2/_catalog", "/httpbin/get"])
def test_path_mounted_modules_serve(client, path):
    assert client.get(path).status_code == 200


def test_host_scoped_module_serves(fresh_client):
    client = _override(fresh_client, "webhook", host="example.com")
    resp = client.post("/webhook/github/push", base_url="http://example.com/")
    assert resp.status_code == 200


def test_host_scoped_module_rejects_other_hosts(fresh_client):
    client = _override(fresh_client, "webhook", host="example.com")
    resp = client.post("/webhook/github/push", base_url="http://localhost/")
    assert resp.status_code == 404


def test_registry_root_carries_api_version_header(client):
    resp = client.get("/v2/")
    assert resp.headers.get("Docker-Distribution-Api-Version") == "registry/2.0"


def test_httpbin_uuid(client):
    resp = client.get("/httpbin/uuid")
    assert resp.status_code == 200
    assert "uuid" in resp.get_json()


def _override(fresh_client, module_id, *, path_prefix=None, host=None, routes=None):
    from mimic.modules.registry import list_modules, save_module_overrides

    entry = list_modules()[module_id]
    save_module_overrides(
        module_id,
        path_prefix=path_prefix if path_prefix is not None else entry["path_prefix"],
        host=host if host is not None else (entry["host"] or ""),
        routes=routes if routes is not None else json.loads(entry["routes_json"]),
        error_pages=json.loads(entry["error_pages_json"]),
    )
    return fresh_client()


def test_route_without_responses_falls_back_to_default(fresh_client):
    from mimic.modules.registry import list_modules

    routes = json.loads(list_modules()["docker_registry"]["routes_json"])
    routes.append({"uri": "bare"})

    client = _override(fresh_client, "docker_registry", routes=routes)
    assert client.get("/v2/bare").status_code == 200


def test_module_serves_from_changed_path_prefix(fresh_client):
    client = _override(fresh_client, "docker_registry", path_prefix="/registry/")
    assert client.get("/registry/_catalog").status_code == 200
    assert client.get("/v2/_catalog").status_code == 404


def test_httpbin_delay_is_clamped(client, monkeypatch):
    from mimic.modules.httpbin import httpbin

    slept: list[float] = []
    monkeypatch.setattr(httpbin.time, "sleep", slept.append)

    assert client.get("/httpbin/delay/3600").status_code == 200
    assert slept == [httpbin.MAX_DELAY_SECONDS]
