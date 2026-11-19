"""Capture HTTP exchanges for the admin activity log."""

from __future__ import annotations

from typing import Any

from flask import Flask, Response, g, request

from mimic.httplog.store import insert_log
from mimic.settings import MAX_BODY_CHARS

_FILTERED_ROUTES = [
    "/admin",
    "/healthz",
    "/favicon.ico",
]


def _is_filtered_route(path: str) -> bool:
    return any(path == route or path.startswith(route + "/") for route in _FILTERED_ROUTES)


def _headers_to_dict(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        out[k] = v
    return out


def _truncate_body(text: str | None) -> str | None:
    if text is None:
        return None
    if len(text) > MAX_BODY_CHARS:
        return text[:MAX_BODY_CHARS] + "\n...[truncated]"
    return text


def register_http_logging(app: Flask) -> None:
    @app.before_request
    def mimic_capture_request() -> None:
        g.mimic_exchange_logged = False
        if not _is_filtered_route(request.path):
            g.mimic_req_body = _truncate_body(request.get_data(cache=True, as_text=True))

    @app.after_request
    def mimic_log_after(response: Response) -> Response:
        if _is_filtered_route(request.path):
            return response
        try:
            err = None
            resp_body: str | None = None
            # get_data() buffers a streamed response to completion, so it is only
            # safe for finite bodies; an SSE stream never completes. Redirects also
            # arrive streamed (Flask materializes them through run_wsgi_app) but
            # their HTML page is small and finite, so they are captured like any
            # other body.
            if response.direct_passthrough:
                err = "response body unavailable: direct passthrough"
            elif response.is_streamed and response.mimetype == "text/event-stream":
                err = "response body unavailable: streamed response"
            else:
                try:
                    resp_body = _truncate_body(response.get_data(as_text=True))
                except UnicodeDecodeError:
                    # Binary bodies (file downloads) have no text form; log their size instead.
                    resp_body = f"<binary body, {len(response.get_data())} bytes>"

            insert_log(
                method=request.method,
                path=request.path,
                query_string=request.query_string.decode("utf-8") if request.query_string else None,
                module_id=getattr(g, "mimic_module_id", None),
                request_headers=_headers_to_dict(request.headers),
                request_body=g.get("mimic_req_body"),
                response_status=response.status_code,
                response_headers=_headers_to_dict(response.headers),
                response_body=resp_body,
                error=err,
            )
            g.mimic_exchange_logged = True
        except Exception:
            app.logger.exception("mimic: failed to persist HTTP log row")
        return response

    @app.teardown_request
    def mimic_log_teardown(exc: BaseException | None) -> None:
        if exc is None or getattr(g, "mimic_exchange_logged", False) or _is_filtered_route(request.path):
            return
        try:
            body = g.get("mimic_req_body")
            insert_log(
                method=request.method,
                path=request.path,
                query_string=request.query_string.decode("utf-8") if request.query_string else None,
                module_id=getattr(g, "mimic_module_id", None),
                request_headers=_headers_to_dict(request.headers),
                request_body=body,
                response_status=None,
                response_headers={},
                response_body=None,
                error=repr(exc),
            )
        except Exception:
            app.logger.exception("mimic: failed to persist error log row")
