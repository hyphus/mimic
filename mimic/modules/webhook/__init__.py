from mimic.modules.base import MimicModule, ModuleSpec

MODULE_SPEC = ModuleSpec(module_id="webhook", module=MimicModule("webhook"))

__all__ = ["MODULE_SPEC"]
