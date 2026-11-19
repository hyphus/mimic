"""Modules serving files from their own directory: static content and downloads."""

from __future__ import annotations

import json

import pytest
from werkzeug.exceptions import NotFound

from mimic.httplog.store import fetch_logs
from mimic.modules import registry
from mimic.modules.base import MimicModule
from mimic.modules.schema import is_safe_relative_path, validate


@pytest.fixture
def file_module(tmp_path, monkeypatch):
    """Rewrite one module's manifest and point its file root at a tmp directory."""

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


# --- serving ------------------------------------------------------------------


def test_a_route_serves_a_file_from_the_module_directory(fresh_client, file_module):
    file_module(
        files={"index.html": "<h1>hello</h1>"},
        routes=[{"uri": "", "responses": [{"response": {"file": "index.html"}}]}],
    )
    client = fresh_client()
    resp = client.get("/httpbin/")
    assert resp.status_code == 200
    assert resp.data == b"<h1>hello</h1>"
    assert resp.headers["Content-Type"] == "text/html; charset=utf-8"


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("page.html", "text/html; charset=utf-8"),
        ("app.js", "application/javascript; charset=utf-8"),
        ("data.json", "application/json"),
        ("style.css", "text/css; charset=utf-8"),
        ("notes.txt", "text/plain; charset=utf-8"),
        ("blob.bin", "application/octet-stream"),
    ],
)
def test_content_types_are_guessed_from_the_extension(fresh_client, file_module, name, content_type):
    file_module(
        files={f"www/{name}": "x"},
        routes=[{"uri": "<path:wildcard>", "responses": [{"response": {"file": "www/{wildcard}"}}]}],
    )
    client = fresh_client()
    resp = client.get(f"/httpbin/{name}")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == content_type


def test_an_explicit_content_type_header_wins_for_extensionless_files(fresh_client, file_module):
    """A `match` entry can force a type on files whose extension would guess octet-stream."""
    file_module(
        files={"www/login": "<h1>sign in</h1>", "www/blob.bin": b"\x89PNG"},
        routes=[
            {
                "uri": "<path:wildcard>",
                "responses": [
                    {
                        "match": {"wildcard": ["login"]},
                        "response": {
                            "file": "www/{wildcard}",
                            "headers": {"Content-Type": "text/html; charset=utf-8"},
                        },
                    },
                    {"response": {"file": "www/{wildcard}"}},
                ],
            }
        ],
    )
    client = fresh_client()
    resp = client.get("/httpbin/login")
    assert resp.status_code == 200
    assert resp.data == b"<h1>sign in</h1>"
    assert resp.headers["Content-Type"] == "text/html; charset=utf-8"

    # Without a matching entry, an extensionless file still falls back to octet-stream.
    blob = client.get("/httpbin/blob.bin")
    assert blob.status_code == 200
    assert blob.headers["Content-Type"] == "application/octet-stream"


def test_deep_paths_are_served_through_the_catch_all(fresh_client, file_module):
    file_module(
        files={"www/path1/path2/path3/index.html": "<p>deep</p>"},
        routes=[{"uri": "<path:wildcard>", "responses": [{"response": {"file": "www/{wildcard}"}}]}],
    )
    client = fresh_client()
    resp = client.get("/httpbin/path1/path2/path3/index.html")
    assert resp.status_code == 200
    assert resp.data == b"<p>deep</p>"


