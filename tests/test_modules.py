"""Phase 6: manifests on disk are truth, SQLite holds only overrides."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimic.httplog.store import fetch_logs
from mimic.modules import loader, registry
from mimic.modules.base import MimicModule, ModuleSpec
from mimic.modules.registry import (
    EffectiveModule,
    list_modules,
    load_module,
    reset_module_overrides,
    save_module_overrides,
    set_module_enabled,
)
from mimic.modules.schema import Manifest, resolve_routes, validate
from tests.helpers import csrf_token


def _shipped(module_id: str) -> dict:
    return json.loads(registry.manifest_path(module_id).read_text())


@pytest.fixture
def edited_manifest(tmp_path, monkeypatch):
    """Rewrite one module's module.json somewhere else and point the registry at it."""

    def edit(module_id: str, **changes):
        raw = _shipped(module_id)
        raw.update(changes)
        path = tmp_path / f"{module_id}.json"
        path.write_text(json.dumps(raw))
        real = registry.manifest_path
        monkeypatch.setattr(
            registry,
            "manifest_path",
            lambda mid: path if mid == module_id else real(mid),
        )

    return edit


# --- disk is truth ------------------------------------------------------------


def test_editing_the_manifest_takes_effect_on_restart(fresh_client, edited_manifest):
    edited_manifest("docker_registry", default_prefix="/registry/")
    client = fresh_client()
    assert client.get("/registry/_catalog").status_code == 200
    assert client.get("/v2/_catalog").status_code == 404


def test_a_ui_toggle_survives_a_restart(fresh_client):
    set_module_enabled("docker_registry", False)
    assert fresh_client().get("/v2/_catalog").status_code == 404
    assert load_module("docker_registry").overridden == frozenset({"enabled"})
    assert fresh_client().get("/v2/_catalog").status_code == 404


def test_reset_brings_the_manifest_value_back(fresh_client):
    save_module_overrides(
        "docker_registry",
        path_prefix="/registry/",
        host="",
        routes=json.loads(list_modules()["docker_registry"]["routes_json"]),
        error_pages=json.loads(list_modules()["docker_registry"]["error_pages_json"]),
    )
    assert fresh_client().get("/registry/_catalog").status_code == 200

    reset_module_overrides("docker_registry")
    assert load_module("docker_registry").overridden == frozenset()
    assert fresh_client().get("/v2/_catalog").status_code == 200


def test_editing_a_value_back_to_the_shipped_one_clears_the_override():
    entry = list_modules()["docker_registry"]
    routes = json.loads(entry["routes_json"])
    error_pages = json.loads(entry["error_pages_json"])
    save_module_overrides("docker_registry", path_prefix="/registry/", host="", routes=routes, error_pages=error_pages)
    assert "path_prefix" in load_module("docker_registry").overridden

    save_module_overrides("docker_registry", path_prefix="/v2/", host="", routes=routes, error_pages=error_pages)
    assert load_module("docker_registry").overridden == frozenset()


# --- the module contract ------------------------------------------------------


def _fake_specs(monkeypatch, specs, manifests):
    monkeypatch.setattr(loader, "iter_discovered_specs", lambda: (specs, []))
    monkeypatch.setattr(loader, "load_module", lambda mid: manifests[mid])


def _effective(module_id: str, **raw) -> EffectiveModule:
    manifest, errors = validate({"default_prefix": f"/{module_id}", "default_enabled": True, **raw})
    return EffectiveModule(module_id, manifest, manifest, errors=errors)


class _Custom(MimicModule):
    def build_blueprints(self, url_prefix, manifest):
        return iter(())


def test_manifest_kind_may_not_override_build_blueprints(monkeypatch):
    spec = ModuleSpec(module_id="liar", module=_Custom("liar"))
    _fake_specs(monkeypatch, [spec], {"liar": _effective("liar")})

    result = loader.build_modules()
    assert "liar: declares kind 'manifest' but overrides build_blueprints" in result.errors
    assert result.loaded == []


def test_custom_kind_must_actually_override_build_blueprints(monkeypatch):
    spec = ModuleSpec(module_id="pretend", module=MimicModule("pretend"))
    _fake_specs(monkeypatch, [spec], {"pretend": _effective("pretend", kind="custom")})

    result = loader.build_modules()
    assert "pretend: declares kind 'custom' but inherits the default build_blueprints" in result.errors


