"""Module manifests on disk plus the admin UI's overrides in SQLite.

`module.json` is the source of truth and is re-read on every boot. The
`module_overrides` table records only what an operator changed in the UI, so
clearing a row always returns a module to its shipped behaviour.
"""

from __future__ import annotations

import json
import logging

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mimic.modules as modules_pkg

from mimic.db import execute, query_one
from mimic.modules.schema import Manifest, validate

log = logging.getLogger(__name__)

OVERRIDABLE = ("enabled", "path_prefix", "host", "routes", "error_pages")


@dataclass(frozen=True)
class EffectiveModule:
    """A module's shipped manifest, the same manifest with overrides applied, and why not."""

    module_id: str
    manifest: Manifest | None
    shipped: Manifest | None
    overridden: frozenset[str] = frozenset()
    errors: list[str] = field(default_factory=list)


def manifest_path(module_id: str) -> Path:
    return Path(modules_pkg.__file__).parent / module_id / "module.json"


def _read_manifest(module_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        with open(manifest_path(module_id), encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None, ["module.json is missing"]
    except Exception as e:
        return None, [f"module.json could not be read: {e}"]
    if not isinstance(raw, dict):
        return None, ["module.json is not a JSON object"]
    return raw, []


def _override_row(module_id: str):
    return query_one("SELECT * FROM module_overrides WHERE module_id = ?", (module_id,))


def load_module(module_id: str) -> EffectiveModule:
    raw, errors = _read_manifest(module_id)
    if raw is None:
        return EffectiveModule(module_id, None, None, errors=errors)

    shipped, shipped_errors = validate(raw)
    errors += shipped_errors

    row = _override_row(module_id)
    if row is None:
        return EffectiveModule(module_id, shipped, shipped, errors=errors)

    applied: set[str] = set()
    patched = dict(raw)
    if row["enabled"] is not None:
        patched["default_enabled"] = bool(row["enabled"])
        applied.add("enabled")
    if row["path_prefix"] is not None:
        patched["default_prefix"] = row["path_prefix"]
        applied.add("path_prefix")
    if row["host"] is not None:
        patched["host"] = row["host"] or None
        applied.add("host")
    if row["routes_json"] is not None:
        try:
            patched["routes"] = json.loads(row["routes_json"])
            applied.add("routes")
        except json.JSONDecodeError as e:
            errors.append(f"stored routes override is not valid JSON: {e}")
    if row["error_pages_json"] is not None:
        try:
            patched["error_pages"] = json.loads(row["error_pages_json"])
            applied.add("error_pages")
        except json.JSONDecodeError as e:
            errors.append(f"stored error_pages override is not valid JSON: {e}")

    if not applied:
        return EffectiveModule(module_id, shipped, shipped, errors=errors)

    manifest, patched_errors = validate(patched)
    return EffectiveModule(module_id, manifest, shipped, frozenset(applied), errors + patched_errors)


def list_modules() -> dict[str, dict[str, Any]]:
    """Everything the admin dashboard shows, per discovered module."""
    from mimic.modules.discovery import specs_by_module_id

    out: dict[str, dict[str, Any]] = {}
    for module_id in sorted(specs_by_module_id()):
        effective = load_module(module_id)
        manifest = effective.manifest
        shipped = effective.shipped
        out[module_id] = {
            "title": (manifest.title if manifest else "") or module_id,
            "description": manifest.description if manifest else "",
            "kind": manifest.kind if manifest else "",
            "enabled": bool(manifest and manifest.default_enabled),
            "path_prefix": manifest.default_prefix if manifest else None,
            "host": manifest.host if manifest else None,
            "shipped_prefix": shipped.default_prefix if shipped else "",
            "shipped_host": shipped.host if shipped else "",
            "routes_json": json.dumps(_routes_of(manifest), indent=2),
            "error_pages_json": json.dumps(_error_pages_of(manifest), indent=2),
            "overridden": effective.overridden,
            "errors": effective.errors,
        }
    return out


def _routes_of(manifest: Manifest | None) -> list[dict[str, Any]]:
    if manifest is None:
        return []
    return [
        {
            "uri": r.uri,
            "methods": r.methods,
            "handler": r.handler,
            "responses": r.responses,
            "strict_slashes": r.strict_slashes,
        }
        for r in manifest.routes
    ]


def _error_pages_of(manifest: Manifest | None) -> dict[str, Any]:
    if manifest is None:
        return {}
    return manifest.error_pages


def module_ids() -> list[str]:
    from mimic.modules.discovery import specs_by_module_id

    return sorted(specs_by_module_id())


def set_module_enabled(module_id: str, enabled: bool) -> None:
    execute(
        """
        INSERT INTO module_overrides (module_id, enabled) VALUES (?, ?)
        ON CONFLICT(module_id) DO UPDATE SET enabled = excluded.enabled
        """,
        (module_id, int(enabled)),
    )


def save_module_overrides(
    module_id: str, *, path_prefix: str, host: str, routes: list[Any], error_pages: dict[str, Any]
) -> None:
    """Store only the fields that differ from the shipped manifest.

    Editing a value back to what the manifest ships clears the override, so
    "overridden" in the UI always means "disk says something else".
    """
    shipped = load_module(module_id).shipped
    shipped_routes = _routes_of(shipped)
    shipped_error_pages = _error_pages_of(shipped)
    execute(
        """
        INSERT INTO module_overrides (module_id, path_prefix, host, routes_json, error_pages_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(module_id) DO UPDATE SET
            path_prefix = excluded.path_prefix,
            host = excluded.host,
            routes_json = excluded.routes_json,
            error_pages_json = excluded.error_pages_json
        """,
        (
            module_id,
            None if shipped and path_prefix == shipped.default_prefix else path_prefix,
            None if shipped and host == (shipped.host or "") else host,
            None if routes == shipped_routes else json.dumps(routes),
            None if error_pages == shipped_error_pages else json.dumps(error_pages),
        ),
    )


def reset_module_overrides(module_id: str) -> None:
    execute("DELETE FROM module_overrides WHERE module_id = ?", (module_id,))
