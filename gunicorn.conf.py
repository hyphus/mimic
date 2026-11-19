# Used to configure gunicorn

import os
import sys

from pathlib import Path
from dotenv import load_dotenv

from mimic.settings import load_bind_settings
from mimic.tls import ensure_certificate


data_dir = Path(os.getenv("MIMIC_DATA_DIR", "data"))
load_dotenv(data_dir / ".env")

binds = load_bind_settings()
# Two sockets only when an admin bind is configured; the gate in mimic.server keeps
# /admin off the public one and everything but /admin and /healthz off the admin one.
bind = [binds.public] + ([binds.admin] if binds.admin else [])

# Exactly one worker, always. ModuleRuntimeRegistry lives in-process, so a second
# worker would serve a stale module registry after any admin toggle. Concurrency
# comes from threads instead.
workers = 1

# The sync worker sends its arbiter heartbeat around inline request handling, so a
# long-lived request (the admin SSE stream) starves it and the worker is reaped.
# gthread hands requests to a thread pool and heartbeats from its own accept loop.
worker_class = "gthread"
threads = 8

# log to stdout
accesslog = os.getenv("GUNICORN_ACCESS_LOG", "-")
errorlog = os.getenv("GUNICORN_ERROR_LOG", "-")

# HTTPS is the default: a self-signed cert is generated on first boot, and a
# Let's Encrypt cert (same files) overrides it once issued.
ensure_certificate()
cert_dir = data_dir / "certs"
keyfile = str(cert_dir / "certificate.key")
certfile = str(cert_dir / "fullchain.pem")


def ssl_context(conf, default_ssl_context_factory):
    # gunicorn 23 ignores the old `ssl_version` setting; this hook is how the
    # minimum TLS version (GUNICORN_TLS_VERSION, default TLSv1_2) is applied.
    import ssl

    context = default_ssl_context_factory()
    version = os.getenv("GUNICORN_TLS_VERSION", "TLSv1_2")
    minimum = getattr(ssl.TLSVersion, version, None)
    if minimum is None:
        print(f"Unknown GUNICORN_TLS_VERSION {version!r}, falling back to TLSv1_2", file=sys.stderr)
        minimum = ssl.TLSVersion.TLSv1_2
    context.minimum_version = minimum
    return context

# The () makes gunicorn call the factory; a bare name would be used as the WSGI
# app itself and never invoked.
wsgi_app = "mimic.server:load_app()"
