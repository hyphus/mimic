"""Copy this directory to mimic/modules/<name>/ to start a new module.

Discovery skips packages whose name starts with an underscore, so this one
never loads. Everything the app needs is in module.json; the class only exists
for routes that declare a `handler`.
"""

from flask import Response, request

from mimic.modules.base import MimicModule, ModuleSpec


class TemplateModule(MimicModule):
    def on_echo(self) -> Response:
        """Named by the "echo" route's `handler` key in module.json."""
        return Response(f"{request.method} {request.path}\n", mimetype="text/plain")


MODULE_SPEC = ModuleSpec(module_id="_template", module=TemplateModule("_template"))

__all__ = ["MODULE_SPEC"]
