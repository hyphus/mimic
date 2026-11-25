from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from flask.testing import FlaskClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Point MIMIC_DATA_DIR at tmp_path and clear the ambient MIMIC_* environment.

    Without this the suite writes into the repo's data/ directory and tests
    contaminate each other through .env and mimic.sqlite3.
    """
    for key in [k for k in os.environ if k.startswith("MIMIC_")]:
        monkeypatch.delenv(key, raising=False)

    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("MIMIC_DATA_DIR", str(d))
    monkeypatch.setenv("MIMIC_ADMIN_PASSWORD", "hunter2")
    monkeypatch.setenv("MIMIC_SECRET_KEY", "test-secret-key")

    # create_app() does this at boot; tests that query the DB without one need it too.
    from mimic.db import init_schema

    init_schema()
    return d


@pytest.fixture(autouse=True)
def reset_login_throttle():
    """The login throttle keeps process-global state; isolate each test."""
    from mimic.auth import reset_login_throttle_state

    reset_login_throttle_state()
    yield
    reset_login_throttle_state()


@pytest.fixture(autouse=True)
def reset_http_log_retention_cache():
    """Retention bounds are cached per process; re-read them for every test."""
    from mimic.settings import reset_http_log_retention_cache as _reset

    _reset()


@pytest.fixture(autouse=True)
def shipped_modules_enabled(tmp_path, monkeypatch):
    """Serve the shipped modules from copies with default_enabled on.

    They ship disabled so a fresh deployment exposes nothing until an operator opts
    in, but most tests exercise them. Fixtures that rewrite a manifest wrap the
    patched manifest_path, so they start from the enabled copy.
    """
    from mimic.modules import registry

    real = registry.manifest_path
    copies = tmp_path / "enabled_manifests"
    copies.mkdir()

    def manifest_path(module_id: str) -> Path:
        source = real(module_id)
        if not source.exists():
            return source
        copy = copies / f"{module_id}.json"
        if not copy.exists():
            raw = json.loads(source.read_text())
            raw["default_enabled"] = True
            copy.write_text(json.dumps(raw))
        return copy

    monkeypatch.setattr(registry, "manifest_path", manifest_path)


@pytest.fixture
def app():
    from mimic.app import create_app

    return create_app()


class BufferedClient(FlaskClient):
    """Always drain and close the WSGI iterable.

    Werkzeug's unbuffered default never calls close(), so the dispatcher's
    concurrency slots would never be returned between requests.
    """

    def open(self, *args, buffered=True, **kwargs):
        return super().open(*args, buffered=buffered, **kwargs)


@pytest.fixture
def client(app):
    app.test_client_class = BufferedClient
    return app.test_client()


@pytest.fixture
def fresh_client():
    """Build a brand new app (and client) so config changes take effect."""
    from mimic.app import create_app

    def factory():
        new_app = create_app()
        new_app.test_client_class = BufferedClient
        return new_app.test_client()

    return factory
