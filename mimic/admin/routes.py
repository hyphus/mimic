"""Password-protected management UI."""

from __future__ import annotations

import json
import logging
import secrets

from functools import wraps
from typing import Any

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from markupsafe import Markup, escape

from mimic.auth import (
    login_retry_after,
    register_login_failure,
    register_login_success,
    verify_admin_password,
)
from mimic.aws import validate_aws_credentials
from mimic.events import stream
from mimic.httplog.setup import get_dashboard_log_entries
from mimic.httplog.store import (
    clear_logs,
    count_logs,
    fetch_logs,
    fetch_logs_after,
    log_row_to_json,
)
from mimic.modules.discovery import specs_by_module_id
from mimic.modules.registry import (
    list_modules,
    module_ids,
    reset_module_overrides,
    save_module_overrides,
    set_module_enabled,
)
from mimic.settings import (
    AwsSettings,
    FeatureSettings,
    RuntimeSettings,
    load_runtime_settings,
    save_runtime_settings,
)

log = logging.getLogger(__name__)

admin_bp = Blueprint(
    "admin",
    __name__,
    url_prefix="/admin",
    static_folder="static",
    template_folder="templates",
)


def _routes_drafts() -> dict[str, str]:
    """Unsaved routes JSON, kept in the session so a syntax error is not lost."""
    drafts = session.get("module_routes_drafts")
    return drafts if isinstance(drafts, dict) else {}


def _set_routes_draft(module_id: str, text: str) -> None:
    drafts = _routes_drafts()
    drafts[module_id] = text
    session["module_routes_drafts"] = drafts


def _clear_routes_draft(module_id: str) -> None:
    drafts = _routes_drafts()
    drafts.pop(module_id, None)
    session["module_routes_drafts"] = drafts


def _error_pages_drafts() -> dict[str, str]:
    """Unsaved error-pages JSON, kept in the session so a syntax error is not lost."""
    drafts = session.get("module_error_pages_drafts")
    return drafts if isinstance(drafts, dict) else {}


def _set_error_pages_draft(module_id: str, text: str) -> None:
    drafts = _error_pages_drafts()
    drafts[module_id] = text
    session["module_error_pages_drafts"] = drafts


def _clear_error_pages_draft(module_id: str) -> None:
    drafts = _error_pages_drafts()
    drafts.pop(module_id, None)
    session["module_error_pages_drafts"] = drafts


# Modules need to be reloaded for some settings changes
def _needs_module_reload(current: RuntimeSettings, updated: RuntimeSettings) -> bool:
    return (
        current.features.create_dns_records,
        current.features.create_certificates,
        current.aws.target_host,
    ) != (
        updated.features.create_dns_records,
        updated.features.create_certificates,
        updated.aws.target_host,
    )


def _reload_modules() -> bool:
    registry = current_app.extensions.get("mimic_module_registry")
    return registry is not None and registry.refresh()


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@admin_bp.app_template_global("csrf_input")
def csrf_input() -> Markup:
    return Markup(f'<input type="hidden" name="csrf_token" value="{escape(_csrf_token())}">')


@admin_bp.before_request
def require_csrf_token() -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        # Mint it here so it is in the session before any form is rendered.
        _csrf_token()
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("csrf_token") or ""
    if not expected or not secrets.compare_digest(sent.encode(), expected.encode()):
        abort(400, description="Invalid or missing CSRF token")


# Applied to every admin blueprint response (pages, redirects and static files).
# Scripts stay same-origin only, so the inline handlers that used to live in the
# templates moved to admin/static/js; inline styles are still allowed because the
# pages use style attributes and <style> blocks for layout.
ADMIN_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


@admin_bp.after_request
def set_security_headers(response: Response) -> Response:
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = ADMIN_CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def login_required(view):
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        if not session.get("mimic_admin"):
            nxt = request.path
            if request.query_string:
                nxt += "?" + request.query_string.decode("utf-8")
            return redirect(url_for("admin.login", next=nxt))
        return view(*args, **kwargs)

    return wrapped


@admin_bp.route("/login", methods=["GET", "POST"])
def login() -> Any:
    if session.get("mimic_admin"):
        return redirect(url_for("admin.dashboard"))
    err = None
    # The admin account is a singleton and the endpoint is unauthenticated, so
    # throttle by client address before doing any password check. Failures cost
    # an expensive scrypt/pbkdf2 verification each; the lockout bounds both the
    # guess rate and the CPU a single client can spend in the gthread pool.
    client = request.remote_addr or ""
    if request.method == "POST":
        retry_after = login_retry_after(client)
        if retry_after > 0:
            response = make_response(
                render_template("admin/login.html", error="Too many failed attempts. Try again later."),
                429,
            )
            response.headers["Retry-After"] = str(int(retry_after) + 1)
            return response
        admin_pass = (request.form.get("password") or "").strip()
        if verify_admin_password(admin_pass):
            register_login_success(client)
            # A fresh session rotates the CSRF token at the privilege change.
            session.clear()
            session["mimic_admin"] = True
            nxt = request.args.get("next") or ""
            # Only same-site admin paths; this also rules out //host and /\host.
            if not (nxt == "/admin" or nxt.startswith("/admin/")):
                nxt = url_for("admin.dashboard")
            return redirect(nxt)
        register_login_failure(client)
        err = "Invalid password."
    return render_template("admin/login.html", error=err)


