from __future__ import annotations

from dataclasses import replace

import broken_modules

from mimic.modules.base import MimicModule, ModuleSpec
from mimic.modules.discovery import iter_discovered_specs
from mimic.modules.registry import EffectiveModule, load_module
from mimic.modules.schema import validate


def _discover_fixtures():
    return iter_discovered_specs(broken_modules)


def test_broken_package_does_not_stop_the_others():
    specs, errors = _discover_fixtures()
    assert "good" in {s.module_id for s in specs}
    assert any("boom" in e and "import failed" in e for e in errors)


def test_duplicate_module_id_is_recorded_not_raised():
    specs, errors = _discover_fixtures()
    assert [s.module_id for s in specs].count("dupe") == 1
    assert any("duplicate module_id" in e for e in errors)


def test_malformed_manifest_is_reported_and_loads_nothing(monkeypatch):
    from mimic.modules import registry

    monkeypatch.setattr(registry, "modules_pkg", broken_modules)
    effective = load_module("bad_manifest")
    assert effective.manifest is None
    assert any("could not be read" in e for e in effective.errors)


class _Exploding(MimicModule):
    def build_blueprints(self, url_prefix, manifest):
        raise RuntimeError("module is broken")


def _spec(module_id, module=None):
    return ModuleSpec(module_id=module_id, module=module or MimicModule(module_id))


def _manifests(monkeypatch, overrides=None):
    """Give the synthetic specs manifests without putting packages on disk."""
    overrides = overrides or {}
    from mimic.modules import loader

    def fake(module_id: str) -> EffectiveModule:
        if module_id in overrides:
            return overrides[module_id]
        manifest, _ = validate({"default_prefix": f"/{module_id}", "default_enabled": True})
        return EffectiveModule(module_id, manifest, manifest)

    monkeypatch.setattr(loader, "load_module", fake)


def test_build_blueprints_failure_isolates_one_module(monkeypatch):
    from mimic.modules import loader

    specs = [_spec("bad", _Exploding("bad")), _spec("ok")]
    monkeypatch.setattr(loader, "iter_discovered_specs", lambda: (specs, []))
    # kind defaults to "manifest", so the exploding override needs to declare itself.
    custom, _ = validate({"default_prefix": "/bad", "default_enabled": True, "kind": "custom"})
    _manifests(monkeypatch, {"bad": EffectiveModule("bad", custom, custom)})

    result = loader.build_modules()
    assert any(e.startswith("bad: failed to build blueprints") for e in result.errors)
    assert not any(e.startswith("ok") for e in result.errors)


def test_unreadable_manifest_isolates_one_module(monkeypatch):
    from mimic.modules import loader

    specs = [_spec("bad"), _spec("ok")]
    monkeypatch.setattr(loader, "iter_discovered_specs", lambda: (specs, []))
    broken = EffectiveModule("bad", None, None, errors=["module.json is missing"])
    _manifests(monkeypatch, {"bad": broken})

    result = loader.build_modules()
    assert "bad: module.json is missing" in result.errors
    assert [m.id for m in result.loaded] == ["ok"]


def test_other_modules_keep_serving_when_one_module_is_broken(app, monkeypatch):
    from mimic.modules import loader

    specs, _ = iter_discovered_specs()
    patched = [
        replace(s, module=_Exploding(s.module_id)) if s.module_id == "docker_registry" else s
        for s in specs
    ]
    monkeypatch.setattr(loader, "iter_discovered_specs", lambda: (patched, []))

    registry = app.extensions["mimic_module_registry"]
    assert registry.refresh() is True
    prefixes = {prefix for prefix, _, _ in registry.get_pairs()}
    assert "/v2" not in prefixes
    assert "/httpbin" in prefixes
