"""Validate `module.json` manifests and apply their defaults.

Two kinds of module. `manifest` declares its routes here and lets
`MimicModule.build_blueprints` register them; individual routes may name a
`handler` method to opt into real Python. `custom` declares no routes and owns
its routing by overriding `build_blueprints`. The kind is checked against the
class in `mimic.modules.loader`, which is the only place that sees both.
"""

from __future__ import annotations

import logging

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

log = logging.getLogger(__name__)

KINDS = ("manifest", "custom")
DEFAULT_METHODS = ["GET"]
DEFAULT_MAX_CONCURRENT = 4


@dataclass(frozen=True)
class Route:
    uri: str
    """Relative to the module's mounted prefix; "" is the prefix root."""

    methods: list[str]
    handler: str | None
    responses: list[Any]
    strict_slashes: bool


@dataclass(frozen=True)
class Manifest:
    kind: str
    title: str
    description: str
    default_enabled: bool
    default_prefix: str
    host: str | None
    max_concurrent: int
    default_methods: list[str]
    default_response: dict[str, Any]
    strict_slashes: bool
    routes: list[Route] = field(default_factory=list)
    error_pages: dict[str, Any] = field(default_factory=dict)


def _typed(raw: dict, key: str, expected: type, default: Any, errors: list[str]) -> Any:
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, expected) or (expected is not bool and isinstance(value, bool)):
        errors.append(f"{key!r} must be a {expected.__name__}")
        return default
    return value


def is_safe_relative_path(path: str) -> bool:
    """No absolute paths, '..' segments or NUL bytes."""
    return not path.startswith("/") and "\x00" not in path and ".." not in PurePosixPath(path).parts


def _sanitize_file_keys(where: str, fragment: Any, errors: list[str]) -> None:
    """Validate the file-serving keys of a response fragment, in place.

    A bad value is dropped from the fragment (with an error) rather than failing
    the whole module, matching how other wrong-typed scalars are handled. The
    path must stay inside the module directory; what it points at is checked per
    request, since variables can only be known then.
    """
    if not isinstance(fragment, dict):
        return

    if "file" in fragment:
        path = fragment["file"]
        if not isinstance(path, str) or not path.strip():
            errors.append(f"{where}: 'file' must be a non-empty string")
            del fragment["file"]
        elif not is_safe_relative_path(path):
            errors.append(
                f"{where}: 'file' {path!r} must be relative to the module directory and must not contain '..'"
            )
            del fragment["file"]

    if "download" in fragment and not isinstance(fragment["download"], bool):
        errors.append(f"{where}: 'download' must be a bool")
        del fragment["download"]

    if "filename" in fragment and not isinstance(fragment["filename"], str):
        errors.append(f"{where}: 'filename' must be a string")
        del fragment["filename"]


def _route(raw: Any, index: int, manifest: dict, errors: list[str]) -> Route | None:
    where = f"routes[{index}]"
    if not isinstance(raw, dict):
        errors.append(f"{where} is not an object")
        return None

    uri = raw.get("uri")
    if not isinstance(uri, str):
        errors.append(f"{where}: 'uri' is required and must be a string")
        return None
    if uri.startswith("/"):
        errors.append(f"{where}: uri {uri!r} must be relative to the module prefix")
        return None

    handler = raw.get("handler")
    if handler is not None and not isinstance(handler, str):
        errors.append(f"{where}: 'handler' must be a string")
        return None

    methods = _typed(raw, "methods", list, manifest["default_methods"], errors)
    responses = _typed(raw, "responses", list, [], errors)
    for index, entry in enumerate(responses):
        if isinstance(entry, dict) and isinstance(entry.get("response"), dict):
            _sanitize_file_keys(f"{where}.responses[{index}]", entry["response"], errors)
    strict = _typed(raw, "strict_slashes", bool, manifest["strict_slashes"], errors)
    return Route(uri=uri, methods=methods, handler=handler, responses=responses, strict_slashes=strict)


