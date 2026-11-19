"""Thread-safe runtime registry of module Flask sub-apps + hot reload from config."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from flask import Flask, g
from werkzeug.exceptions import HTTPException

from mimic.aws import create_dns_record
from mimic.acme import generate_certificate
from mimic.httplog.middleware import register_http_logging
from mimic.modules.base import LoadedModule
from mimic.modules.loader import ModuleLoadResult, build_modules, normalize_prefix
from mimic.settings import le_staging, load_runtime_settings

log = logging.getLogger(__name__)


def _load_info_public_dict(result: ModuleLoadResult) -> dict[str, Any]:
    return {
        "loaded": [
            {
                "id": m.id,
                "path_prefix": m.path_prefix,
                "host": m.host,
                "blueprints": len(m.blueprints),
            }
            for m in result.loaded
        ],
        "errors": list(result.errors),
        "skipped": list(result.skipped),
    }


def _provision_dns(result: ModuleLoadResult) -> None:
    """A module that declares a host needs that record to exist before it can serve.

    This lives next to the certificate logic rather than in MimicModule so that
    modules stay free of the AWS and settings layers.
    """
    runtime = load_runtime_settings()
    if not runtime.features.create_dns_records:
        return
    for m in list(result.loaded):
        if not m.host:
            continue
        if not create_dns_record(m.host, runtime.aws.target_host):
            result.loaded.remove(m)
            result.errors.append(f"{m.id}: failed to create DNS record {m.host!r}; not loaded")


def _make_sub_app(loaded: LoadedModule, main: Flask) -> Flask:
    """One Flask application per LoadedModule carrying only that module's blueprints."""
    sub = Flask(f"mimic.dispatch.{loaded.id}", static_folder=None)
    sub.secret_key = main.secret_key
    sub.testing = main.testing
    sub.debug = main.debug
    sub.config["MAX_CONTENT_LENGTH"] = main.config["MAX_CONTENT_LENGTH"]
    # Only modules that declare a host get host matching; with it on, rules
    # registered without a host never match a real request.
    sub.url_map.host_matching = bool(loaded.host)
    register_http_logging(sub)

    # tag module traffic with module id
    @sub.before_request
    def tag_module() -> None:
        g.mimic_module_id = loaded.id

    # global error handler for HTTP exceptions
    @sub.errorhandler(HTTPException)
    def handle_http_exception(e: HTTPException):
        # A module may define its own page (JSON or static file) for a status code;
        # anything it does not cover, or that fails to render, falls back to JSON.
        if loaded.module is not None:
            try:
                custom = loaded.module.error_response(e)
            except Exception:
                log.exception(f"Module {loaded.id!r} failed to render its {e.code} error page")
                custom = None
            if custom is not None:
                return custom

        response = e.get_response()
        response.content_type = "application/json"
        response.data = json.dumps({
            "status_code": e.code,
            "status": e.name,
            "message": e.description,
        })
        return response

    for bp in loaded.blueprints:
        sub.register_blueprint(bp)
    return sub


class ModuleRuntimeRegistry:
    """
    Builds isolated Flask sub-apps per loaded module and exposes an immutable
    (prefix, wsgi) tuple for the dispatcher. refresh() re-reads config and
    rebuilds everything (path conflicts run inside build_modules).
    """

    __slots__ = ("_lock", "_main", "_pairs", "_sub_apps", "_cert_thread")

    def __init__(
        self,
        main_app: Flask,
    ) -> None:
        self._lock = threading.RLock()
        self._main = main_app
        self._pairs: tuple[tuple[str, Any, int], ...] = ()
        self._sub_apps: list[Flask] = []
        self._cert_thread: threading.Thread | None = None

    def get_pairs(self) -> tuple[tuple[str, Any, int], ...]:
        with self._lock:
            return self._pairs

    def refresh(self) -> bool:
        """
        Rebuild module sub-apps from disk. On failure, keeps the previous snapshot.
        Returns True if the new snapshot was applied.
        """
        log = self._main.logger
        try:
            result = build_modules()
            _provision_dns(result)
            new_subs: list[Flask] = []
            mount_list: list[tuple[str, Any, int]] = []
            for m in list(result.loaded):
                try:
                    sub = _make_sub_app(m, self._main)
                except Exception as e:
                    result.loaded.remove(m)
                    result.errors.append(f"{m.id}: failed to build sub-app: {e!r}")
                    continue
                new_subs.append(sub)
                mount_list.append((m.path_prefix, sub.wsgi_app, m.max_concurrent))

            mount_list.sort(
                key=lambda item: (len(normalize_prefix(item[0])), item[0]),
                reverse=True,
            )
            new_pairs = tuple(mount_list)

            load_info = _load_info_public_dict(result)
            hosts = [m.host for m in result.loaded if m.host]
            features = load_runtime_settings().features
            if hosts and features.create_certificates:
                # Start certificate generation in background so startup isn't blocked.
                try:
                    self._start_certificate_generation(hosts, le_staging())
                except Exception:
                    log.exception("Failed to start background certificate generation")

            for error in result.errors:
                log.error(f"Module load error: {error}")

            with self._lock:
                self._pairs = new_pairs
                self._sub_apps = new_subs
                self._main.config["MIMIC_MODULE_LOAD"] = load_info

            return True
        except Exception as e:
            log.exception(f"Module registry refresh failed, keeping previous routes\nError: {e}")
            return False

    def _start_certificate_generation(self, hosts: list[str], staging: bool) -> None:
        """Spawn a background thread to generate certificates and reload Gunicorn master on success."""
        # If a thread is already running and alive, don't start another
        if self._cert_thread is not None and self._cert_thread.is_alive():
            log.debug("Certificate generation already in progress, skipping new request")
            return

        # TODO: Make this work with Flask and clean it up
        def _worker(hosts_local: list[str], staging_local: bool) -> None:
            try:
                if generate_certificate(hosts_local, staging_local):
                    try:
                        from mimic.gunicorn_reload import schedule_gunicorn_master_reload

                        scheduled = schedule_gunicorn_master_reload()
                        if scheduled:
                            log.info("Reloading to apply new certificates")
                    except Exception:
                        log.exception("Failed to apply new certificates")
            except Exception as e:
                log.exception(f"Certificate generation failed\n{e}")

        t = threading.Thread(target=_worker, args=(hosts, staging), daemon=True)
        self._cert_thread = t
        t.start()
