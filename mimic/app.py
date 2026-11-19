"""Flask application factory."""

from __future__ import annotations

import json

from flask import Flask
from werkzeug.exceptions import HTTPException

from mimic.admin.routes import admin_bp
from mimic.auth import ensure_admin_creds
from mimic.db import init_schema
from mimic.dispatch import PrefixDispatcherMiddleware
from mimic.httplog.middleware import register_http_logging
from mimic.httplog.setup import setup_logging
from mimic.modules.runtime import ModuleRuntimeRegistry
from mimic.settings import ensure_secret_key, load_settings
from datetime import UTC

# Every request body is read into memory for the HTTP log, so it has to be bounded.
MAX_REQUEST_BYTES = 8 * 1024 * 1024


def _register_template_filters(app: Flask) -> None:
    @app.template_filter("tsfmt")
    def tsfmt(ts: float) -> str:
        from datetime import datetime

        return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def create_app() -> Flask:
    # The only settings write, kept ahead of load_settings() so it is visible here.
    ensure_secret_key()
    settings = load_settings()

    init_schema()
    ensure_admin_creds()

    log = setup_logging()
    log.info("Mimic is running")

    app = Flask(__name__)
    # Set this before anything else to prevent /admin requests showing in the logs
    register_http_logging(app)

    app.extensions["mimic_settings"] = settings
    app.secret_key = settings.server.secret_key
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
    )
    _register_template_filters(app)

    registry = ModuleRuntimeRegistry(app)
    app.extensions["mimic_module_registry"] = registry
    registry.refresh()

    app.register_blueprint(admin_bp)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.errorhandler(HTTPException)
    def handle_exception(e):
        response = e.get_response()
        response.content_type = "application/json"
        response.data = json.dumps({
            "status_code": e.code,
            "status": e.name,
            "message": e.description,
        })

        return response

    app.wsgi_app = PrefixDispatcherMiddleware(app.wsgi_app, registry.get_pairs)

    return app