def _error_pages(raw: Any, errors: list[str]) -> dict[str, Any]:
    """Parse the optional `error_pages` mapping of status code -> response fragment.

    Keys must be integer status codes; each value reuses the normal response shape
    (body/file/headers). A bad key or fragment is dropped with an error so one typo
    cannot take down the module's other error pages.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append("'error_pages' must be a JSON object mapping status codes to responses")
        return {}

    pages: dict[str, Any] = {}
    for code, fragment in raw.items():
        try:
            status = int(code)
        except (TypeError, ValueError):
            errors.append(f"error_pages[{code!r}]: status code must be an integer")
            continue
        if not 100 <= status <= 599:
            errors.append(f"error_pages[{code!r}]: status code {status} is out of range")
            continue
        if not isinstance(fragment, dict):
            errors.append(f"error_pages[{code!r}]: response must be an object")
            continue
        _sanitize_file_keys(f"error_pages[{code!r}]", fragment, errors)
        pages[str(status)] = fragment
    return pages


def validate(raw: Any) -> tuple[Manifest | None, list[str]]:
    """Returns the manifest and everything wrong with it.

    A `None` manifest means the module cannot load at all. Route-level problems
    drop just that route and still return a usable manifest.
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["manifest is not a JSON object"]

    kind = _typed(raw, "kind", str, "manifest", errors)
    if kind not in KINDS:
        return None, errors + [f"'kind' must be one of {' or '.join(map(repr, KINDS))}, not {kind!r}"]

    prefix = raw.get("default_prefix")
    if not isinstance(prefix, str) or not prefix.strip():
        return None, errors + ["'default_prefix' is required"]

    host = _typed(raw, "host", str, None, errors) or None
    defaults = {
        "default_methods": _typed(raw, "default_methods", list, DEFAULT_METHODS, errors),
        "strict_slashes": _typed(raw, "strict_slashes", bool, True, errors),
    }

    routes: list[Route] = []
    seen: set[str] = set()
    for index, entry in enumerate(_typed(raw, "routes", list, [], errors)):
        route = _route(entry, index, defaults, errors)
        if route is None:
            continue
        if route.uri in seen:
            errors.append(f"routes[{index}]: duplicate uri {route.uri!r}")
            continue
        seen.add(route.uri)
        routes.append(route)

    if kind == "custom" and routes:
        return None, errors + ["kind 'custom' must not declare routes"]

    default_response = _typed(raw, "default_response", dict, {}, errors)
    _sanitize_file_keys("default_response", default_response, errors)

    error_pages = _error_pages(raw.get("error_pages"), errors)

    manifest = Manifest(
        kind=kind,
        title=_typed(raw, "title", str, "", errors),
        description=_typed(raw, "description", str, "", errors),
        default_enabled=_typed(raw, "default_enabled", bool, False, errors),
        default_prefix=prefix,
        host=host,
        max_concurrent=_typed(raw, "max_concurrent", int, DEFAULT_MAX_CONCURRENT, errors),
        default_methods=defaults["default_methods"],
        default_response=default_response,
        strict_slashes=defaults["strict_slashes"],
        routes=routes,
        error_pages=error_pages,
    )
    return manifest, errors


def resolve_routes(
    manifest: Manifest, module: object
) -> tuple[list[tuple[Route, Callable[..., Any] | None]], list[str]]:
    """Pair each route with its handler method, dropping the ones that name a bad handler.

    Resolution happens at load time so a typo is a dashboard error rather than a
    500 on the first request.
    """
    # Local import: mimic.modules.base imports this module.
    from mimic.modules.base import MimicModule

    resolved: list[tuple[Route, Callable[..., Any] | None]] = []
    errors: list[str] = []
    for route in manifest.routes:
        if route.handler is None:
            resolved.append((route, None))
            continue
        # Handlers come from editable config, so private methods, dunders and the
        # base class's own machinery (e.g. __init__, _serve_file) are off limits.
        if route.handler.startswith("_") or hasattr(MimicModule, route.handler):
            errors.append(f"route {route.uri!r}: handler {route.handler!r} is not allowed")
            continue
        func = getattr(module, route.handler, None)
        if not callable(func):
            errors.append(f"route {route.uri!r}: handler {route.handler!r} is not a callable method")
            continue
        resolved.append((route, func))
    return resolved, errors
