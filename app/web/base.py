"""Every request: the sign-in check, the form token, security headers, the database; what every page is
given; the activity log; reaching Odoo."""

import secrets
from urllib.parse import urlparse

from flask import flash, g, redirect, request, session, url_for

from . import bp
from .forms import FORMS
from ..core import store
from ..odoo.client import Odoo


# The route an email takes, in order, then the states off the route.
STATE_LABELS = {"new": "Queued", "review": "Needs review", "done": "Done",
                "failed": "Failed", "skipped": "Skipped"}
ROUTE = ("new", "review", "done")
OFF_ROUTE = ("failed", "skipped")


# ── request plumbing ─────────────────────────────────────────────────

@bp.before_app_request
def open_db_and_check_login():
    g.db = store.connect()
    if request.method == "POST" and not secrets.compare_digest(
            request.form.get("csrf", ""), session.get("csrf", "")):
        flash("That page was open too long, so nothing was saved. Please try again.", "error")
        return redirect(form_page())
    if request.endpoint in ("web.login", "web.setup", "static"):
        return None
    if not store.get_setting(g.db, "admin_password_hash"):
        return redirect(url_for("web.setup"))
    if not session.get("signed_in") or session.get("version") != store.get_setting(g.db, "session_version"):
        # Keep the form token: a background request (the browser's favicon) must not
        # break a login form that is already on the screen.
        token = session.get("csrf")
        session.clear()
        if token:
            session["csrf"] = token
        return redirect(url_for("web.login"))
    return None


def form_page():
    """Where to send a refused form: the page it was posted to when that page can be
    shown, else the page it came from on this site, else the home page."""
    if request.url_rule and "GET" in request.url_rule.methods:
        return request.path
    came_from = urlparse(request.referrer or "")
    if came_from.netloc == request.host and came_from.path.startswith("/"):
        return came_from.path
    return url_for("web.home")


@bp.after_app_request
def security_headers(response):
    """Pages run only this app's own scripts, are never framed, and never leak their address."""
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; form-action 'self' https://login.microsoftonline.com; "
        "frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response


@bp.teardown_app_request
def close_db(_exc):
    db = g.pop("db", None)
    if db:
        db.close()


@bp.app_context_processor
def template_helpers():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    failed, on = 0, []
    if session.get("signed_in") and "db" in g:
        failed = g.db.execute("SELECT COUNT(*) FROM message WHERE state = 'failed'").fetchone()[0]
        on = g.db.execute(f"SELECT id, name, is_production FROM odoo_connection WHERE active = 1 "
                          f"ORDER BY {store.CONNECTION_ORDER}").fetchall()
    return {"csrf": session["csrf"], "forms": FORMS, "state_labels": STATE_LABELS, "failed_count": failed,
            "connections_on": on}


def audit(kind, summary, detail=None, level="info", link=None):
    """An activity entry for something a person did on this page."""
    store.log_activity(g.db, kind, summary, detail, level=level, actor=f"admin from {request.remote_addr or '?'}",
                       link=link)


def reference_url():
    """The address of the Odoo the pages read and link to, "" when no connection is on."""
    reference = store.reference_connection(g.db)
    return ((reference or {}).get("url") or "").strip().rstrip("/")


def _odoo_or_error():
    try:
        return Odoo.from_store(g.db), None
    except Exception as exc:
        return None, str(exc)
