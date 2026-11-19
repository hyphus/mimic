from mimic.modules.base import ModuleSpec
from mimic.modules.httpbin.httpbin import HttpBinModule

MODULE_SPEC = ModuleSpec(module_id="httpbin", module=HttpBinModule("httpbin"))

__all__ = ["MODULE_SPEC"]
