"""Build Flask blueprints from discovered ModuleSpec implementations."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from flask import Blueprint

from mimic.modules.base import LoadedModule, MimicModule, ModuleSpec
from mimic.modules.discovery import iter_discovered_specs
from mimic.modules.registry import load_module
from mimic.modules.schema import Manifest, resolve_routes

log = logging.getLogger(__name__)

# Served by the main app; a module mounted over them would hijack the admin UI.
RESERVED_PREFIXES = ("/admin", "/healthz")


@dataclass
class ModuleLoadResult:
    loaded: list[LoadedModule] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def normalize_prefix(prefix: str) -> str:
    """Normalize a URL prefix for comparison (leading slash, no trailing slash except root)."""
    if not prefix or prefix == "/":
        return "/"
    p = "/" + prefix.strip().strip("/")
    return p


def prefixes_conflict(a: str, b: str) -> bool:
    """Return True if two enabled modules cannot share these path prefixes."""
    na, nb = normalize_prefix(a), normalize_prefix(b)
    if na == nb:
        return True
    return na.startswith(nb + "/") or nb.startswith(na + "/")


def find_path_conflicts(prefixes: list[tuple[str, str]]) -> list[tuple[str, str, str, str]]:
    """
    prefixes: list of (module_id, normalized_prefix)
    Returns list of (winner_id, winner_prefix, loser_id, loser_prefix) where loser should not load.
    First occurrence in the list wins.
    """
    conflicts: list[tuple[str, str, str, str]] = []
    claimed: list[tuple[str, str]] = []
    for mid, pref in prefixes:
        np = normalize_prefix(pref)
        drop = False
        for wid, wp in claimed:
            if prefixes_conflict(np, wp):
                conflicts.append((wid, wp, mid, np))
                drop = True
                break
        if not drop:
            claimed.append((mid, np))
    return conflicts


def kind_errors(manifest: Manifest, module: MimicModule) -> list[str]:
    """The manifest's declared kind has to agree with what the class actually does."""
    overridden = type(module).build_blueprints is not MimicModule.build_blueprints
    if manifest.kind == "manifest" and overridden:
        return ["declares kind 'manifest' but overrides build_blueprints"]
    if manifest.kind == "custom" and not overridden:
        return ["declares kind 'custom' but inherits the default build_blueprints"]
    return []


def build_modules() -> ModuleLoadResult:
    """
    Build modules from discovered specs and their effective manifests.

    Each module's `module.json` is re-read here, overlaid with whatever the
    admin UI changed. Anything wrong with one module is recorded and skipped;
    the others still load.
    """
    result = ModuleLoadResult()
    candidates: list[tuple[ModuleSpec, Manifest, str]] = []

    specs, discovery_errors = iter_discovered_specs()
    result.errors.extend(discovery_errors)

    for spec in specs:
        effective = load_module(spec.module_id)
        result.errors.extend(f"{spec.module_id}: {e}" for e in effective.errors)
        manifest = effective.manifest
        if manifest is None:
            continue

        problems = kind_errors(manifest, spec.module)
        if problems:
            result.errors.extend(f"{spec.module_id}: {e}" for e in problems)
            continue

        if not manifest.default_enabled:
            result.skipped.append(f"{spec.module_id} (disabled)")
            continue

        prefix = normalize_prefix(manifest.default_prefix)
        reserved = next((r for r in RESERVED_PREFIXES if prefixes_conflict(prefix, r)), None)
        if reserved:
            result.errors.append(f"{spec.module_id}: path prefix {prefix!r} overlaps reserved {reserved!r}; not loaded")
            continue

        _, handler_errors = resolve_routes(manifest, spec.module)
        result.errors.extend(f"{spec.module_id}: {e}" for e in handler_errors)

        candidates.append((spec, manifest, prefix))

    conflict_rows = find_path_conflicts([(s.module_id, p) for s, _, p in candidates])
    losers = {row[2] for row in conflict_rows}
    for wid, wp, lid, lp in conflict_rows:
        result.skipped.append(
            f"{lid} path {lp!r} overlaps with module {wid!r} ({wp!r}); not loaded"
        )

    for spec, manifest, norm_prefix in candidates:
        if spec.module_id in losers:
            continue

        # Set before building so both 'manifest' and 'custom' modules can render
        # their custom error pages from the sub-app's error handler.
        spec.module._error_pages = manifest.error_pages

        collected: list[Blueprint] = []
        try:
            for bp in spec.module.build_blueprints(norm_prefix, manifest):
                collected.append(bp)
            if not collected:
                log.debug(f"Module {spec.module_id!r} produced no blueprints")
                continue
        except Exception as e:
            result.errors.append(f"{spec.module_id}: failed to build blueprints: {e!r}")
            continue

        result.loaded.append(
            LoadedModule(
                id=spec.module_id,
                blueprints=collected,
                path_prefix=norm_prefix,
                host=manifest.host,
                max_concurrent=manifest.max_concurrent,
                module=spec.module,
            )
        )

    return result
