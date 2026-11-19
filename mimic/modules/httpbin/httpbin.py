"""The worked example of the hybrid tier: every route is declared in module.json,
and every one of them names a `handler` method here.
"""

import json
import logging
import time

from flask import Response, request
from uuid import uuid4

from mimic.modules.base import MimicModule

log = logging.getLogger(__name__)

MAX_DELAY_SECONDS = 10


class HttpBinModule(MimicModule):
    def echo(self) -> Response:
        return Response(json.dumps({
            "method": request.method,
            "url": request.url,
            "base_url": request.base_url,
            "path": request.path,
            "headers": dict(request.headers),
            "args": request.args.to_dict(flat=False),
            "form": request.form.to_dict(flat=False),
            "files": {
                k: v.filename
                for k, v in request.files.items()
            },
            "cookies": request.cookies,
            "json": request.get_json(silent=True),
            "data": request.get_data(as_text=True),
            "origin": request.remote_addr,
        }, indent=4), mimetype='application/json')

    def status(self, code: int) -> Response:
        return Response("", status=code)

    def delay(self, seconds: int) -> Response:
        # Unauthenticated endpoint: cap the sleep so it cannot hold a thread forever.
        time.sleep(min(max(seconds, 0), MAX_DELAY_SECONDS))
        return self.echo()

    def uuid(self) -> Response:
        return Response(json.dumps({"uuid": str(uuid4())}, indent=4), mimetype='application/json')

    def root(self) -> Response:
        # TODO: Make this a page listing routes?
        return Response("", status=200)