def test_a_uri_need_not_mirror_the_file_path(fresh_client, file_module):
    """An explicit route may map any URI to any file in the module directory."""
    file_module(
        files={"www/index.html": "<h1>home</h1>"},
        routes=[{"uri": "path1/path2/path3/", "responses": [{"response": {"file": "www/index.html"}}]}],
    )
    client = fresh_client()
    assert client.get("/httpbin/path1/path2/path3/").data == b"<h1>home</h1>"

    # strict_slashes: the unslashed form redirects to it.
    resp = client.get("/httpbin/path1/path2/path3", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["Location"].endswith("/httpbin/path1/path2/path3/")


def test_default_response_can_serve_a_file(fresh_client, file_module):
    file_module(files={"index.html": "<h1>root</h1>"}, routes=[], default_response={"file": "index.html"})
    client = fresh_client()
    resp = client.get("/httpbin/")
    assert resp.status_code == 200
    assert resp.data == b"<h1>root</h1>"


def test_an_explicit_content_type_header_wins(fresh_client, file_module):
    file_module(
        files={"a.txt": "text"},
        routes=[
            {
                "uri": "a",
                "responses": [
                    {"response": {"file": "a.txt", "headers": {"Content-Type": "application/octet-stream"}}}
                ],
            }
        ],
    )
    client = fresh_client()
    assert client.get("/httpbin/a").headers["Content-Type"] == "application/octet-stream"


def test_a_file_response_keeps_its_status(fresh_client, file_module):
    file_module(
        files={"a.txt": "text"},
        routes=[{"uri": "a", "responses": [{"response": {"file": "a.txt", "status": 201}}]}],
    )
    client = fresh_client()
    assert client.get("/httpbin/a").status_code == 201


# --- downloads ------------------------------------------------------------------


def test_download_sets_content_disposition(fresh_client, file_module):
    file_module(
        files={"data/report.bin": b"\x00\x01\x02"},
        routes=[{"uri": "report", "responses": [{"response": {"file": "data/report.bin", "download": True}}]}],
    )
    client = fresh_client()
    resp = client.get("/httpbin/report")
    assert resp.status_code == 200
    assert resp.data == b"\x00\x01\x02"
    assert resp.headers["Content-Type"] == "application/octet-stream"
    assert resp.headers["Content-Disposition"] == 'attachment; filename="report.bin"'


def test_download_filename_can_be_overridden_and_templated(fresh_client, file_module):
    file_module(
        files={"www/a.txt": "A"},
        routes=[
            {
                "uri": "<path:wildcard>",
                "responses": [
                    {
                        "response": {
                            "file": "www/{wildcard}.txt",
                            "download": True,
                            "filename": "dl-{wildcard}.txt",
                        }
                    }
                ],
            }
        ],
    )
    client = fresh_client()
    resp = client.get("/httpbin/a")
    assert resp.status_code == 200
    assert resp.headers["Content-Disposition"] == 'attachment; filename="dl-a.txt"'


# --- safety ---------------------------------------------------------------------


def test_file_paths_must_stay_inside_the_module_directory():
    manifest, errors = validate(
        {
            "default_prefix": "/x",
            "routes": [
                {"uri": "a", "responses": [{"response": {"file": "../secret.txt"}}]},
                {"uri": "b", "responses": [{"response": {"file": "/etc/passwd"}}]},
            ],
        }
    )
    assert manifest is not None
    assert any("'file' '../secret.txt'" in e for e in errors)
    assert any("'file' '/etc/passwd'" in e for e in errors)
    # the bad keys are dropped, so the routes degrade to their body/default
    assert "file" not in manifest.routes[0].responses[0]["response"]
    assert "file" not in manifest.routes[1].responses[0]["response"]


def test_bad_file_key_types_are_dropped_with_errors():
    manifest, errors = validate(
        {
            "default_prefix": "/x",
            "default_response": {"file": 42, "download": "yes", "filename": 7},
        }
    )
    assert any("'file' must be a non-empty string" in e for e in errors)
    assert any("'download' must be a bool" in e for e in errors)
    assert any("'filename' must be a string" in e for e in errors)
    assert manifest.default_response == {}


def test_serve_file_refuses_to_escape_the_module_dir(tmp_path, monkeypatch):
    module_dir = tmp_path / "mod"
    module_dir.mkdir()
    (tmp_path / "secret.txt").write_text("top secret")
    monkeypatch.setattr(MimicModule, "_module_dir", lambda self: module_dir)

    module = MimicModule("x")
    with pytest.raises(NotFound):
        module._serve_file("../secret.txt", 200, {}, False, None)
    with pytest.raises(NotFound):
        module._serve_file("missing.txt", 200, {}, False, None)


def test_a_templated_path_cannot_escape_the_module_dir(fresh_client, file_module):
    file_module(
        files={"www/ok.txt": "ok"},
        routes=[{"uri": "<path:wildcard>", "responses": [{"response": {"file": "www/{wildcard}"}}]}],
    )
    client = fresh_client()
    resp = client.get("/httpbin/..%2F..%2Fmodule.json")
    assert resp.status_code == 404
    assert b"kind" not in resp.data  # the manifest must never leak


def test_a_missing_file_is_a_404(fresh_client, file_module):
    file_module(routes=[{"uri": "gone", "responses": [{"response": {"file": "nope.html"}}]}])
    client = fresh_client()
    resp = client.get("/httpbin/gone")
    assert resp.status_code == 404
    assert resp.get_json()["status"] == "Not Found"


# --- activity log -----------------------------------------------------------------


def test_served_files_are_captured_in_the_activity_log(fresh_client, file_module):
    file_module(
        files={"www/page.html": "<h1>hi</h1>", "www/blob.bin": b"\x89PNG\x00\x01"},
        routes=[{"uri": "<path:wildcard>", "responses": [{"response": {"file": "www/{wildcard}"}}]}],
    )
    client = fresh_client()
    assert client.get("/httpbin/page.html").status_code == 200
    assert client.get("/httpbin/blob.bin").status_code == 200

    rows, total = fetch_logs(limit=50)
    assert total == 2
    by_path = {row.path: row for row in rows}
    html_row = by_path["/httpbin/page.html"]
    assert html_row.response_body == "<h1>hi</h1>"
    assert html_row.error is None
    bin_row = by_path["/httpbin/blob.bin"]
    assert bin_row.response_status == 200
    assert "binary" in (bin_row.response_body or "")
    assert bin_row.error is None


# --- the shipped static module ------------------------------------------------------


def test_the_static_module_serves_its_own_files(client):
    resp = client.get("/static/")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
    assert b"Mimic static module" in resp.data

    assert client.get("/static/app.js").headers["Content-Type"] == "application/javascript; charset=utf-8"
    assert client.get("/static/data.json").get_json()["module"] == "static"
    assert client.get("/static/docs/guide.html").status_code == 200

    download = client.get("/static/download")
    assert download.status_code == 200
    assert download.headers["Content-Disposition"] == 'attachment; filename="sample.txt"'


@pytest.mark.parametrize(
    "path",
    [
        "/static/..%2fmodule.json",
        "/static/../module.json",
        "/static/..%2f__init__.py",
        "/static/docs/..%2f..%2fmodule.json",
        "/static/a%00b",
    ],
)
def test_the_static_module_cannot_be_walked_out_of_www(client, path):
    # One level up is still inside the module dir, so only the request-time
    # check on substituted variables stops this; the containment check would not.
    resp = client.get(path)
    assert resp.status_code == 404
    assert b'"kind"' not in resp.data
    assert b"MODULE_SPEC" not in resp.data


def test_the_static_module_still_serves_nested_files(client):
    assert client.get("/static/docs/guide.html").status_code == 200


@pytest.mark.parametrize(
    ("path", "safe"),
    [
        ("www/index.html", True),
        ("www/a/b.txt", True),
        ("www/..hidden", True),
        ("../secret", False),
        ("www/../module.json", False),
        ("/etc/passwd", False),
        ("www/a\x00b", False),
    ],
)
def test_is_safe_relative_path(path, safe):
    assert is_safe_relative_path(path) is safe


def test_serve_file_refuses_unsafe_paths_even_inside_the_module_dir(tmp_path, monkeypatch):
    module_dir = tmp_path / "mod"
    (module_dir / "www").mkdir(parents=True)
    (module_dir / "module.json").write_text("{}")
    monkeypatch.setattr(MimicModule, "_module_dir", lambda self: module_dir)

    with pytest.raises(NotFound):
        MimicModule("x")._serve_file("www/../module.json", 200, {}, False, None)
