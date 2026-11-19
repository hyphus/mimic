"""Application settings.

Two tiers, deliberately not merged:

- **Startup settings** come from the process environment, then `$MIMIC_DATA_DIR/.env`,
  then a built-in default, and are immutable for the process lifetime. They decide
  where state lives and how it is signed, so the UI may not change them.
- **Runtime settings** live in the `settings` table and are owned by the admin UI.

Loading is pure: nothing here mutates `os.environ`, and the only file written is
the session key persisted by `ensure_secret_key()`.
"""

from __future__ import annotations

import logging
import os
import secrets

from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, set_key

log = logging.getLogger(__name__)

DEFAULT_DATA_DIR = "data"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_PUBLIC_BIND = "127.0.0.1:8080"
DASHBOARD_BUFFER_SIZE = 250
MAX_BODY_CHARS = 512 * 1024
BUSY_TIMEOUT_SECONDS = 5.0

_TRUTHY = ("1", "true", "t", "yes", "on")


def _as_bool(value: object) -> bool:
    return str(value).strip().lower() in _TRUTHY


def _restrict_permissions(path: Path) -> None:
    """Owner-only. These files hold the AWS secret, the admin hash and the session key."""
    try:
        path.chmod(0o600)
    except OSError as e:
        log.warning(f"Could not restrict permissions on {path}: {e}")


def data_dir() -> Path:
    """Where all state lives. Environment-only, and created if missing."""
    path = Path(os.environ.get("MIMIC_DATA_DIR") or DEFAULT_DATA_DIR).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def env_path() -> Path:
    return data_dir() / ".env"


def _env() -> dict[str, str | None]:
    """The startup environment. Real environment variables win over `.env`."""
    return {**dotenv_values(env_path()), **os.environ}


def database_path() -> Path:
    path = data_dir() / "mimic.sqlite3"
    path.touch(mode=0o600, exist_ok=True)
    _restrict_permissions(path)
    return path


@dataclass(frozen=True)
class ServerSettings:
    data_dir: Path
    secret_key: str
    gunicorn_reload_hup: bool


@dataclass(frozen=True)
class LoggingSettings:
    level: str
    dashboard_buffer_size: int
    max_body_chars: int


@dataclass(frozen=True)
class DatabaseSettings:
    path: Path
    busy_timeout: float


@dataclass(frozen=True)
class BindSettings:
    """Socket addresses (host:port) the server listens on.

    Consumed by the server entry points (gunicorn.conf.py, mimic.dev), not by
    create_app(): the dev server serves one socket and never gates /admin.
    """

    public: str
    admin: str | None


@dataclass(frozen=True)
class Settings:
    """Startup-only settings. Built once in create_app()."""

    server: ServerSettings
    logging: LoggingSettings
    database: DatabaseSettings


@dataclass(frozen=True)
class AwsSettings:
    use_iam_role: bool = False
    access_key_id: str | None = None
    secret_access_key: str | None = None
    target_host: str = ""


@dataclass(frozen=True)
class FeatureSettings:
    create_dns_records: bool = False
    create_certificates: bool = False


@dataclass(frozen=True)
class RuntimeSettings:
    """Admin-owned settings. Re-read from SQLite on access, never cached."""

    aws: AwsSettings = AwsSettings()
    features: FeatureSettings = FeatureSettings()


def load_server_settings() -> ServerSettings:
    env = _env()
    return ServerSettings(
        data_dir=data_dir(),
        secret_key=env.get("MIMIC_SECRET_KEY") or "",
        gunicorn_reload_hup=_as_bool(env.get("MIMIC_GUNICORN_RELOAD_HUP")),
    )


def load_logging_settings() -> LoggingSettings:
    level = (_env().get("MIMIC_LOG_LEVEL") or DEFAULT_LOG_LEVEL).upper()
    if level not in logging.getLevelNamesMapping():
        log.warning(f"Unknown MIMIC_LOG_LEVEL {level!r}, falling back to {DEFAULT_LOG_LEVEL}")
        level = DEFAULT_LOG_LEVEL
    return LoggingSettings(
        level=level,
        dashboard_buffer_size=DASHBOARD_BUFFER_SIZE,
        max_body_chars=MAX_BODY_CHARS,
    )


def load_database_settings() -> DatabaseSettings:
    return DatabaseSettings(path=database_path(), busy_timeout=BUSY_TIMEOUT_SECONDS)


def load_bind_settings() -> BindSettings:
    env = _env()
    return BindSettings(
        public=env.get("MIMIC_PUBLIC_BIND") or DEFAULT_PUBLIC_BIND,
        admin=env.get("MIMIC_ADMIN_BIND") or None,
    )


def le_staging() -> bool:
    """Let's Encrypt staging directory. Environment-only."""
    return _as_bool(_env().get("MIMIC_USE_LE_STAGING"))


def split_bind(bind: str) -> tuple[str, int]:
    """Split a host:port bind address. A bare port binds to all interfaces."""
    host, _, port = bind.rpartition(":")
    return (host or "0.0.0.0"), int(port)


def load_settings() -> Settings:
    return Settings(
        server=load_server_settings(),
        logging=load_logging_settings(),
        database=load_database_settings(),
    )


def ensure_secret_key() -> str:
    """Generate and persist the session key on first boot.

    The only settings value the app writes. It is kept out of the loaders so the
    write is visible at the call site instead of hiding inside a getter.
    """
    key = _env().get("MIMIC_SECRET_KEY")
    if key:
        return key
    key = secrets.token_hex(32)
    set_key(env_path(), "MIMIC_SECRET_KEY", key)
    _restrict_permissions(env_path())
    log.info("Generated a new session secret key")
    return key


def load_runtime_settings() -> RuntimeSettings:
    # Local import: db resolves its path through this module.
    from mimic.db import query_one

    row = query_one("SELECT * FROM settings WHERE id = 1")
    if row is None:
        return RuntimeSettings()
    return RuntimeSettings(
        aws=AwsSettings(
            use_iam_role=bool(row["use_iam_role"]),
            access_key_id=row["aws_access_key_id"],
            secret_access_key=row["aws_secret_access_key"],
            target_host=(row["target_host"] or "").lower(),
        ),
        features=FeatureSettings(
            create_dns_records=bool(row["create_dns_records"]),
            create_certificates=bool(row["create_certificates"]),
        ),
    )


def save_runtime_settings(runtime: RuntimeSettings) -> None:
    """UPSERT the single row."""
    from mimic.db import execute

    execute(
        """
        INSERT OR REPLACE INTO settings (
            id,
            use_iam_role,
            aws_access_key_id,
            aws_secret_access_key,
            target_host,
            create_dns_records,
            create_certificates
        ) VALUES (1, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(runtime.aws.use_iam_role),
            runtime.aws.access_key_id,
            runtime.aws.secret_access_key,
            runtime.aws.target_host.lower(),
            int(runtime.features.create_dns_records),
            int(runtime.features.create_certificates),
        ),
    )