@admin_bp.route("/logout", methods=["POST"])
@login_required
def logout() -> Any:
    session.pop("mimic_admin", None)
    flash("Signed out.", "info")
    return redirect(url_for("admin.login"))


@admin_bp.route("/")
@login_required
def dashboard() -> Any:
    load_info = current_app.config.get("MIMIC_MODULE_LOAD", {})
    modules = list_modules()
    routes_drafts = _routes_drafts()
    error_pages_drafts = _error_pages_drafts()
    for mid, entry in modules.items():
        entry["routes_text"] = routes_drafts.get(mid, entry["routes_json"])
        entry["error_pages_text"] = error_pages_drafts.get(mid, entry["error_pages_json"])

    return render_template(
        "admin/dashboard.html",
        modules=modules,
        load_errors=load_info.get("errors", []),
        loaded_ids={row["id"] for row in load_info.get("loaded", [])},
        expanded=request.args.get("expand"),
        runtime=load_runtime_settings(),
    )


@admin_bp.route("/modules/<module_id>/toggle", methods=["POST"])
@login_required
def module_toggle(module_id: str) -> Any:
    enabled = request.form.get("enabled") == "1"
    if module_id not in specs_by_module_id():
        log.error(f"Unknown module {module_id!r}.")
        return redirect(url_for("admin.dashboard"))

    set_module_enabled(module_id, enabled)
    _reload_modules()

    log.info(f"Module {module_id!r} {'enabled' if enabled else 'disabled'}.")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/modules/<module_id>/config", methods=["POST"])
@login_required
def module_config_save(module_id: str) -> Any:
    """Store the overrides for one module. Values matching module.json clear theirs."""
    if module_id not in specs_by_module_id():
        log.error(f"Unknown module {module_id!r}.")
        return redirect(url_for("admin.dashboard"))

    raw_routes = request.form.get("routes_json") or "[]"
    raw_error_pages = request.form.get("error_pages_json") or "{}"

    try:
        routes = json.loads(raw_routes)
        routes_ok = isinstance(routes, list)
        if not routes_ok:
            flash("Failed to save config, routes must be a JSON array", "error")
    except json.JSONDecodeError as exc:
        routes = None
        routes_ok = False
        flash("Failed to save config, invalid JSON", "error")
        log.error(f"Invalid routes JSON: {exc}")

    try:
        error_pages = json.loads(raw_error_pages)
        pages_ok = isinstance(error_pages, dict)
        if not pages_ok:
            flash("Failed to save config, error pages must be a JSON object", "error")
    except json.JSONDecodeError as exc:
        error_pages = None
        pages_ok = False
        flash("Failed to save config, invalid JSON", "error")
        log.error(f"Invalid error pages JSON: {exc}")

    if not (routes_ok and pages_ok):
        # Preserve whatever the operator typed so a failed save loses nothing.
        _set_routes_draft(module_id, raw_routes)
        _set_error_pages_draft(module_id, raw_error_pages)
        return redirect(url_for("admin.dashboard", expand=module_id))

    save_module_overrides(
        module_id,
        path_prefix=(request.form.get("path_prefix") or "").strip(),
        host=(request.form.get("host") or "").strip(),
        routes=routes,
        error_pages=error_pages,
    )

    _clear_routes_draft(module_id)
    _clear_error_pages_draft(module_id)
    if _reload_modules():
        log.info(f"Config saved and module {module_id!r} reloaded.")
    else:
        log.error(f"Config saved for {module_id!r}, but reload failed; previous routes kept.")

    flash("Config saved")
    return redirect(url_for("admin.dashboard", expand=module_id))


@admin_bp.route("/modules/<module_id>/reset", methods=["POST"])
@login_required
def module_reset(module_id: str) -> Any:
    """Drop every override so the module goes back to what module.json ships."""
    if module_id not in specs_by_module_id():
        log.error(f"Unknown module {module_id!r}.")
        return redirect(url_for("admin.dashboard"))

    reset_module_overrides(module_id)
    _clear_routes_draft(module_id)
    _reload_modules()

    flash(f"Module {module_id!r} reset to default configuration", "info")
    return redirect(url_for("admin.dashboard", expand=module_id))


