"""WSGI middleware: keep /admin off the public port and the modules off the admin port."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

WSGIApp = Callable[..., Any]


def _is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


class PortGateMiddleware:
    """
    Splits one app across two sockets by SERVER_PORT. The public port serves
    everything except /admin; the admin port serves only /admin and /healthz.
    Requests arriving on any other port are refused.
    """

    __slots__ = ("app", "public_port", "admin_port")

    def __init__(self, app: WSGIApp, public_port: int, admin_port: int) -> None:
        self.app = app
        self.public_port = public_port
        self.admin_port = admin_port

    def __call__(self, environ: dict, start_response: Callable) -> Any:
        try:
            port = int(environ.get("SERVER_PORT") or 0)
        except ValueError:
            port = 0
        path = environ.get("PATH_INFO") or "/"
        if isinstance(path, bytes):
            path = path.decode("latin-1")
        # Werkzeug strips leading slashes before routing (MapAdapter.match), so
        # "//admin" reaches the admin blueprint; judge the path it will route.
        path = "/" + path.lstrip("/")

        if port == self.public_port:
            allowed = not _is_admin_path(path)
        elif port == self.admin_port:
            allowed = _is_admin_path(path) or path == "/healthz"
        else:
            allowed = False
        if not allowed:
            return _not_found(start_response)
        return self.app(environ, start_response)


def _not_found(start_response: Callable) -> list[bytes]:
    body = b'{"status_code": 404, "status": "Not Found", "message": "Not found"}'
    start_response(
        "404 Not Found",
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]