def test_custom_kind_may_not_declare_routes():
    manifest, errors = validate(
        {"kind": "custom", "default_prefix": "/x", "routes": [{"uri": "a"}]}
    )
    assert manifest is None
    assert "kind 'custom' must not declare routes" in errors


def test_a_bad_handler_drops_only_its_own_route(monkeypatch):
    spec = ModuleSpec(module_id="hb", module=MimicModule("hb"))
    effective = _effective(
        "hb",
        routes=[{"uri": "fine"}, {"uri": "broken", "handler": "nope"}],
    )
    _fake_specs(monkeypatch, [spec], {"hb": effective})

    result = loader.build_modules()
    assert "hb: route 'broken': handler 'nope' is not a callable method" in result.errors
    assert [m.id for m in result.loaded] == ["hb"]

    resolved, _ = resolve_routes(effective.manifest, spec.module)
    assert [r.uri for r, _ in resolved] == ["fine"]


def test_route_uris_must_be_relative_to_the_prefix():
    manifest, errors = validate({"default_prefix": "/x", "routes": [{"uri": "/absolute"}]})
    assert manifest.routes == []
    assert any("must be relative" in e for e in errors)


def test_manifest_defaults():
    manifest, errors = validate({"default_prefix": "/x"})
    assert errors == []
    assert (manifest.kind, manifest.default_enabled, manifest.host) == ("manifest", False, None)
    assert manifest.max_concurrent == 4


def test_route_methods_fall_back_to_the_manifest_default():
    manifest, _ = validate(
        {"default_prefix": "/x", "default_methods": ["GET", "POST"], "routes": [{"uri": "a"}]}
    )
    assert manifest.routes[0].methods == ["GET", "POST"]


def test_max_concurrent_reaches_the_dispatcher(app, edited_manifest):
    limits = {prefix: limit for prefix, _, limit in app.extensions["mimic_module_registry"].get_pairs()}
    assert limits["/httpbin"] == 4

    edited_manifest("httpbin", max_concurrent=1)
    app.extensions["mimic_module_registry"].refresh()
    limits = {prefix: limit for prefix, _, limit in app.extensions["mimic_module_registry"].get_pairs()}
    assert limits["/httpbin"] == 1


# --- error reporting ----------------------------------------------------------


def test_a_malformed_manifest_is_a_dashboard_error_and_the_rest_still_load(
    app, client, tmp_path, monkeypatch
):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    real = registry.manifest_path
    monkeypatch.setattr(
        registry, "manifest_path", lambda mid: bad if mid == "docker_registry" else real(mid)
    )
    assert app.extensions["mimic_module_registry"].refresh() is True

    client.post("/admin/login", data={"password": "hunter2", "csrf_token": csrf_token(client)})
    page = client.get("/admin/")
    assert page.status_code == 200
    assert b"docker_registry: module.json could not be read" in page.data
    assert client.get("/httpbin/uuid").status_code == 200


def test_a_missing_manifest_is_reported():
    effective = load_module("does_not_exist")
    assert effective.manifest is None
    assert effective.errors == ["module.json is missing"]


# --- httpbin as the hybrid worked example -------------------------------------


@pytest.mark.parametrize(
    "method,path,status",
    [
        ("get", "/httpbin/get", 200),
        ("post", "/httpbin/post", 200),
        ("get", "/httpbin/uuid", 200),
        ("get", "/httpbin/status/418", 418),
        ("get", "/httpbin/", 200),
        ("get", "/httpbin/get?x=1", 200),
        ("get", "/httpbin/nope", 404),
    ],
)
def test_httpbin_endpoints_are_unchanged(client, method, path, status):
    assert getattr(client, method)(path).status_code == status


def test_httpbin_echoes_the_request(client):
    body = client.get("/httpbin/get?a=1").get_json()
    assert body["method"] == "GET"
    assert body["args"] == {"a": ["1"]}


