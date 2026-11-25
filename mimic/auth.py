"""Admin authentication against a password hash persisted in SQLite."""

from __future__ import annotations

import logging
import os
import re
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

from mimic.db import execute, query_one

log = logging.getLogger(__name__)

# The login endpoint verifies every submitted password with an expensive
# scrypt/pbkdf2 hash. Without a limit an attacker who can reach the admin socket
# can submit unlimited guesses and occupy the small gthread pool. Two guards are
# combined: a per-client lockout with escalating backoff, and a global
# per-account cap. The admin account is a singleton, so a source address is a
# guess-only detail an attacker can rotate; the global cap bounds total attempts
# no matter how many addresses are used, and the per-client lockout makes a
# single retrying client increasingly expensive to persist with.
LOGIN_FAILURE_WINDOW_SECONDS = 300.0
LOGIN_MAX_FAILURES = 5
LOGIN_GLOBAL_MAX_FAILURES = 30
LOGIN_LOCKOUT_BASE_SECONDS = 30.0
LOGIN_LOCKOUT_MAX_SECONDS = 900.0

# In-process state: Gunicorn runs a single worker (see gunicorn.conf.py), so this
# is process-global. A restart clears it, which only ever loosens the limit.
_login_lock = threading.Lock()
_login_failures: dict[str, list[float]] = {}
# Completed lockouts per client; drives the escalating backoff so a client that
# waits out one lockout does not earn a fresh, equally short one.
_login_strikes: dict[str, int] = {}
_login_locked_until: dict[str, float] = {}
# Global (per-account) counters, independent of source address.
_global_failures: list[float] = []
_global_strikes = 0
_global_locked_until = 0.0


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


def _prune_failures(now: float, attempts: list[float]) -> list[float]:
    cutoff = now - LOGIN_FAILURE_WINDOW_SECONDS
    return [t for t in attempts if t > cutoff]


def _lockout_delay(strikes: int) -> float:
    """Backoff after `strikes` completed lockouts: 30s, 60s, 120s, ... capped."""
    return min(LOGIN_LOCKOUT_BASE_SECONDS * (2 ** strikes), LOGIN_LOCKOUT_MAX_SECONDS)


def _sweep_stale_clients(now: float) -> None:
    """Drop clients with no active lockout and no in-window failures.

    Caller holds `_login_lock`. Strike history is kept so backoff keeps escalating.
    """
    cutoff = now - LOGIN_FAILURE_WINDOW_SECONDS
    for key in [k for k, until in _login_locked_until.items() if until <= now]:
        _login_locked_until.pop(key, None)
    for key in [k for k, ts in _login_failures.items() if not any(t > cutoff for t in ts)]:
        _login_failures.pop(key, None)


def login_retry_after(client: str | None, now: float | None = None) -> float:
    """Seconds the caller must wait before another attempt, or 0 if allowed."""
    global _global_locked_until
    now = time.monotonic() if now is None else now
    key = client or ""
    with _login_lock:
        _global_failures[:] = _prune_failures(now, _global_failures)
        global_retry = _global_locked_until - now if _global_locked_until > now else 0.0
        if _global_locked_until and _global_locked_until <= now:
            _global_locked_until = 0.0

        locked_until = _login_locked_until.get(key, 0.0)
        if locked_until > now:
            return max(locked_until - now, global_retry)
        if locked_until:
            _login_locked_until.pop(key, None)

        attempts = _prune_failures(now, _login_failures.get(key, []))
        if attempts:
            _login_failures[key] = attempts
        else:
            _login_failures.pop(key, None)
    return global_retry


def register_login_failure(client: str | None, now: float | None = None) -> None:
    """Record a failed attempt, locking out the client or the account if needed."""
    global _global_strikes, _global_locked_until
    now = time.monotonic() if now is None else now
    key = client or ""
    with _login_lock:
        _sweep_stale_clients(now)

        _global_failures[:] = _prune_failures(now, _global_failures)
        _global_failures.append(now)
        if len(_global_failures) >= LOGIN_GLOBAL_MAX_FAILURES:
            delay = _lockout_delay(_global_strikes)
            _global_locked_until = now + delay
            _global_strikes += 1
            _global_failures.clear()
            log.warning(
                "Admin login locked out for all clients for %ss after %d failed attempts",
                int(delay),
                LOGIN_GLOBAL_MAX_FAILURES,
            )

        attempts = _prune_failures(now, _login_failures.get(key, []))
        attempts.append(now)
        if len(attempts) >= LOGIN_MAX_FAILURES:
            strikes = _login_strikes.get(key, 0)
            delay = _lockout_delay(strikes)
            _login_locked_until[key] = now + delay
            _login_strikes[key] = strikes + 1
            _login_failures.pop(key, None)
            log.warning("Admin login locked out for %ss after %d failed attempts", int(delay), len(attempts))
        else:
            _login_failures[key] = attempts


def register_login_success(client: str | None) -> None:
    """Clear the client's and the account's failure history after a correct password."""
    global _global_strikes, _global_locked_until
    key = client or ""
    with _login_lock:
        _login_failures.pop(key, None)
        _login_strikes.pop(key, None)
        _login_locked_until.pop(key, None)
        _global_failures.clear()
        _global_strikes = 0
        _global_locked_until = 0.0


def reset_login_throttle_state() -> None:
    """Drop all in-process throttle state. Used by tests; not a runtime control."""
    global _global_strikes, _global_locked_until
    with _login_lock:
        _login_failures.clear()
        _login_strikes.clear()
        _login_locked_until.clear()
        _global_failures.clear()
        _global_strikes = 0
        _global_locked_until = 0.0


def verify_admin_password(password: str) -> bool:
    stored = _get_password_hash()
    if not stored:
        return False
    return check_password_hash(stored, password)
