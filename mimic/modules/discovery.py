"""Discover ModuleSpec implementations from mimic.modules sub-packages."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from types import ModuleType

import mimic.modules as modules_pkg

from mimic.modules.base import MimicModule, ModuleSpec


def iter_discovered_specs(package: ModuleType = modules_pkg) -> tuple[list[ModuleSpec], list[str]]:
    """
    Each directory under the package with __init__.py may export MODULE_SPEC.

    Returns the usable specs plus one error string per package that could not
    contribute one; a broken package never stops the others from loading.
    """
    specs: list[ModuleSpec] = []
    errors: list[str] = []
    seen_types: dict[str, str] = {}
    pkg_path = Path(package.__file__).parent

    for info in pkgutil.iter_modules([str(pkg_path)]):
        if not info.ispkg:
            continue
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{package.__name__}.{info.name}")
        except Exception as e:
            errors.append(f"{info.name}: import failed: {e!r}")
            continue
        spec = getattr(mod, "MODULE_SPEC", None)
        if spec is None:
            continue
        if not isinstance(spec, ModuleSpec):
            errors.append(f"{info.name}: MODULE_SPEC is not a ModuleSpec")
            continue
        if not isinstance(spec.module, MimicModule):
            errors.append(f"{info.name}: MODULE_SPEC.module is not a MimicModule")
            continue
        if spec.module_id in seen_types:
            errors.append(
                f"{info.name}: duplicate module_id {spec.module_id!r}, already provided by "
                f"{seen_types[spec.module_id]!r}"
            )
            continue
        seen_types[spec.module_id] = info.name
        specs.append(spec)

    return specs, errors


def specs_by_module_id() -> dict[str, ModuleSpec]:
    specs, _ = iter_discovered_specs()
    return {spec.module_id: spec for spec in specs}
