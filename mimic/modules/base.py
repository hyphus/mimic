from __future__ import annotations

from copy import deepcopy
import json
import logging
import mimetypes

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from flask import Blueprint, Response, request
from pathlib import Path
from typing import Any
from werkzeug.exceptions import HTTPException, NotFound

from mimic.modules.schema import Manifest, is_safe_relative_path, resolve_routes

log = logging.getLogger(__name__)


def _attachment_header(filename: str) -> str:
    """A Content-Disposition value for a download.

    Filenames can be templated with captured URL variables, so newlines are
    stripped (they would inject headers) and quotes/backslashes escaped.
    """
    safe = filename.replace("\r", "").replace("\n", "").replace("\\", "\\\\").replace('"', '\\"')
    return f'attachment; filename="{safe}"'


@dataclass
class LoadedModule:
    id: str
    blueprints: list[Blueprint]
    path_prefix: str
    host: str | None
    max_concurrent: int
    module: MimicModule | None = None

    def __post_init__(self) -> None:
        if not self.blueprints:
            log.error(f"Failed to load module {self.id!r}: at least one blueprint is required")


@dataclass(frozen=True)
class ModuleSpec:
    """Published as MODULE_SPEC by each package under mimic/modules/<name>/.

    One type is one instance, so the spec is just an id and the object that
    serves it; everything else the app needs is in that package's module.json.
    """

    module_id: str
    module: MimicModule


