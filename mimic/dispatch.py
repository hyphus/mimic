"""WSGI middleware: route requests to module sub-applications by longest path-prefix match."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from mimic.modules.loader import normalize_prefix


WSGIApp = Callable[..., Any]

# Module traffic never gets every worker thread: the remainder is headroom for
# /admin and /healthz, which bypass this middleware entirely.
GLOBAL_MODULE_CONCURRENCY = 6
RETRY_AFTER_SECONDS = "1"


def _path_info_str(environ: dict) -> str:
    raw = environ.get("PATH_INFO") or "/"
    if isinstance(raw, bytes):
        return raw.decode("latin-1")
    return raw


def path_matches_prefix(path: str, prefix: str) -> bool:
    """True if request path is under prefix (prefix must not be root /)."""
    p = normalize_prefix(prefix)
    if p == "/":
        return False
    if path == p or path.rstrip("/") == p:
        return True
    return path.startswith(p + "/")


class _ReleasingIterable:
    """Releases the held semaphores once the WSGI server is done with the body."""

    __slots__ = ("_inner", "_release")

    def __init__(self, inner: Any, release: Callable[[], None]) -> None:
        self._inner = inner
        self._release = release

    def __iter__(self):
        return iter(self._inner)

    def close(self) -> None:
        try:
            close = getattr(self._inner, "close", None)
            if close is not None:
                close()
        finally:
            self._release()


class PrefixDispatcherMiddleware:
    """
    If PATH_INFO is under a mounted module prefix, delegate to that sub-app's WSGI
    with an unchanged PATH_INFO (Flask blueprints keep their configured url_prefix).

    Module traffic is capped globally and per module so that one slow module
    cannot consume every worker thread.
    """

    __slots__ = ("app", "_get_pairs", "_global", "_per_module", "_lock")

    def __init__(self, app: WSGIApp, get_pairs: Callable[[], tuple[tuple[str, WSGIApp, int], ...]]):
        self.app = app
        self._get_pairs = get_pairs
        self._global = threading.BoundedSemaphore(GLOBAL_MODULE_CONCURRENCY)
        self._per_module: dict[tuple[str, int], threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    def _module_semaphore(self, prefix: str, limit: int) -> threading.BoundedSemaphore:
        # Keyed by the limit too: a manifest that changes max_concurrent gets a
        # fresh semaphore instead of silently keeping the old cap.
        key = (prefix, limit)
        with self._lock:
            sem = self._per_module.get(key)
            if sem is None:
                sem = threading.BoundedSemaphore(limit)
                self._per_module[key] = sem
            return sem

    def __call__(self, environ: dict, start_response: Callable) -> Any:
        path = _path_info_str(environ)
        for prefix, sub_wsgi, max_concurrent in self._get_pairs():
            if path_matches_prefix(path, prefix):
                return self._dispatch(prefix, max_concurrent, sub_wsgi, environ, start_response)
        return self.app(environ, start_response)

    def _dispatch(self, prefix: str, limit: int, sub_wsgi: WSGIApp, environ: dict, start_response: Callable) -> Any:
        module_sem = self._module_semaphore(prefix, limit)
        if not self._global.acquire(blocking=False):
            return _busy(start_response)
        if not module_sem.acquire(blocking=False):
            self._global.release()
            return _busy(start_response)

        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                module_sem.release()
                self._global.release()

        try:
            # Released from close(), not here: the body may be a lazy iterable and
            # the slot is still in use until the server has drained it.
            return _ReleasingIterable(sub_wsgi(environ, start_response), release)
        except BaseException:
            release()
            raise


def _busy(start_response: Callable) -> list[bytes]:
    body = b'{"status_code": 503, "status": "Service Unavailable", "message": "Module is at capacity"}'
    start_response(
        "503 Service Unavailable",
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Retry-After", RETRY_AFTER_SECONDS),
        ],
    )
    return [body]
