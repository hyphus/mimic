"""Modules defining their own error pages (JSON or static file) via the manifest."""

from __future__ import annotations

import json

import pytest

from mimic.modules import registry
from mimic.modules.base import MimicModule
from mimic.modules.registry import (
    list_modules,
    load_module,
    reset_module_overrides,
    save_module_overrides,
)
from mimic.modules.schema import validate


@pytest.fixture
def error_page_module(tmp_path, monkeypatch):
    """Rewrite one module's manifest (with error_pages) and point its file root at tmp."""

    def make(module_id: str = "httpbin", files: dict | None = None, **changes):
        root = tmp_path / "files"
        for name, content in (files or {}).items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes) else content.encode())

        raw = json.loads(registry.manifest_path(module_id).read_text())
        raw.update(changes)
        manifest_copy = tmp_path / f"{module_id}.json"
        manifest_copy.write_text(json.dumps(raw))

        real_manifest_path = registry.manifest_path
        monkeypatch.setattr(
            registry,
            "manifest_path",
            lambda mid: manifest_copy if mid == module_id else real_manifest_path(mid),
        )
        monkeypatch.setattr(MimicModule, "_module_dir", lambda self: root)

    return make


# A single working route so the rest of the prefix 404s deterministically.
_ONE_ROUTE = {"routes": [{"uri": "ok", "responses": [{"response": {"body": {"ok": True}}}]}]}


# --- manifest-defined pages ---------------------------------------------------


def test_a_module_serves_its_json_error_page(fresh_client, error_page_module):
    changes = dict(_ONE_ROUTE)
    changes["error_pages"] = {
        "404": {"body": {"error": "not found here"}, "headers": {"Content-Type": "application/json"}}
    }
    error_page_module(**changes)
    client = fresh_client()

    assert client.get("/httpbin/ok").status_code == 200
    resp = client.get("/httpbin/definitely-missing")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "not found here"}


def test_a_module_serves_a_static_file_error_page(fresh_client, error_page_module):
    changes = dict(_ONE_ROUTE)
    changes["error_pages"] = {"404": {"file": "www/404.html"}}
    error_page_module(files={"www/404.html": "<h1>custom 404</h1>"}, **changes)
    client = fresh_client()

    resp = client.get("/httpbin/definitely-missing")
    assert resp.status_code == 404
    assert resp.data == b"<h1>custom 404</h1>"
    assert resp.headers["Content-Type"] == "text/html; charset=utf-8"


def test_a_module_without_error_pages_keeps_the_default_json(fresh_client, error_page_module):
    error_page_module(**_ONE_ROUTE)
    client = fresh_client()

    resp = client.get("/httpbin/definitely-missing")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["status_code"] == 404
    assert body["status"] == "Not Found"


def test_a_missing_error_page_file_falls_back_to_default_json(fresh_client, error_page_module):
    # No file is written; the manifest points at one that does not exist. The handler
    # must fall back to JSON rather than recurse into another 404.
    changes = dict(_ONE_ROUTE)
    changes["error_pages"] = {"404": {"file": "www/does-not-exist.html"}}
    error_page_module(**changes)
    client = fresh_client()

    resp = client.get("/httpbin/definitely-missing")
    assert resp.status_code == 404
    assert resp.get_json()["status_code"] == 404


def test_the_error_page_status_is_forced_to_the_error_code(fresh_client, error_page_module):
    # A fragment's own `status` must not override the real error code.
    changes = dict(_ONE_ROUTE)
    changes["error_pages"] = {"404": {"status": 200, "body": {"error": "x"}}}
    error_page_module(**changes)
    client = fresh_client()

    resp = client.get("/httpbin/definitely-missing")
    assert resp.status_code == 404


# --- manifest validation ------------------------------------------------------


def test_error_pages_must_be_an_object():
    manifest, errors = validate({"default_prefix": "/x", "error_pages": [1, 2]})
    assert manifest is not None
    assert any("error_pages" in e for e in errors)


def test_a_bad_status_code_key_is_dropped():
    manifest, errors = validate({"default_prefix": "/x", "error_pages": {"nope": {"body": {}}}})
    assert manifest is not None
    assert manifest.error_pages == {}
    assert any("status code" in e for e in errors)


def test_a_non_object_fragment_is_dropped():
    manifest, errors = validate({"default_prefix": "/x", "error_pages": {"404": "oops"}})
    assert manifest is not None
    assert manifest.error_pages == {}
    assert any("response must be an object" in e for e in errors)


def test_a_valid_error_page_is_kept():
    manifest, errors = validate({"default_prefix": "/x", "error_pages": {"404": {"body": {}}}})
    assert manifest is not None
    assert errors == []
    assert manifest.error_pages == {"404": {"body": {}}}


# --- admin overrides ----------------------------------------------------------


def test_a_saved_error_pages_override_is_served(fresh_client):
    entry = list_modules()["docker_registry"]
    save_module_overrides(
        "docker_registry",
        path_prefix=entry["path_prefix"],
        host=entry["host"] or "",
        routes=json.loads(entry["routes_json"]),
        error_pages={"404": {"body": {"overridden": True}, "headers": {"Content-Type": "application/json"}}},
    )
    assert "error_pages" in load_module("docker_registry").overridden

    client = fresh_client()
    resp = client.get("/v2/definitely-missing")
    assert resp.status_code == 404
    assert resp.get_json() == {"overridden": True}


def test_editing_error_pages_back_to_shipped_clears_the_override():
    entry = list_modules()["docker_registry"]
    shipped_pages = json.loads(entry["error_pages_json"])  # {} for docker_registry
    routes = json.loads(entry["routes_json"])

    save_module_overrides(
        "docker_registry",
        path_prefix=entry["path_prefix"],
        host=entry["host"] or "",
        routes=routes,
        error_pages={"404": {"body": {"x": 1}}},
    )
    assert "error_pages" in load_module("docker_registry").overridden

    save_module_overrides(
        "docker_registry",
        path_prefix=entry["path_prefix"],
        host=entry["host"] or "",
        routes=routes,
        error_pages=shipped_pages,
    )
    assert "error_pages" not in load_module("docker_registry").overridden


def test_reset_clears_the_error_pages_override():
    entry = list_modules()["docker_registry"]
    save_module_overrides(
        "docker_registry",
        path_prefix=entry["path_prefix"],
        host=entry["host"] or "",
        routes=json.loads(entry["routes_json"]),
        error_pages={"404": {"body": {"x": 1}}},
    )
    assert "error_pages" in load_module("docker_registry").overridden

    reset_module_overrides("docker_registry")
    assert load_module("docker_registry").overridden == frozenset()
