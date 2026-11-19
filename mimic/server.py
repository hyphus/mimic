"""Gunicorn entry point: create_app() plus the port gate when an admin bind is set."""

from __future__ import annotations

from mimic.app import create_app
from mimic.settings import load_bind_settings, split_bind


def load_app():
    app = create_app()
    binds = load_bind_settings()
    if binds.admin:
        from mimic.admin_gate import PortGateMiddleware

        _, public_port = split_bind(binds.public)
        _, admin_port = split_bind(binds.admin)
        # The gate tells the sockets apart by port alone.
        if public_port == admin_port or 0 in (public_port, admin_port):
            raise RuntimeError(
                "MIMIC_PUBLIC_BIND and MIMIC_ADMIN_BIND need distinct, non-zero ports "
                f"(got {public_port} and {admin_port})"
            )
        app.wsgi_app = PortGateMiddleware(app.wsgi_app, public_port, admin_port)
    return app
