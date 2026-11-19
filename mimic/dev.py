"""Local development server.

Reads the same bind settings as the gunicorn deployment (MIMIC_PUBLIC_BIND), so local and
production listen on the same address by default. The dev server serves a single socket:
MIMIC_ADMIN_BIND is ignored and /admin stays on the public port.
"""

from __future__ import annotations

import ipaddress
import os
import sys

from mimic.app import create_app
from mimic.settings import load_bind_settings, split_bind
from mimic.tls import cert_paths, ensure_certificate


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def main() -> None:
    binds = load_bind_settings()
    host, port = split_bind(binds.public)
    if binds.admin:
        if not _is_loopback(host):
            sys.exit(
                f"MIMIC_ADMIN_BIND is set but the dev server serves one socket, so /admin would be "
                f"exposed on {host}. Use gunicorn, or bind MIMIC_PUBLIC_BIND to 127.0.0.1"
            )
        print(
            "MIMIC_ADMIN_BIND is set but the dev server serves one socket; "
            "ignoring it and keeping /admin on the public port",
            file=sys.stderr,
        )

    app = create_app()

    kwargs: dict = {"threaded": True, "debug": os.environ.get("FLASK_DEBUG") == "1"}
    # HTTPS is the default; a self-signed cert is generated on first boot.
    ensure_certificate()
    fullchain_path, key_path = cert_paths()
    kwargs["ssl_context"] = (str(fullchain_path), str(key_path))

    app.run(host=host, port=port, **kwargs)


if __name__ == "__main__":
    main()