@admin_bp.route("/logs")
@login_required
def logs() -> Any:
    page = max(1, int(request.args.get("page") or 1))
    per_page = min(100, max(10, int(request.args.get("per") or 50)))
    offset = (page - 1) * per_page
    # Get filter parameters
    module_filter = request.args.get("module")

    # Only the total matters here; the rows themselves are fetched client-side
    # from /admin/logs/rows.
    _rows, total = fetch_logs(limit=per_page, offset=offset, module_id=module_filter)

    pages = (total + per_page - 1) // per_page if total else 1
    # Get all unique module IDs for filter dropdown
    unique_modules = module_ids()

    return render_template(
        "admin/logs.html",
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
        module_filter=module_filter,
        unique_modules=unique_modules,
    )


@admin_bp.get("/logs/rows")
@login_required
def logs_rows() -> Any:
    """JSON: one page of log rows for the client-side rendered log table."""
    page = max(1, int(request.args.get("page") or 1))
    per_page = min(100, max(10, int(request.args.get("per") or 50)))
    offset = (page - 1) * per_page
    module_filter = request.args.get("module")

    rows, total = fetch_logs(limit=per_page, offset=offset, module_id=module_filter)
    return jsonify(
        {
            "rows": [log_row_to_json(r) for r in rows],
            "total": total,
        }
    )


@admin_bp.get("/logs/poll")
@login_required
def logs_poll() -> Any:
    """JSON: new log rows after a given id.

    Not polled any more — this is the resync path for a client whose event
    stream fell behind.
    """
    try:
        after_id = int(request.args.get("after_id", "0"))
    except ValueError:
        after_id = 0
    rows = fetch_logs_after(after_id=after_id, limit=50)
    return jsonify(
        {
            "rows": [log_row_to_json(r) for r in rows],
            "total": count_logs(),
        }
    )


@admin_bp.route("/logs/clear", methods=["POST"])
@login_required
def logs_clear() -> Any:
    n = clear_logs()
    flash(f"Cleared {n} log entries.", "info")
    return redirect(url_for("admin.logs"))


@admin_bp.route("/config", methods=["POST"])
@login_required
def config_save() -> Any:
    # Get form data
    use_iam_role = request.form.get("use_iam_role") == "on"
    aws_access_key_id = (request.form.get("aws_access_key_id") or "").strip()
    aws_secret_access_key = request.form.get("aws_secret_access_key", "").strip()
    target_host = (request.form.get("target_host") or "").strip()
    create_dns_records = request.form.get("create_dns_records") == "on"
    create_certificates = request.form.get("create_certificates") == "on"

    # Validation
    if not any([use_iam_role, aws_access_key_id]) and any([create_certificates, create_dns_records]):
        flash("Valid AWS credentials are required to manage DNS and certificates", "error")
        return redirect(url_for("admin.dashboard"))

    if create_dns_records and not target_host:
        flash("Target IP/Hostname required to create DNS records", "error")
        return redirect(url_for("admin.dashboard"))

    if create_certificates and not create_dns_records:
        flash("\"Create DNS Records\" must be enabled to create certificates", "error")
        return redirect(url_for("admin.dashboard"))

    current = load_runtime_settings()

    if use_iam_role:
        # Role-based credentials must not carry a stored static secret forward:
        # erase it rather than retaining it, so the saved row is genuinely empty.
        if current.aws.secret_access_key:
            flash("Stored AWS secret cleared because the IAM role is now in use.", "info")
        aws_secret_access_key = None
    elif not aws_secret_access_key:
        # A blank secret field means "leave the stored one alone".
        aws_secret_access_key = current.aws.secret_access_key or ""

    # Validate AWS creds
    if use_iam_role or aws_access_key_id:
        if not validate_aws_credentials(key=aws_access_key_id, secret=aws_secret_access_key or ""):
            return redirect(url_for("admin.dashboard"))

    updated = RuntimeSettings(
        aws=AwsSettings(
            use_iam_role=use_iam_role,
            access_key_id=aws_access_key_id or None,
            secret_access_key=aws_secret_access_key,
            target_host=target_host,
        ),
        features=FeatureSettings(
            create_dns_records=create_dns_records,
            create_certificates=create_certificates,
        ),
    )

    save_runtime_settings(updated)
    log.info("Configuration saved")

    if _needs_module_reload(current, updated):
        log.info("Reloading modules")
        _reload_modules()

    return redirect(url_for("admin.dashboard"))


@admin_bp.get("/events")
@login_required
def events() -> Any:
    """One SSE stream per tab, carrying both `applog` and `httplog` events.

    The dashboard's app-log backlog is replayed on connect so a fresh tab is
    populated immediately.
    """
    backlog = ((("applog"), entry) for entry in get_dashboard_log_entries())
    return Response(
        stream_with_context(stream(backlog)),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@admin_bp.post("/logs/append")
@login_required
def logs_append() -> Any:
    """Programmatic endpoint to push a message into the dashboard log."""
    raw = request.get_json(silent=True) or {}
    message = raw.get("message", "")
    if not message:
        return jsonify({"error": "Missing 'message' field"}), 400
    log.info(message)
    return jsonify({"ok": True})
