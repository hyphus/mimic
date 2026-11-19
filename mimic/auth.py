"""Admin authentication against a password hash persisted in SQLite."""

from __future__ import annotations

import logging
import os
import re

from werkzeug.security import check_password_hash, generate_password_hash

from mimic.db import execute, query_one

log = logging.getLogger(__name__)


def _is_hashed(value: object) -> bool:
    """Only the real Werkzeug format: method$salt$hexdigest."""
    return isinstance(value, str) and re.fullmatch(r"(scrypt|pbkdf2):[^$]+\$[^$]+\$[0-9a-f]+", value) is not None


def _get_password_hash() -> str | None:
    row = query_one("SELECT password_hash FROM admin WHERE id = 1")
    return row["password_hash"] if row else None


def _save_password_hash(password_hash: str) -> None:
    execute(
        "INSERT OR REPLACE INTO admin (id, password_hash) VALUES (1, ?)",
        (password_hash,),
    )


def ensure_admin_creds() -> None:
    """Bootstrap the stored password hash from MIMIC_ADMIN_PASSWORD on first boot.

    MIMIC_ADMIN_PASSWORD is a bootstrap-only input: once a hash is stored it is
    never read again, so the credential no longer round-trips through the
    process environment.
    """
    admin_pass = os.environ.get("MIMIC_ADMIN_PASSWORD")

    if _get_password_hash() is not None:
        if admin_pass:
            log.info(
                "An admin password is already stored; MIMIC_ADMIN_PASSWORD is ignored. "
                "Clear the admin table in mimic.sqlite3 to bootstrap a new one."
            )
        return

    if not admin_pass:
        raise RuntimeError("No admin password configured. Set MIMIC_ADMIN_PASSWORD and restart")

    # A value that is already a Werkzeug hash (a stale .env line) is adopted as-is
    # rather than hashed a second time.
    _save_password_hash(admin_pass if _is_hashed(admin_pass) else generate_password_hash(admin_pass))
    log.info("Stored admin password hash from MIMIC_ADMIN_PASSWORD")


def verify_admin_password(password: str) -> bool:
    stored = _get_password_hash()
    if not stored:
        return False
    return check_password_hash(stored, password)
