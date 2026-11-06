"""Signing in: the first start's password, login with lockout, password change, logout."""

import secrets
from datetime import datetime, timedelta, timezone

from flask import flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import bp
from .base import audit
from ..core import store


# ── login ────────────────────────────────────────────────────────────

@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First start: choose the admin password."""
    if store.get_setting(g.db, "admin_password_hash"):
        return redirect(url_for("web.login"))
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 8:
            flash("Use at least 8 characters.", "error")
        elif password != request.form.get("confirm"):
            flash("The two passwords are different.", "error")
        else:
            store.set_setting(g.db, "admin_password_hash", generate_password_hash(password))
            sign_in()
            audit("security", "Admin password set on first start")
            return redirect(url_for("web.settings"))
    return render_template("password.html", setup=True)


# Failed logins per address: after MAX_FAILURES within LOCKOUT, that address waits LOCKOUT.
MAX_FAILURES = 5
LOCKOUT = timedelta(minutes=15)
_failures = {}


def sign_in(sign_out_others=False):
    """A fresh session, so a session set before login is never reused. A new version
    makes every other browser's session invalid."""
    if sign_out_others or not store.get_setting(g.db, "session_version"):
        store.set_setting(g.db, "session_version", secrets.token_hex(8))
    session.clear()
    session.permanent = True
    session["signed_in"] = True
    session["version"] = store.get_setting(g.db, "session_version")
    session["csrf"] = secrets.token_urlsafe(32)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        now = datetime.now(timezone.utc)
        address = request.remote_addr or "?"
        recent = [t for t in _failures.get(address, []) if now - t < LOCKOUT]
        if len(recent) >= MAX_FAILURES:
            minutes = int((LOCKOUT - (now - recent[0])).total_seconds() // 60) + 1
            flash(f"Too many wrong passwords. Try again in {minutes} minutes.", "error")
            return render_template("password.html", setup=False), 429
        stored = store.get_setting(g.db, "admin_password_hash")
        if stored and check_password_hash(stored, request.form.get("password", "")):
            _failures.pop(address, None)
            sign_in()
            audit("security", "Signed in")
            return redirect(url_for("web.messages"))
        _failures[address] = recent + [now]
        if len(_failures[address]) >= MAX_FAILURES:
            audit("security", f"Locked out for {LOCKOUT.seconds // 60} minutes after {MAX_FAILURES} wrong passwords",
                  level="error")
        else:
            audit("security", "Wrong password", level="warning")
        flash("Wrong password.", "error")
    return render_template("password.html", setup=False)


@bp.route("/password", methods=["GET", "POST"])
def change_password():
    if request.method == "POST":
        current = request.form.get("current", "")
        password = request.form.get("password", "")
        if not check_password_hash(store.get_setting(g.db, "admin_password_hash") or "", current):
            audit("security", "Wrong current password when changing the password", level="warning")
            flash("The current password is wrong.", "error")
        elif len(password) < 8:
            flash("Use at least 8 characters for the new password.", "error")
        elif password != request.form.get("confirm"):
            flash("The two new passwords are different.", "error")
        else:
            store.set_setting(g.db, "admin_password_hash", generate_password_hash(password))
            sign_in(sign_out_others=True)
            audit("security", "Password changed; every other browser signed out")
            flash("Password changed. Other browsers are signed out.")
            return redirect(url_for("web.settings"))
    return render_template("change_password.html", title="Change password")


@bp.route("/logout", methods=["POST"])
def logout():
    audit("security", "Signed out")
    session.clear()
    return redirect(url_for("web.login"))
