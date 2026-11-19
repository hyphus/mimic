"""Minimal OCI distribution / Docker Registry HTTP API V2 style endpoints."""

from mimic.modules.base import MimicModule, ModuleSpec

MODULE_SPEC = ModuleSpec(module_id="docker_registry", module=MimicModule("docker_registry"))

__all__ = ["MODULE_SPEC"]