class MimicModule:
    def __init__(self, module_id: str) -> None:
        self.module_id = module_id
        # Populated from the manifest by the loader before blueprints are built;
        # read by error_response() to render a module's custom error pages.
        self._error_pages: dict[str, Any] = {}

    def _module_dir(self) -> Path:
        return Path(__file__).resolve().parent / self.module_id

    # Explicit types for the static content we promise to serve; anything else
    # falls back to the system mime database, then application/octet-stream.
    _FILE_MIME_TYPES = {
        ".html": "text/html",
        ".htm": "text/html",
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".json": "application/json",
        ".css": "text/css",
        ".txt": "text/plain",
        ".svg": "image/svg+xml",
    }
    _TEXTUAL_MIME_TYPES = {"application/javascript", "image/svg+xml"}

    def _build_response(self, resp_data: dict[str, Any]) -> Response:
        status = 200
        headers: dict[str, str] = {}
        body = ""
        # Werkzeug would default to text/html, turning templated bodies into reflected XSS.
        default_type = "text/plain; charset=utf-8"
        passthrough = False
        file_path: Any = None
        download = False
        filename: str | None = None

        if resp_data:
            status = resp_data.get("status", status)
            headers = resp_data.get("headers", headers)
            body = resp_data.get("body", body)
            if not isinstance(body, str):
                body = json.dumps(body)
                default_type = "application/json"
            passthrough = resp_data.get("passthrough", passthrough)
            file_path = resp_data.get("file")
            download = resp_data.get("download", False)
            filename = resp_data.get("filename")

        # A file response replaces the body; a non-string 'file' (a manifest that
        # slipped past validation) falls back to it.
        if isinstance(file_path, str) and file_path:
            return self._serve_file(file_path, status, headers, download, filename)

        if not any(key.lower() == "content-type" for key in headers):
            headers = {**headers, "Content-Type": default_type}
        return Response(body, status=status, headers=headers, direct_passthrough=passthrough)

    def error_response(self, e: HTTPException) -> Response | None:
        """Render this module's custom page for an HTTP error, or None to fall back.

        The fragment reuses the normal response shape (body/file/headers); the status
        is always forced to the error's own code so a 404.html is served as a 404.
        """
        fragment = self._error_pages.get(str(e.code))
        if not isinstance(fragment, dict):
            return None
        return self._build_response({**fragment, "status": e.code})

    @classmethod
    def _content_type_for(cls, name: str) -> str:
        mime = (
            cls._FILE_MIME_TYPES.get(Path(name).suffix.lower())
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )
        if mime.startswith("text/") or mime in cls._TEXTUAL_MIME_TYPES:
            return f"{mime}; charset=utf-8"
        return mime

    def _serve_file(
        self, rel_path: str, status: int, headers: dict[str, str], download: bool, filename: str | None
    ) -> Response:
        """Serve a file from this module's own directory.

        The path may have been templated with captured URL variables, so the
        containment check runs here, per request, not just at load time.
        """
        # The manifest itself cannot contain '..', so one here came from a URL variable.
        if not is_safe_relative_path(rel_path):
            raise NotFound()
        module_dir = self._module_dir().resolve()
        candidate = (module_dir / rel_path).resolve()
        if not candidate.is_relative_to(module_dir) or not candidate.is_file():
            raise NotFound()

        headers = dict(headers)
        if not any(key.lower() == "content-type" for key in headers):
            headers["Content-Type"] = self._content_type_for(candidate.name)
        if download or filename:
            headers["Content-Disposition"] = _attachment_header(filename or candidate.name)

        return Response(candidate.read_bytes(), status=status, headers=headers)

    def build_blueprints(self, url_prefix: str, manifest: Manifest) -> Iterable[Blueprint]:
        """The manifest is the effective one the loader resolved, not a fresh read."""
        bp = Blueprint(f"mimic_{self.module_id}", __name__, url_prefix=url_prefix)
        default_response = manifest.default_response
        host = manifest.host

        def _matches(match: dict[str, Any], variables: dict[str, Any]) -> bool:
            for key, expected in match.items():
                if key == "query":
                    # Reserved: an object of query param name -> value spec, where a
                    # string is exact, a list is any-of and "*" means present.
                    if not isinstance(expected, dict):
                        return False
                    query = variables.get("query") or {}
                    for param, spec in expected.items():
                        actual = query.get(param)
                        if spec == "*":
                            if actual is None:
                                return False
                        elif isinstance(spec, str):
                            if actual != spec:
                                return False
                        elif isinstance(spec, list):
                            if actual not in spec:
                                return False
                        else:
                            return False
                    continue

                actual = variables.get(key)

                if isinstance(expected, str):
                    if actual != expected:
                        return False
                elif isinstance(expected, list):
                    if actual not in expected:
                        return False
                else:
                    return False
            return True

        def _render_template(obj: Any, variables: dict[str, Any]) -> Any:
            if isinstance(obj, dict):
                return {
                    k: _render_template(v, variables)
                    for k, v in obj.items()
                }

            if isinstance(obj, list):
                return [
                    _render_template(v, variables)
                    for v in obj
                ]

            if isinstance(obj, str):
                return obj.format(**variables)

            return deepcopy(obj)

        def _find_matching_response(route_config: list[Any], variables: dict[str, Any]) -> dict[str, Any] | None:
            for candidate in route_config:
                if _matches(candidate.get("match", {}), variables):
                    return candidate["response"]

            return default_response

        def make_dynamic_route(route_config: list[Any]) -> Callable[..., Response]:
            def route_func(**kwargs) -> Response:
                # inject HTTP method and query string for matching requests
                kwargs["method"] = request.method
                kwargs["query"] = dict(request.args)
                response = _find_matching_response(route_config, kwargs)

                # This can only happen if default_response isn't set
                if response is None:
                    raise NotFound()

                response = _render_template(response, kwargs)

                return self._build_response(response)

            return route_func

        # Handler names were already validated by the loader; a route naming a
        # bad one is dropped here for the same reason it was reported there.
        resolved, _ = resolve_routes(manifest, self)

        # A catch-all at the prefix root, unless the module declares that route itself.
        if not any(route.uri == "" for route, _ in resolved):
            @bp.route("/", methods=manifest.default_methods, strict_slashes=manifest.strict_slashes, host=host)
            def root() -> Response:
                return self._build_response(default_response)

        for route, handler in resolved:
            try:
                bp.add_url_rule(
                    rule=f"/{route.uri}" if route.uri else "/",
                    endpoint=route.uri or "root",
                    view_func=handler or make_dynamic_route(route.responses),
                    strict_slashes=route.strict_slashes,
                    methods=route.methods,
                    host=host,
                )
            except Exception as e:
                log.error(f"Failed to load route for module {self.module_id}: {e}\nRoute: {route!r}")

        yield bp