def test_trailing_slash_redirect_is_logged_in_full(client):
    """A 308 to the prefixed base URI must log headers and body, not a 'streamed' error.

    Flask materializes the redirect through run_wsgi_app, so it arrives as a
    streamed response; its HTML page is finite and has to be captured.
    """
    resp = client.get("/httpbin", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["Location"].endswith("/httpbin/")

    rows, total = fetch_logs(limit=50)
    assert total == 1
    row = rows[0]
    assert (row.method, row.path, row.module_id) == ("GET", "/httpbin", "httpbin")
    assert row.response_status == 308
    assert row.error is None
    assert row.request_headers
    assert row.response_headers["Location"].endswith("/httpbin/")
    assert row.response_body


def test_httpbin_routes_are_visible_to_the_admin_editor(client):
    client.post("/admin/login", data={"password": "hunter2", "csrf_token": csrf_token(client)})
    page = client.get("/admin/").get_data(as_text=True)
    assert "delay/&lt;int:seconds&gt;" in page
    assert "uuid" in page


def test_httpbin_is_a_manifest_module_with_handlers():
    entry = list_modules()["httpbin"]
    assert entry["kind"] == "manifest"
    routes = json.loads(entry["routes_json"])
    assert {r["handler"] for r in routes} == {"root", "echo", "status", "delay", "uuid"}


def test_httpbin_routes_take_part_in_conflict_detection(fresh_client, edited_manifest):
    edited_manifest("httpbin", default_prefix="/v2/httpbin")
    client = fresh_client()
    # /v2 was claimed first, so httpbin loses and never mounts.
    assert client.get("/v2/httpbin/uuid").status_code == 404
    assert client.get("/v2/_catalog").status_code == 200


# --- admin plumbing -----------------------------------------------------------


def test_the_admin_editor_saves_and_resets_overrides(client):
    client.post("/admin/login", data={"password": "hunter2", "csrf_token": csrf_token(client)})
    routes = json.loads(list_modules()["webhook"]["routes_json"])

    resp = client.post(
        "/admin/modules/webhook/config",
        data={
            "csrf_token": csrf_token(client),
            "path_prefix": "/hooks/",
            "host": "example.com",
            "routes_json": json.dumps(routes),
        },
    )
    assert resp.status_code == 302
    effective = load_module("webhook")
    assert effective.manifest.default_prefix == "/hooks/"
    assert effective.overridden == frozenset({"path_prefix", "host"})

    resp = client.post(
        "/admin/modules/webhook/reset", data={"csrf_token": csrf_token(client)}
    )
    assert resp.status_code == 302
    assert load_module("webhook").overridden == frozenset()


def test_invalid_routes_json_is_rejected_and_kept_as_a_draft(client):
    client.post("/admin/login", data={"password": "hunter2", "csrf_token": csrf_token(client)})
    resp = client.post(
        "/admin/modules/webhook/config",
        data={
            "csrf_token": csrf_token(client),
            "path_prefix": "/webhook/",
            "host": "",
            "routes_json": "[ not json",
        },
        follow_redirects=True,
    )
    assert b"invalid JSON" in resp.data
    assert b"[ not json" in resp.data
    assert load_module("webhook").overridden == frozenset()


def test_unknown_module_ids_are_refused(client):
    client.post("/admin/login", data={"password": "hunter2", "csrf_token": csrf_token(client)})
    for path in ("/admin/modules/nope/toggle", "/admin/modules/nope/reset"):
        assert client.post(path, data={"csrf_token": csrf_token(client)}).status_code == 302
    assert load_module("nope").manifest is None


def test_manifest_is_typed():
    manifest, errors = validate({"default_prefix": "/x", "max_concurrent": "lots"})
    assert isinstance(manifest, Manifest)
    assert manifest.max_concurrent == 4
    assert "'max_concurrent' must be a int" in errors


# --- security controls ------------------------------------------------------------


def _login(client):
    return client.post("/admin/login", data={"password": "hunter2", "csrf_token": csrf_token(client)})


class _WithHandler(MimicModule):
    def echo(self):
        return "echo"


@pytest.mark.parametrize(
    "name",
    [
        "__init__",
        "__class__",
        "__setattr__",
        "_serve_file",
        "_build_response",
        "build_blueprints",
        "error_response",
    ],
)
def test_private_and_base_class_methods_are_not_handlers(monkeypatch, name):
    spec = ModuleSpec(module_id="hb", module=_WithHandler("hb"))
    effective = _effective("hb", routes=[{"uri": "ok", "handler": "echo"}, {"uri": "bad", "handler": name}])
    _fake_specs(monkeypatch, [spec], {"hb": effective})

    result = loader.build_modules()
    assert f"hb: route 'bad': handler {name!r} is not allowed" in result.errors

    resolved, _ = resolve_routes(effective.manifest, spec.module)
    assert [(r.uri, h.__name__) for r, h in resolved] == [("ok", "echo")]


def test_an_admin_override_cannot_repoint_a_modules_directory(client, fresh_client):
    # The exploit chain from the security review: a route calling __init__ with a
    # captured module_id moved _module_dir() to / and turned the static module
    # into an arbitrary file read.
    from mimic.modules.discovery import specs_by_module_id

    _login(client)
    shipped = json.loads(list_modules()["static"]["routes_json"])
    resp = client.post(
        "/admin/modules/static/config",
        data={
            "csrf_token": csrf_token(client),
            "path_prefix": "/static/",
            "host": "",
            "routes_json": json.dumps(shipped + [{"uri": "pwn/<path:module_id>", "handler": "__init__"}]),
            "error_pages_json": list_modules()["static"]["error_pages_json"],
        },
    )
    assert resp.status_code == 302

    public = fresh_client()
    assert public.get("/static/pwn/../../../../../../../..").status_code == 404
    assert specs_by_module_id()["static"].module.module_id == "static"
    assert public.get("/static/index.html").status_code == 200
    assert public.get("/static/..%2f..%2f..%2f..%2f..%2f..%2f..%2fetc%2fpasswd").status_code == 404


@pytest.mark.parametrize(
    ("prefix", "reserved"),
    [("/admin/", "/admin"), ("/admin/sub/", "/admin"), ("admin", "/admin"), ("/healthz/", "/healthz")],
)
def test_a_module_cannot_mount_over_a_reserved_prefix(edited_manifest, prefix, reserved):
    from mimic.app import create_app

    edited_manifest("httpbin", default_prefix=prefix)
    app = create_app()
    normalized = loader.normalize_prefix(prefix)
    assert (
        f"httpbin: path prefix {normalized!r} overlaps reserved {reserved!r}; not loaded"
        in app.config["MIMIC_MODULE_LOAD"]["errors"]
    )

    client = app.test_client()
    login = client.get("/admin/login")
    assert login.status_code == 200 and b"csrf_token" in login.data
    assert client.get("/healthz").get_json() == {"status": "ok"}


def test_a_prefix_that_only_starts_with_admin_is_allowed_and_logged(fresh_client, edited_manifest):
    edited_manifest("httpbin", default_prefix="/administrator/")
    client = fresh_client()
    assert client.get("/administrator/get").status_code == 200

    rows, _ = fetch_logs(limit=10)
    assert [row.path for row in rows] == ["/administrator/get"]


@pytest.mark.parametrize(
    ("route", "content_type"),
    [
        (
            {"uri": "s/<name>", "responses": [{"response": {"body": "<script>{name}</script>"}}]},
            "text/plain; charset=utf-8",
        ),
        ({"uri": "j", "responses": [{"response": {"body": {"a": 1}}}]}, "application/json"),
        ({"uri": "e", "responses": [{"response": {}}]}, "text/plain; charset=utf-8"),
        (
            {"uri": "h", "responses": [{"response": {"body": "<i>x</i>", "headers": {"Content-Type": "text/html"}}}]},
            "text/html",
        ),
        (
            {"uri": "c", "responses": [{"response": {"body": "a,b", "headers": {"content-type": "text/csv"}}}]},
            "text/csv",
        ),
    ],
)
def test_canned_responses_are_never_html_unless_asked(fresh_client, edited_manifest, route, content_type):
    edited_manifest("webhook", routes=[route])
    uri = route["uri"].replace("<name>", "x")
    resp = fresh_client().get(f"/webhook/{uri}")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == content_type


# --- query matching -----------------------------------------------------------


def _query_route(responses):
    return {"uri": "q", "responses": responses}


def test_query_match_exact_value(fresh_client, edited_manifest):
    route = _query_route(
        [
            {"match": {"query": {"digest": "sha256:abc"}}, "response": {"body": "matched"}},
            {"response": {"body": "fallback"}},
        ]
    )
    edited_manifest("webhook", routes=[route])
    client = fresh_client()
    assert client.get("/webhook/q?digest=sha256:abc").get_data(as_text=True) == "matched"
    assert client.get("/webhook/q?digest=sha256:other").get_data(as_text=True) == "fallback"
    assert client.get("/webhook/q").get_data(as_text=True) == "fallback"


def test_query_match_list_is_any_of(fresh_client, edited_manifest):
    route = _query_route(
        [
            {"match": {"query": {"n": ["1", "2"]}}, "response": {"body": "matched"}},
            {"response": {"body": "fallback"}},
        ]
    )
    edited_manifest("webhook", routes=[route])
    client = fresh_client()
    assert client.get("/webhook/q?n=2").get_data(as_text=True) == "matched"
    assert client.get("/webhook/q?n=3").get_data(as_text=True) == "fallback"


def test_query_wildcard_requires_the_parameter(fresh_client, edited_manifest):
    route = _query_route(
        [
            {"match": {"query": {"digest": "*"}}, "response": {"body": "matched"}},
            {"response": {"body": "fallback"}},
        ]
    )
    edited_manifest("webhook", routes=[route])
    client = fresh_client()
    assert client.get("/webhook/q?digest=anything").get_data(as_text=True) == "matched"
    assert client.get("/webhook/q").get_data(as_text=True) == "fallback"


def test_query_match_requires_all_listed_parameters(fresh_client, edited_manifest):
    route = _query_route(
        [
            {"match": {"query": {"a": "1", "b": "*"}}, "response": {"body": "matched"}},
            {"response": {"body": "fallback"}},
        ]
    )
    edited_manifest("webhook", routes=[route])
    client = fresh_client()
    assert client.get("/webhook/q?a=1&b=x").get_data(as_text=True) == "matched"
    assert client.get("/webhook/q?a=1").get_data(as_text=True) == "fallback"
    assert client.get("/webhook/q?b=x").get_data(as_text=True) == "fallback"


def test_query_match_with_wrong_typed_spec_never_matches(fresh_client, edited_manifest):
    route = _query_route(
        [
            {"match": {"query": {"digest": 42}}, "response": {"body": "matched"}},
            {"response": {"body": "fallback"}},
        ]
    )
    edited_manifest("webhook", routes=[route])
    assert fresh_client().get("/webhook/q?digest=42").get_data(as_text=True) == "fallback"


def test_query_parameters_do_not_match_top_level_keys(fresh_client, edited_manifest):
    route = _query_route(
        [
            {"match": {"digest": "sha256:abc"}, "response": {"body": "matched"}},
            {"response": {"body": "fallback"}},
        ]
    )
    edited_manifest("webhook", routes=[route])
    assert fresh_client().get("/webhook/q?digest=sha256:abc").get_data(as_text=True) == "fallback"


def test_query_matching_does_not_break_templating(fresh_client, edited_manifest):
    route = {
        "uri": "q/<name>",
        "responses": [
            {"match": {"query": {"digest": "*"}}, "response": {"body": "got {name}"}},
        ],
    }
    edited_manifest("webhook", routes=[route])
    assert fresh_client().get("/webhook/q/thing?digest=x").get_data(as_text=True) == "got thing"


def test_shipped_registry_upload_completion_requires_digest(client):
    # The shipped manifest already writes query syntax; before the fix the
    # framework ignored it and every PUT fell through to default_response.
    resp = client.put(
        "/v2/library/ubuntu/blobs/uploads/7d9b3d0d-4b77-4a5b-a564-c4b6a5c7d9e1?digest=sha256:abc"
    )
    assert resp.status_code == 201
    assert "Docker-Content-Digest" in resp.headers

    fallback = client.put("/v2/library/ubuntu/blobs/uploads/7d9b3d0d-4b77-4a5b-a564-c4b6a5c7d9e1")
    assert fallback.status_code == 200


def test_a_json_error_page_without_headers_is_json(fresh_client, edited_manifest):
    edited_manifest("httpbin", error_pages={"404": {"body": {"missing": True}}})
    resp = fresh_client().get("/httpbin/definitely-missing")
    assert resp.status_code == 404
    assert resp.headers["Content-Type"] == "application/json"
    assert resp.get_json() == {"missing": True}


def test_shipped_modules_are_disabled_on_disk():
    # A fresh deployment must expose nothing until an operator opts in. conftest
    # serves enabled copies to every other test, so only this one sees the truth.
    import mimic.modules as modules_pkg

    manifests = sorted(Path(modules_pkg.__file__).parent.glob("*/module.json"))
    assert manifests
    for path in manifests:
        assert json.loads(path.read_text()).get("default_enabled", False) is False, path.parent.name
