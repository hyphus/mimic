<p align="center">
  <img src="images/mimic.png" alt="Mimic" width="160">
</p>

# Mimic

[![CI](https://github.com/hyphus/mimic/actions/workflows/ci.yml/badge.svg)](https://github.com/hyphus/mimic/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Mimic is a Flask-based mock HTTP server with pluggable modules - a webhook receiver, Docker Registry API stubs, an httpbin clone, static file serving, and whatever else you add. Each module is served from a configurable URL prefix and, by default, declares its routes in a JSON manifest.  

Modules can be enabled, disabled, or reconfigured from a password-protected admin UI that logs all incoming requests and responses.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MIMIC_ADMIN_PASSWORD='hunter2'
PYTHONPATH=. python -m mimic.dev
```

On first boot Mimic creates `data/mimic.sqlite3`, hashes `MIMIC_ADMIN_PASSWORD` into it, and
generates a session signing key into `data/.env`. It also generates a self-signed certificate
into `data/certs/`, so every socket is HTTPS from the start. Nothing else is required.

- **Admin**: <https://127.0.0.1:8080/admin> (self-signed until a Let's Encrypt cert is issued)
- **Health**: `GET /healthz`
- **Registry stub**: `GET /v2/`, `GET /v2/_catalog`, tags and manifest routes, minimal blob/upload behaviour
- **Webhook**: any verb on `/webhook/` and `/webhook/<subpath>`, with configurable JSON responses
- **httpbin**: `/httpbin/get`, `/httpbin/post`, `/httpbin/status/<code>`, `/httpbin/delay/<seconds>`, `/httpbin/uuid`
- **Static**: `GET /static/` serves files from the module's `www/` directory; `/static/download` is a sample file download

All four shipped modules are disabled by default (`default_enabled: false`); enable each one in the
admin UI before its endpoints respond.

## How it works

`create_app()` builds one main Flask app for `/admin` and `/healthz`, then builds a **separate Flask
sub-app per loaded module**. A WSGI middleware in front of the main app dispatches by path prefix,
longest match first, and falls through to the main app when nothing matches. That is what lets each
module have its own error handlers, its own `host_matching` setting, and its own concurrency cap
without any of them interfering.

The module registry lives in memory and can be rebuilt at runtime (`refresh()`), which is why the
server must run as a single worker — see [Deployment](#deployment).

## Configuration

Two tiers, deliberately not merged.

**Startup settings** decide where state lives and how sessions are signed, so the UI may not change
them. They are read once at boot from the process environment, then `$MIMIC_DATA_DIR/.env`, then a
built-in default — first one wins:

| Precedence | Source |
| --- | --- |
| 1 | process environment |
| 2 | `$MIMIC_DATA_DIR/.env` |
| 3 | built-in default |

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MIMIC_DATA_DIR` | `./data` | Directory for `.env`, `mimic.sqlite3`, and certificates. |
| `MIMIC_ADMIN_PASSWORD` | — | Bootstrap password, hashed into the database on first boot. |
| `MIMIC_SECRET_KEY` | generated | Flask session signing. Written to `.env` on first boot if unset. |
| `MIMIC_LOG_LEVEL` | `INFO` | Root log level. An unknown value falls back to `INFO` with a warning. |
| `MIMIC_PUBLIC_BIND` | `127.0.0.1:8080` | Socket the server listens on, as `host:port`. Read by both Gunicorn and the dev server. |
| `MIMIC_ADMIN_BIND` | off | A second socket for `/admin` and `/healthz` only, also `host:port`. When set, Gunicorn serves both sockets and a port gate keeps `/admin` off the public one. Its port must differ from the public port. The dev server serves one socket, so it refuses to start when this is set and `MIMIC_PUBLIC_BIND` is not loopback. |
| `MIMIC_GUNICORN_RELOAD_HUP` | off | If truthy, send **SIGHUP** to the Gunicorn master after config changes so all workers reload. On by default in the Docker image, off elsewhere. |
| `MIMIC_HTTP_LOG_MAX_ROWS` | 1000000 | Retention cap for the request log: rows beyond this (oldest first) are pruned as new ones arrive. |
| `MIMIC_HTTP_LOG_MAX_BYTES` | 4294967296 | Same cap in bytes of stored headers and bodies; both bounds apply, whichever is hit first. The defaults are generous on purpose — the point is to keep an unauthenticated client from growing the database without bound, not to limit normal use. |
| `MIMIC_USE_LE_STAGING` | off | Issue Let's Encrypt certificates from the staging directory instead of production — untrusted certificates, but no rate limits. |
| `FLASK_DEBUG` | off | Dev server only; when set to `1`, enables Flask debug mode in `mimic.dev`. |

Gunicorn itself reads `GUNICORN_ACCESS_LOG` and `GUNICORN_ERROR_LOG` (both default to stdout).
`gunicorn.conf.py` maps `GUNICORN_TLS_VERSION` (default `TLSv1_2`) to the minimum TLS version.

Bind values must be `host:port`. Gunicorn does not accept a bare port — a value like `8443` is read
as the *hostname* `8443` on Gunicorn's default port 8000, and the master exits with
`Invalid address: ('8443', 8000)`.

**Runtime settings** are owned by the admin **Configuration** panel and live in the `settings` table
in SQLite. They are re-read on access and are *not* overridable from the environment:

| Field | Purpose |
| --- | --- |
| Use IAM role | Authenticate to AWS with the ambient role instead of a stored key pair. |
| AWS access key ID / secret access key | Used when *Use IAM role* is off. A blank secret field leaves the stored value alone. |
| Target IP/Hostname | What DNS records for module hosts point at. |
| Create DNS Records | Create a Route 53 record for every module that declares a `host`. |
| Create Certificates | Obtain a Let's Encrypt certificate covering those hosts. Requires *Create DNS Records*. |

Saving credentials validates them with `sts:GetCallerIdentity` before storing.

### AWS, Route 53, and Let's Encrypt

Both features are **off by default** and Mimic makes no AWS calls until you turn them on.

With **Create DNS Records** on, every module that declares a `host` gets a Route 53 record pointing
at the configured target (an `A` record if the target is an IP, otherwise a `CNAME`) before it is
mounted. A module whose record cannot be created is not loaded, and the failure appears in the
dashboard's error list.

With **Create Certificates** on as well, Mimic runs an ACME DNS-01 order for the set of module hosts
in a background thread: it writes `_acme-challenge` TXT records, waits for them to propagate,
answers the challenges, and always deletes the challenge records afterwards. The result is written
to `$MIMIC_DATA_DIR/certs/` as `certificate.key` and `fullchain.pem`, alongside the reused ACME
account key and registration URI — replacing the self-signed fallback in the same files.
`gunicorn.conf.py` picks those files up on the next boot, and if
`MIMIC_GUNICORN_RELOAD_HUP` is set Mimic signals the master so the reload happens immediately.

The IAM permissions required are:

| Action | Why |
| --- | --- |
| `sts:GetCallerIdentity` | credential validation in the Configuration panel |
| `route53:ListHostedZones` | find the most specific zone for a host |
| `route53:ListResourceRecordSets` | skip records that already point where they should |
| `route53:ChangeResourceRecordSets` | create module records and ACME challenge records |

### Security notes

- **Every socket is HTTPS.** On first boot Mimic generates a self-signed certificate in
  `$MIMIC_DATA_DIR/certs/`, so there is no plain-HTTP listener. Clients must use `https://` and
  accept the untrusted cert (e.g. `curl -k`) until a Let's Encrypt certificate is issued, which
  overrides it.
- **AWS credentials are stored unencrypted.** An access key and secret entered in the Configuration
  panel are written in plaintext to `mimic.sqlite3`. Prefer an **IAM role** (instance profile, ECS
  task role, or IRSA) and tick *Use IAM role* — then no long-lived secret is stored at all. Switching
  to *Use IAM role* clears any previously stored key pair. That only removes the copy in
  `mimic.sqlite3`; the key itself is **not** rotated or deactivated in AWS, so revoke it in IAM too.
- `mimic.sqlite3` and `.env` under `MIMIC_DATA_DIR` are created `0600`. They hold the AWS secret,
  the admin password hash, and the session signing key. Keep the data volume off shared storage.
- `MIMIC_ADMIN_PASSWORD` is read **once**, at first boot, and hashed into the database. It is never
  written to `.env`. Afterwards the variable is ignored; to set a new password, clear the `admin`
  table and restart.
- **Admin login is rate-limited.** Failed `POST /admin/login` attempts are counted both per client
  address and account-wide. A client is locked out with escalating backoff (a `429` plus
  `Retry-After`) after a handful of failures within the window; because a source address can be
  rotated, a separate account-wide cap locks out all clients after more failures, so total guesses
  stay bounded no matter how many addresses an attacker uses. A successful login clears both
  counters. The lockout bounds the guess rate and the CPU spent on the expensive password hash.
  When a reverse proxy fronts the admin socket, also rate-limit there — Mimic keys on the socket
  peer address and does not trust forwarded headers.
- Mimic is a mock server. The modules answer unauthenticated requests by design — do not expose it
  to the internet with anything real behind it.

## Modules

Each module is a package under `mimic/modules/<name>/` with a `module.json` manifest and a
`MODULE_SPEC`. Discovery finds them at startup; packages whose name starts with an underscore are
skipped, which is how `_template` stays out of the way.

**`module.json` on disk is the source of truth** and is re-read on every boot, so editing a manifest
and restarting is all it takes to change a module. The admin dashboard can override `enabled`,
`path_prefix`, `host` and `routes` per module; those overrides are the only module state in the
database. Editing a value back to what the manifest ships clears the override, and **Reset** clears
all of them — so "overridden" on the dashboard always means "disk says something else".

A module is either `kind: "manifest"` (the loader registers the manifest's `routes`, individual
routes may name a `handler` method for real Python) or `kind: "custom"` (it overrides
`build_blueprints` and declares no routes). All four shipped modules are `manifest`; `custom` is
the escape hatch for a route table that a list cannot express.

| Module | `default_prefix` | Routes |
| --- | --- | --- |
| `docker_registry` | `/v2/` | canned responses |
| `webhook` | `/webhook/` | canned responses |
| `httpbin` | `/httpbin/` | every route names a `handler` — the hybrid worked example |
| `static` | `/static/` | serves files from the module's `www/` directory, plus a sample download |

Two enabled modules may not claim overlapping prefixes. The first to claim one wins; the other is
skipped with a message on the dashboard.

**[docs/modules.md](docs/modules.md) is the full guide** — the manifest schema, canned `responses`
with `match` conditions and `{variable}` templating, handlers, and how to start from `_template`.

### Host matching

Setting a module's `host` turns on Flask host matching for its sub-app, which means the module
answers **only** requests carrying that `Host` header. It disappears from every other name,
including `localhost` — this is a whole alternative to path-prefix mounting, not a filter layered on
top of it. Leave `host` unset unless that is what you want.

### Limits

Each module is capped at `max_concurrent` simultaneous in-flight requests (default 4, set in its
manifest), and the server as a whole at a global cap. Past either, the request is refused with
**503** rather than queued; `/admin` and `/healthz` are outside the module caps and keep answering.
A 503 from a module under load is the cap, not the module.

## Admin UI

`/admin` is password-protected and holds:

- **Modules** — every discovered module with its effective prefix, host, kind and routes; which
  fields are overridden; an enable/disable toggle; a routes editor; **Reset**; and the load-error
  list for anything that failed to load.
- **Configuration** — the runtime settings above.
- **Mimic Logs** — a rolling buffer of the server's own log output, on the dashboard.
- **Request log** (`/admin/logs`) — every HTTP exchange the modules served, with full request and
  response headers and bodies (truncated at 512 KB), filterable by module and paginated. Admin,
  health and favicon requests are not logged. The table is kept under a retention cap — by default the
  newest one million rows or 4 GB of stored bodies, whichever comes first (see `MIMIC_HTTP_LOG_MAX_ROWS`
  and `MIMIC_HTTP_LOG_MAX_BYTES`) — with older rows pruned as new ones arrive, so unauthenticated module
  traffic cannot grow the database without bound.

Both log views update live over a single Server-Sent Events stream at `/admin/events`; there is no
polling timer.

## Docker

The image is published on [Docker Hub](https://hub.docker.com/r/hyphus/mimic):

```bash
docker run --rm -p 8080:8080 -p 127.0.0.1:8443:8443 \
  -e MIMIC_ADMIN_PASSWORD=changeme \
  -v mimic-data:/data \
  hyphus/mimic
```

To build from source instead, run `docker build -t mimic .` and use `mimic` in place of
`hyphus/mimic`.

`MIMIC_DATA_DIR` is `/data` in the image, and `/data` is a volume — that is where the database,
`.env`, and any certificates live. The image also sets `MIMIC_PUBLIC_BIND=0.0.0.0:8080` and
`MIMIC_ADMIN_BIND=0.0.0.0:8443` inside the container; override either with `-e` (values must be
`host:port`). Publish the admin port on loopback only, as above, and reach it over a tunnel.

## Deployment

`gunicorn.conf.py` at the repo root is the deployment configuration, and two of its settings are
load-bearing:

- **`workers = 1`, always.** The module registry and the SSE broker both live in process memory. A
  second worker would serve a stale module registry after any admin change and would only see half
  the log events. Concurrency comes from threads, not processes.
- **`worker_class = "gthread"`, `threads = 8`.** The sync worker sends its arbiter heartbeat around
  inline request handling, so the long-lived admin SSE stream starves it and the worker gets reaped.
  `gthread` hands requests to a thread pool and heartbeats from its own accept loop. `threads` is
  the ceiling on genuinely concurrent requests; SQLite is in WAL mode, which is what makes that
  safe.
- **Two sockets for admin.** Setting `MIMIC_ADMIN_BIND` makes Gunicorn listen on a second socket.
  A WSGI gate then serves `/admin` and `/healthz` only there, and nothing else on that port — the
  public socket answers `/admin` with 404, and a request on any other port is refused. The two
  binds must use different ports, since the gate tells them apart by port. Bind the admin socket to
  loopback or a VPN interface and reach it over a tunnel. Note Gunicorn applies the certificate to every socket, so with TLS
  enabled the admin socket is TLS too.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev setup, tests, and linting.

## License

Mimic is licensed under the [MIT License](LICENSE).
