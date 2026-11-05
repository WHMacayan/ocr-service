"""The Settings page: one admin login, the settings, mailboxes, vendor mappings,
subject formats, and the emails with their state."""

import re
import secrets
import sqlite3
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import (Blueprint, abort, flash, g, redirect, render_template, request, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from markupsafe import Markup, escape

from . import bookings, graph, odoo_pages, search, store
from .odoo import MODEL_NAMES, Odoo
from .settings import SETTING_GROUPS, SETTINGS

bp = Blueprint("web", __name__)

# kind: text | textarea | pattern | int | float | bool | date | secret | select | odoo
# options: (value, label) pairs for select; the name of an ODOO_OPTIONS loader for odoo.
# help: a short hint under the field. tip: what the field is for, behind its info icon.
Field = namedtuple("Field", "name label kind options help name_column group tip",
                   defaults=("text", None, "", None, "", ""))
Form = namedtuple("Form", "table title singular columns fields groups")

PURPOSES = [("freight_booking", "Freight Booking"), ("bill_matching", "Bill Matching"),
            ("customs_entry", "Customs Entry Summaries")]
SUBJECT_GROUPS = {"booking_number", "master_bl", "house_bl", "invoice_number", "etd", "eta"}

# The route an email takes, in order, then the states off the route.
STATE_LABELS = {"new": "Queued", "review": "Needs review", "done": "Done",
                "failed": "Failed", "skipped": "Skipped"}
ROUTE = ("new", "review", "done")
OFF_ROUTE = ("failed", "skipped")

FORMS = {
    "mailboxes": Form(
        "mailbox", "Mailboxes", "mailbox", ["name", "purpose", "status", "last_sync"], [
            Field("name", "Name", group="Mailbox",
                  tip="A name for this mailbox, shown in the lists and the activity log."),
            Field("purpose", "Purpose", "select", PURPOSES, group="Mailbox",
                  tip="Freight Booking: shipping documents become booking suggestions in Odoo. Bill Matching: "
                      "supplier invoices are matched to a draft bill or purchase order."),
            Field("company_id", "Company", "odoo", "companies", group="Mailbox",
                  tip="The Odoo company the bookings or bills from this mailbox belong to."),
            Field("active", "Active", "bool", help="Inactive mailboxes are never read", group="Mailbox",
                  tip="Switch off to stop reading this mailbox. Its emails and history are kept."),
            Field("tenant_id", "Tenant ID", group="Outlook app",
                  tip="Your Microsoft 365 organisation's ID. Azure portal > App registrations > your app > "
                      "Overview > Directory (tenant) ID."),
            Field("client_id", "Client ID", group="Outlook app",
                  tip="The ID of the Azure app the service signs in through. Azure portal > App registrations > "
                      "your app > Overview > Application (client) ID."),
            Field("client_secret", "Client secret", "secret", group="Outlook app",
                  tip="A password for that Azure app, made under Certificates & secrets. Needed to connect "
                      "Outlook and to stay connected. When it expires in Azure, make a new one and paste it here."),
            Field("start_from", "Read mail received from", "date",
                  help="Mail received before this date is never read (UTC)", group="Reading",
                  tip="Set this to the day you switch over from the old Odoo module, so mail it already "
                      "imported is not imported again."),
            Field("folder", "Folder", help="Leave empty to read every folder", group="Reading",
                  tip="The Outlook folder to read, for example Inbox or a subfolder name."),
            Field("fetch_window_days", "Look back (days)", "int", group="Reading",
                  tip="How far back each read looks. Mail already read is skipped, so this only matters for "
                      "mail that arrives late or was missed while the service was off."),
            Field("batch_size", "New emails per run", "int", group="Reading",
                  tip="The most new emails saved in one read. The rest is picked up on the next read."),
            Field("auto_sync", "Read automatically", "bool", group="Reading",
                  tip="Read this mailbox on a schedule. Off means it is only read when you press Read now."),
            Field("sync_interval", "Every (minutes)", "int", group="Reading",
                  tip="Minutes between automatic reads of this mailbox."),
            Field("group_address", "Distribution group",
                  help="Only mail sent to this group (in To or Cc). Enter its exact name, e.g. WTD - PH, "
                       "or its address, e.g. wtd@company.com. Leave empty to read all mail.",
                  group="Filters",
                  tip="Use this when the mailbox also receives mail that is not for this process: only mail "
                      "addressed to the distribution group is read."),
            Field("sender_filter", "From", help="Only mail from this address", group="Filters",
                  tip="Read mail from this one sender address only. Leave empty to read every sender."),
            Field("sender_domains", "From domains", help="Comma-separated, e.g. tlius.com", group="Filters",
                  tip="Read mail only from senders at these domains, whoever at the domain sends it. "
                      "Leave empty to read every sender."),
            Field("subject_keyword", "Subject or preview contains", group="Filters",
                  tip="Read only mail whose subject or first lines contain this word or phrase."),
            Field("skip_sender_domains", "Skip senders",
                  help="Comma-separated domains; their emails are kept without files", group="Filters",
                  tip="For senders whose attachments are never shipping documents, such as newsletters. "
                      "Their emails are listed, but the files are not downloaded or read."),
            Field("booking_number_pattern", "Booking number pattern", "pattern",
                  help="Regular expression for booking numbers in this mailbox", group="Filters",
                  tip="How a booking number looks in this mailbox's subject lines, used when no subject "
                      "format finds it. For example [A-Z]{2}\\d{6}."),
        ], {
            "Mailbox": "What this mailbox is for and which Odoo company it feeds.",
            "Outlook app": "The Azure app registration used to sign in. Save these, then connect Outlook.",
            "Reading": "Which mail is read and how often.",
            "Filters": "Mail that does not match is left in the mailbox.",
        }),
    "vendor-mappings": Form(
        "vendor_mapping", "Vendor mappings", "vendor mapping",
        ["partner_name", "role", "sender_domain", "document_name", "freight_mode", "active"], [
            Field("role", "Role", "select", [("forwarder", "Forwarder"), ("factory", "Factory")],
                  group="Vendor",
                  tip="Forwarder: arranges the transport and sends booking confirmations and freight invoices. "
                      "Factory: ships the goods and sends commercial invoices and packing lists."),
            Field("partner_id", "Odoo partner", "odoo", "partners", name_column="partner_name",
                  group="Vendor",
                  tip="The Odoo partner this vendor is. Bookings from this vendor are linked to it."),
            Field("active", "Active", "bool", group="Vendor",
                  tip="Switch off to stop using this mapping without deleting it."),
            Field("sender_domain", "Sender domain", help="For example maersk.com", group="Recognised by",
                  tip="Email from this domain is recognised as this vendor. This is the surest match."),
            Field("document_name", "Name on documents", help="As printed on their documents",
                  group="Recognised by",
                  tip="The vendor's name as printed on documents. Used when the sender doesn't identify the "
                      "vendor; such a match is flagged on the booking for a person to check."),
            Field("freight_mode", "Freight mode", "select", [("", "None"), ("ocean", "Ocean"), ("inland", "Inland")],
                  group="Freight cost",
                  tip="Ocean or inland freight. Set on bookings from this forwarder."),
            Field("freight_product_id", "Freight product", "odoo", "products", group="Freight cost",
                  tip="The Odoo product this forwarder's freight is suggested on."),
            Field("cost_per_container", "Cost per container", "float", group="Freight cost",
                  tip="This forwarder's agreed cost per container, suggested on the booking when no freight "
                      "invoice amount is known yet."),
        ], {
            "Vendor": "The Odoo partner this vendor is.",
            "Recognised by": "How an email or document is matched to this vendor.",
            "Freight cost": "Used when this forwarder's freight is booked.",
        }),
    "subject-formats": Form(
        "subject_format", "Subject formats", "subject format",
        ["name", "sequence", "sender_domains", "subject_pattern", "confirms_booking", "active"], [
            Field("name", "Name", group="Format",
                  tip="A name for this format, for example the forwarder that uses it."),
            Field("sequence", "Order", "int", help="Lower numbers are tried first", group="Format",
                  tip="Formats are tried from the lowest number up; the first one that matches the subject "
                      "is used."),
            Field("active", "Active", "bool", group="Format",
                  tip="Switch off to stop using this format without deleting it."),
            Field("sender_domains", "Sender domains", help="Comma-separated; leave empty for every sender",
                  group="Match",
                  tip="This format is only tried for mail from these domains."),
            Field("subject_pattern", "Subject pattern", "pattern",
                  help="Regular expression with named groups: " + ", ".join(sorted(SUBJECT_GROUPS)),
                  group="Match",
                  tip="Reads numbers and dates straight from the subject line, before any AI. Name each part, "
                      "for example BKG (?P<booking_number>\\d+), so the service knows what it found."),
            Field("confirms_booking", "This subject confirms the booking", "bool", group="Match",
                  tip="Mail matching this format, from a mapped forwarder, is the booking confirmation: its "
                      "values are taken as confirmed on the booking."),
        ], {
            "Format": "Formats are tried in order; the first that matches is used.",
            "Match": "Which senders use this format and how their subject lines read.",
        }),
}

REQUIRED = {"mailbox": ["name", "purpose", "company_id"], "vendor_mapping": ["role"],
            "subject_format": ["name", "subject_pattern"]}

ODOO_OPTIONS = {
    "companies": ("res.company", []),
    "partners": ("res.partner", [("supplier_rank", ">", 0)]),
    "products": ("product.product", [("purchase_ok", "=", True), ("type", "=", "service")]),
}


def sections(fields, descriptions):
    """Fields grouped in their order of appearance: [(title, description, fields)]."""
    order = []
    for field in fields:
        if field.group not in order:
            order.append(field.group)
    return [(name, descriptions.get(name, ""), [f for f in fields if f.group == name]) for name in order]


def secret_hints(fields, values):
    """For each saved secret, its last four characters; the secret itself never reaches the page."""
    return {f.name: (values.get(f.name) or "")[-4:] for f in fields
            if f.kind == "secret" and values.get(f.name)}


# ── request plumbing ─────────────────────────────────────────────────

@bp.before_app_request
def open_db_and_check_login():
    g.db = store.connect()
    if request.method == "POST" and not secrets.compare_digest(
            request.form.get("csrf", ""), session.get("csrf", "")):
        abort(400, "The form expired. Go back, reload the page and try again.")
    if request.endpoint in ("web.login", "web.setup", "static"):
        return None
    if not store.get_setting(g.db, "admin_password_hash"):
        return redirect(url_for("web.setup"))
    if not session.get("signed_in") or session.get("version") != store.get_setting(g.db, "session_version"):
        session.clear()
        return redirect(url_for("web.login"))
    return None


@bp.app_template_filter("highlight")
def highlight(text, terms):
    """The text, escaped, with every search term wrapped in <mark>."""
    text = str(text or "")
    words = sorted({t for t in terms or [] if t}, key=len, reverse=True)
    if not words:
        return escape(text)
    pattern = re.compile("|".join(re.escape(w) for w in words), re.IGNORECASE)
    out, last = [], 0
    for m in pattern.finditer(text):
        out += [escape(text[last:m.start()]), Markup("<mark>"), escape(m.group(0)), Markup("</mark>")]
        last = m.end()
    out.append(escape(text[last:]))
    return Markup("").join(out)


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
    failed = 0
    if session.get("signed_in") and "db" in g:
        failed = g.db.execute("SELECT COUNT(*) FROM message WHERE state = 'failed'").fetchone()[0]
    return {"csrf": session["csrf"], "forms": FORMS, "state_labels": STATE_LABELS, "failed_count": failed}


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


# ── activity log ─────────────────────────────────────────────────────

def audit(kind, summary, detail=None, level="info", link=None):
    """An activity entry for something a person did on this page."""
    store.log_activity(g.db, kind, summary, detail, level=level, actor=f"admin from {request.remote_addr or '?'}",
                       link=link)


def _shown(value):
    return "(empty)" if value in (None, "") else str(value)


def describe_changes(fields, before, after):
    """'Label: old → new' per changed field. Secrets and prompts only say that they changed."""
    lines = []
    for f in fields:
        if f.name not in after:
            continue
        old, new = before.get(f.name), after[f.name]
        if _shown(old) == _shown(new):
            continue
        if f.kind in ("secret", "textarea"):
            lines.append(f"{f.label}: changed")
        elif f.kind == "bool":
            lines.append(f"{f.label}: {'on' if new else 'off'}")
        else:
            lines.append(f"{f.label}: {_shown(old)} → {_shown(new)}")
    return lines


ACTIVITY_LABELS = {"settings": "Setting changes", "security": "Sign-ins and security",
                   "processing": "Processing", "odoo": "Odoo writes"}
ACTIVITY_PAGE = 100


@bp.route("/activity")
def activity():
    kind = request.args.get("kind")
    query = (request.args.get("q") or "").strip()
    before = request.args.get("before", type=int)
    where, args = [], []
    if kind in ACTIVITY_LABELS:
        where.append("kind = ?")
        args.append(kind)
    if query:
        where.append("(summary LIKE ? OR detail LIKE ?)")
        args += [f"%{query}%"] * 2
    if before:
        where.append("id < ?")
        args.append(before)
    rows = g.db.execute(
        "SELECT * FROM activity" + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY id DESC LIMIT ?", (*args, ACTIVITY_PAGE + 1)).fetchall()
    counts = dict(g.db.execute("SELECT kind, COUNT(*) FROM activity GROUP BY kind").fetchall())
    older = rows[ACTIVITY_PAGE - 1]["id"] if len(rows) > ACTIVITY_PAGE else None
    return render_template("activity.html", title="Activity", rows=rows[:ACTIVITY_PAGE], kind=kind, query=query,
                           labels=ACTIVITY_LABELS, counts=counts, total=sum(counts.values()), older=older,
                           keep_days=store.get_setting(g.db, "activity_days"))


@bp.route("/")
def home():
    return redirect(url_for("web.messages"))


# ── settings ─────────────────────────────────────────────────────────

SETTING_FIELDS = [Field(s.key, s.label, s.kind, help=s.help, group=s.group, tip=s.tip) for s in SETTINGS]


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        values, error = parse_form(SETTING_FIELDS, request.form)
        if error:
            flash(error, "error")
        else:
            before = {f.name: store.get_setting(g.db, f.name) for f in SETTING_FIELDS}
            for key, value in values.items():
                store.set_setting(g.db, key, value)
            changes = describe_changes(SETTING_FIELDS, before, values)
            if changes:
                audit("settings", f"Changed settings: {', '.join(c.split(':')[0] for c in changes)}",
                      "\n".join(changes), link=url_for("web.settings"))
            if request.form.get("then") == "test-odoo":
                test_odoo_connection()
            else:
                flash("Settings saved.")
            return redirect(url_for("web.settings"))
    current = {f.name: store.get_setting(g.db, f.name) for f in SETTING_FIELDS}
    return render_template("form.html", title="Settings", sections=sections(SETTING_FIELDS, SETTING_GROUPS),
                           record=current, hints=secret_hints(SETTING_FIELDS, current), options={},
                           action=url_for("web.settings"), extra="settings")


def test_odoo_connection():
    try:
        odoo = Odoo.from_store(g.db)
    except Exception as exc:
        flash(f"Settings saved, but Odoo could not be reached: {exc}", "error")
        return
    flash(f"Settings saved. Connected to Odoo as user {odoo.uid}.")
    for warning in odoo_warnings(odoo):
        flash(warning, "error")


def odoo_warnings(odoo):
    """Risks in how the service reaches Odoo, worth fixing before real use."""
    warnings = []
    if odoo.uid in (1, 2):
        warnings.append("This is Odoo's administrator account, so a leaked API key would give full control of Odoo. "
                        "Create a dedicated user in OCR Documents / Manager with read access to purchases, "
                        "bills and warehouses, and use its login and API key here.")
    url = urlparse(store.get_setting(g.db, "odoo_url") or "")
    if url.scheme == "http" and url.hostname not in LOCAL_HOSTS:
        warnings.append("The Odoo address uses http://, so the API key and data cross the network unencrypted. "
                        "Use the https:// address.")
    return warnings


# Odoo on this PC: plain http never leaves the machine.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "katana.local", "host.docker.internal"}


# ── mailboxes, vendor mappings, subject formats ──────────────────────

def _form(key):
    if key not in FORMS:
        abort(404)
    return FORMS[key]


def odoo_options(fields):
    """Dropdown choices loaded from Odoo, {} when Odoo cannot be reached."""
    loaders = {f.options for f in fields if f.kind == "odoo"}
    if not loaders:
        return {}
    try:
        odoo = Odoo.from_store(g.db)
        # display_name is not stored, so Odoo cannot sort on it; name is.
        return {name: [(row["id"], row["display_name"]) for row in
                       odoo.search_read(ODOO_OPTIONS[name][0], ODOO_OPTIONS[name][1], ["display_name"],
                                        order="name")]
                for name in loaders}
    except Exception as exc:
        # An Odoo fault carries the whole server traceback; its last line says what went wrong.
        g.odoo_error = (str(getattr(exc, "faultString", exc)).strip().splitlines() or ["unknown error"])[-1][:200]
        return {}


def parse_form(fields, form, options=None):
    """Form values by column, or an error. An empty secret or whole number keeps the stored one."""
    values = {}
    for field in fields:
        raw = form.get(field.name, "").strip()
        try:
            if field.kind == "bool":
                values[field.name] = 1 if field.name in form else 0
            elif field.kind in ("secret", "int"):
                if raw:
                    values[field.name] = int(raw) if field.kind == "int" else raw
            elif field.kind == "odoo":
                values[field.name] = int(raw) if raw else None
            elif field.kind == "float":
                values[field.name] = float(raw) if raw else None
            elif field.kind == "date":
                values[field.name] = f"{raw} 00:00:00" if raw else None
            else:
                values[field.name] = raw or None
        except ValueError:
            return None, f"{field.label}: enter a number."
        if field.name_column:
            names = dict((options or {}).get(field.options, []))
            values[field.name_column] = names.get(values[field.name])
    return values, None


def validate(form, values):
    labels = {f.name: f.label for f in form.fields}
    for column in REQUIRED.get(form.table, []):
        if not values.get(column):
            return f"{labels[column]} is required."
    for column in ("booking_number_pattern", "subject_pattern"):
        if values.get(column):
            try:
                pattern = re.compile(values[column])
            except re.error as exc:
                return f"{labels[column]} is not a valid regular expression: {exc}."
            if column == "subject_pattern":
                unknown = set(pattern.groupindex) - SUBJECT_GROUPS
                if unknown:
                    return (f"Unknown group(s) {', '.join(sorted(unknown))}; "
                            f"use only {', '.join(sorted(SUBJECT_GROUPS))}.")
                if not pattern.groupindex:
                    return "The subject pattern needs at least one named group, e.g. (?P<booking_number>\\w+)."
    return None


def column_defaults(table):
    """The schema's defaults, shown on a new record's form."""
    values = {}
    for column in g.db.execute(f"PRAGMA table_info({table})"):
        default = column["dflt_value"]
        if default is None or default == "CURRENT_TIMESTAMP":
            continue
        values[column["name"]] = default.strip("'") if default.startswith("'") else int(default)
    return values


def record_label(values, record_id):
    return values.get("name") or values.get("partner_name") or values.get("sender_domain") or f"#{record_id}"


@bp.route("/<key>/")
def records(key):
    form = _form(key)
    order = "sequence, id" if form.table == "subject_format" else "id"
    rows = g.db.execute(f"SELECT * FROM {form.table} ORDER BY {order}").fetchall()
    return render_template("list.html", key=key, form=form, rows=rows,
                           labels={f.name: f.label for f in form.fields})


@bp.route("/<key>/new", methods=["GET", "POST"], defaults={"record_id": None})
@bp.route("/<key>/<int:record_id>", methods=["GET", "POST"])
def record(key, record_id):
    form = _form(key)
    if record_id:
        row = g.db.execute(f"SELECT * FROM {form.table} WHERE id = ?", (record_id,)).fetchone()
        if not row:
            abort(404)
        current = dict(row)
        if form.table == "mailbox":
            current = store.mailbox(g.db, record_id)
    else:
        current = column_defaults(form.table)
    options = odoo_options(form.fields)
    if request.method == "POST":
        values, error = parse_form(form.fields, request.form, options)
        error = error or validate(form, values)
        if error:
            flash(error, "error")
            current.update(values)
        else:
            changes = describe_changes(form.fields, current if record_id else {}, values)
            is_new = not record_id
            if form.table == "mailbox" and "client_secret" in values:
                values["client_secret"] = store.encrypt(g.db, values["client_secret"])
            with g.db:
                if record_id:
                    g.db.execute(f"UPDATE {form.table} SET {', '.join(f'{c} = ?' for c in values)} "
                                 "WHERE id = ?", (*values.values(), record_id))
                else:
                    record_id = g.db.execute(
                        f"INSERT INTO {form.table} ({', '.join(values)}) "
                        f"VALUES ({', '.join('?' * len(values))})", tuple(values.values())).lastrowid
            if changes or is_new:
                audit("settings", f"{'Created' if is_new else 'Updated'} {form.singular} {record_label(dict(current, **values), record_id)}",
                      "\n".join(changes), link=url_for("web.record", key=key, record_id=record_id))
            flash(f"{form.singular.capitalize()} saved.")
            if form.table == "mailbox":
                return redirect(url_for("web.record", key=key, record_id=record_id))
            return redirect(url_for("web.records", key=key))
    return render_template("form.html", title=form.title, form=form, key=key, record_id=record_id,
                           sections=sections(form.fields, form.groups), record=current,
                           hints=secret_hints(form.fields, current), options=options,
                           action=request.path, extra="mailbox" if form.table == "mailbox" else None,
                           odoo_error=g.get("odoo_error"),
                           redirect_uri=url_for("web.oauth_callback", _external=True))


@bp.route("/<key>/<int:record_id>/delete", methods=["POST"])
def delete(key, record_id):
    form = _form(key)
    row = g.db.execute(f"SELECT * FROM {form.table} WHERE id = ?", (record_id,)).fetchone() or abort(404)
    try:
        with g.db:
            g.db.execute(f"DELETE FROM {form.table} WHERE id = ?", (record_id,))
        audit("settings", f"Deleted {form.singular} {record_label(dict(row), record_id)}")
        flash(f"{form.singular.capitalize()} deleted.")
    except sqlite3.IntegrityError:
        flash("This mailbox has emails, so it cannot be deleted. Untick Active instead.", "error")
        return redirect(url_for("web.record", key=key, record_id=record_id))
    return redirect(url_for("web.records", key=key))


# ── mailbox actions ──────────────────────────────────────────────────

@bp.route("/mailboxes/<int:mailbox_id>/sync", methods=["POST"])
def sync_now(mailbox_id):
    mailbox = store.mailbox(g.db, mailbox_id) or abort(404)
    store.update_mailbox(g.db, mailbox_id, {"sync_requested": 1})
    audit("processing", f"Asked to read mailbox {mailbox['name']} now",
          link=url_for("web.runs", mailbox_id=mailbox_id))
    flash("The mailbox is read on the worker's next check.")
    return redirect(url_for("web.record", key="mailboxes", record_id=mailbox_id))


# A copy starts unconnected and unread: Microsoft rotates the refresh token, so two mailboxes
# sharing one would sign each other out.
NOT_DUPLICATED = ("id", "refresh_token", "access_token", "token_expires_at", "sync_requested",
                  "fetch_resume_from", "status", "last_sync", "last_error", "group_miss_note")


@bp.route("/mailboxes/<int:mailbox_id>/duplicate", methods=["POST"])
def duplicate_mailbox(mailbox_id):
    """A new, inactive mailbox with this one's settings, opened for editing."""
    row = g.db.execute("SELECT * FROM mailbox WHERE id = ?", (mailbox_id,)).fetchone() or abort(404)
    values = {k: v for k, v in dict(row).items() if k not in NOT_DUPLICATED}
    values.update(name=f"{row['name']} (copy)", active=0)
    with g.db:
        copy_id = g.db.execute(f"INSERT INTO mailbox ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                               tuple(values.values())).lastrowid
    link = url_for("web.record", key="mailboxes", record_id=copy_id)
    audit("settings", f"Duplicated mailbox {row['name']} as {values['name']}", link=link)
    flash("Mailbox duplicated. Change what differs, connect Outlook, then switch on Active.")
    return redirect(link)


@bp.route("/mailboxes/<int:mailbox_id>/clear", methods=["POST"])
def clear_mailbox(mailbox_id):
    """Start the mailbox over: its emails, files and runs go; its settings and Outlook connection stay.
    The next read fetches its mail again from the start date or the look-back window."""
    mailbox = store.mailbox(g.db, mailbox_id) or abort(404)
    link = url_for("web.record", key="mailboxes", record_id=mailbox_id)
    if mailbox["status"] == "running":
        flash("The mailbox is being read right now. Wait for the read to finish, then clear it.", "error")
        return redirect(link)
    files = g.db.execute("SELECT f.path FROM file f JOIN message m ON m.id = f.message_id WHERE m.mailbox_id = ?",
                         (mailbox_id,)).fetchall()
    with g.db:
        emails = g.db.execute("SELECT COUNT(*) FROM message WHERE mailbox_id = ?", (mailbox_id,)).fetchone()[0]
        g.db.execute("DELETE FROM file WHERE message_id IN (SELECT id FROM message WHERE mailbox_id = ?)",
                     (mailbox_id,))
        g.db.execute("DELETE FROM message WHERE mailbox_id = ?", (mailbox_id,))
        g.db.execute("DELETE FROM run WHERE mailbox_id = ?", (mailbox_id,))
    store.update_mailbox(g.db, mailbox_id, {"fetch_resume_from": None, "status": "new", "last_sync": None,
                                            "last_error": None, "group_miss_note": None, "sync_requested": 0})
    # Another purpose's mailbox can hold the same file: keep what is still in use.
    in_use = {row["path"] for row in g.db.execute("SELECT path FROM file")}
    for row in files:
        if row["path"] not in in_use:
            Path(row["path"]).unlink(missing_ok=True)
    audit("processing", f"Cleared mailbox {mailbox['name']} to start over",
          f"{emails} emails, {len(files)} files and the run history removed. Settings and the Outlook "
          "connection kept. Records already in Odoo are not touched.", level="warning", link=link)
    flash(f"Cleared: {emails} emails and {len(files)} files removed. The next read starts fresh.")
    return redirect(link)


@bp.route("/mailboxes/<int:mailbox_id>/connect", methods=["POST"])
def connect_outlook(mailbox_id):
    mailbox = store.mailbox(g.db, mailbox_id) or abort(404)
    if not all(mailbox.get(f) for f in ("tenant_id", "client_id", "client_secret")):
        flash("Fill in the tenant ID, client ID and client secret, and save, before connecting.", "error")
        return redirect(url_for("web.record", key="mailboxes", record_id=mailbox_id))
    state = secrets.token_urlsafe(24)
    session["oauth"] = {"state": state, "mailbox_id": mailbox_id}
    return redirect(graph.authorize_url(mailbox, url_for("web.oauth_callback", _external=True), state))


@bp.route("/oauth/callback")
def oauth_callback():
    pending = session.pop("oauth", None)
    if not pending or request.args.get("state") != pending["state"]:
        abort(400, "This sign-in does not match the one started here. Press Connect Outlook again.")
    mailbox_id = pending["mailbox_id"]
    mailbox = store.mailbox(g.db, mailbox_id) or abort(404)
    link = url_for("web.record", key="mailboxes", record_id=mailbox_id)
    if request.args.get("error"):
        reason = request.args.get("error_description") or request.args["error"]
        audit("security", f"Outlook sign-in for mailbox {mailbox['name']} failed", reason, level="error", link=link)
        flash(f"Microsoft sign-in failed: {reason}", "error")
    else:
        try:
            tokens = graph.exchange_code(mailbox, request.args.get("code", ""),
                                         url_for("web.oauth_callback", _external=True))
            store.update_mailbox(g.db, mailbox_id, dict(tokens, last_error=None))
            audit("security", f"Outlook connected for mailbox {mailbox['name']}", link=link)
            flash("Outlook connected.")
        except Exception as exc:
            audit("security", f"Outlook connection for mailbox {mailbox['name']} failed", str(exc),
                  level="error", link=link)
            flash(f"Outlook connection failed: {exc}", "error")
    return redirect(url_for("web.record", key="mailboxes", record_id=mailbox_id))


@bp.route("/mailboxes/<int:mailbox_id>/runs")
def runs(mailbox_id):
    mailbox = store.mailbox(g.db, mailbox_id) or abort(404)
    rows = g.db.execute("SELECT * FROM run WHERE mailbox_id = ? ORDER BY id DESC LIMIT 20",
                        (mailbox_id,)).fetchall()
    return render_template("runs.html", mailbox=mailbox, runs=rows)


# ── emails ───────────────────────────────────────────────────────────

@bp.route("/messages")
def messages():
    state = request.args.get("state")
    q = (request.args.get("q") or "").strip()
    where, args = search.email_where(search.parse(q))
    if state in STATE_LABELS:
        where, args = f"{where} AND m.state = ?", (*args, state)
    rows = g.db.execute(
        "SELECT m.*, b.name AS mailbox_name, (SELECT COUNT(*) FROM file f WHERE f.message_id = m.id) AS files "
        f"FROM message m JOIN mailbox b ON b.id = m.mailbox_id WHERE {where} "
        "ORDER BY m.received DESC, m.id DESC LIMIT 201", args).fetchall()
    counts = dict(g.db.execute("SELECT state, COUNT(*) FROM message GROUP BY state").fetchall())
    mailboxes = g.db.execute("SELECT COUNT(*) FROM mailbox").fetchone()[0]
    return render_template("messages.html", rows=rows[:200], more=len(rows) > 200, state=state, q=q, counts=counts,
                           route=ROUTE, off_route=OFF_ROUTE, total=sum(counts.values()), mailboxes=mailboxes,
                           in_odoo=odoo_records([r["id"] for r in rows[:200]]))


# The Odoo records an email is shown with; changes to their lines (containers, conflicts…) are in its detail.
MAIN_MODELS = {"b2b.ocr.booking": "Booking", "b2b.ocr.scraped.bill": "Scraped bill"}
ACTION_RANK = {"created": 3, "updated": 2, "linked": 1, "deleted": 0}


def odoo_records(message_ids):
    """Per email id: {"records": [{kind, label, link, action}], "other": n changes to their lines}."""
    if not message_ids:
        return {}
    marks = ", ".join("?" * len(message_ids))
    out = {}
    for r in g.db.execute(f"SELECT message_id, model, record_id, label, link, action FROM odoo_change "
                          f"WHERE message_id IN ({marks}) ORDER BY id", message_ids):
        entry = out.setdefault(r["message_id"], {"records": {}, "other": 0})
        if r["model"] not in MAIN_MODELS:
            entry["other"] += 1
            continue
        old = entry["records"].get((r["model"], r["record_id"]))
        action = max((r["action"], old and old["action"]), key=lambda a: ACTION_RANK.get(a, -1))
        entry["records"][(r["model"], r["record_id"])] = {
            "kind": MAIN_MODELS[r["model"]], "model": r["model"], "id": r["record_id"],
            "label": r["label"] or (old and old["label"]) or f"#{r['record_id']}",
            "link": r["link"] or (old and old["link"]), "action": action}
    # Emails processed before this record was kept: the file still names its booking or bill.
    odoo_url = (store.get_setting(g.db, "odoo_url") or "").rstrip("/")
    for r in g.db.execute(f"SELECT f.message_id, f.odoo_id, b.purpose FROM file f JOIN message m ON m.id = f.message_id "
                          f"JOIN mailbox b ON b.id = m.mailbox_id WHERE f.message_id IN ({marks}) "
                          "AND f.odoo_id IS NOT NULL", message_ids):
        model = "b2b.ocr.booking" if r["purpose"] == "freight_booking" else "b2b.ocr.scraped.bill"
        entry = out.setdefault(r["message_id"], {"records": {}, "other": 0})
        entry["records"].setdefault((model, r["odoo_id"]), {
            "kind": MAIN_MODELS[model], "model": model, "id": r["odoo_id"], "label": f"#{r['odoo_id']}", "action": "linked",
            "link": f"{odoo_url}/web#model={model}&id={r['odoo_id']}&view_type=form" if odoo_url else None})
    return {mid: {"records": list(e["records"].values()), "other": e["other"]} for mid, e in out.items()}


@bp.route("/search")
def search_page():
    """Everything matching the search box: a page, or JSON for the quick search."""
    q = (request.args.get("q") or "").strip()
    if request.args.get("format") == "json":
        groups = search.run(g.db, q, limit=6, state_labels=STATE_LABELS)
        return {"query": q, "terms": search.parse(q).terms,
                "groups": [{"key": key, "label": label, "more": more,
                            "items": [item._asdict() for item in items]} for key, label, items, more in groups]}
    groups = search.run(g.db, q, limit=50, state_labels=STATE_LABELS)
    return render_template("search.html", title="Search", q=q, groups=groups, terms=search.parse(q).terms)


# ── scraped bills and customs: Odoo's records, as its forms show them ─

def _odoo_link(model, record_id):
    return f"{(store.get_setting(g.db, 'odoo_url') or '').rstrip('/')}/web#model={model}&id={record_id}&view_type=form"


def _odoo_record(page, record_id, crumbs, purpose=None):
    feeding = odoo_pages.emails(g.db, purpose, record_id) if purpose else []
    odoo, error = _odoo_or_error()
    record = None
    if odoo:
        try:
            record = odoo_pages.read(odoo, page, record_id)
        except Exception as exc:
            error = str(exc)
    if not record and not error and not feeding:
        abort(404)
    return render_template("odoo_record.html", title=(record or {}).get("title") or f"#{record_id}", record=record,
                           crumbs=crumbs, emails=feeding, error=error, odoo_link=_odoo_link(page["model"], record_id))


def _odoo_list(title, intro, model, ids, columns, endpoint, id_arg, empty, purpose=None):
    odoo, error = _odoo_or_error()
    heads, rows = [], []
    if odoo and ids:
        try:
            heads, rows = odoo_pages.list_rows(odoo, model, ids, columns)
        except Exception as exc:
            error = str(exc)
    counts = odoo_pages.email_counts(g.db, purpose) if purpose else {}
    for row in rows:
        row["emails"] = counts.get(row["id"], 0)
    return render_template("odoo_list.html", title=title, intro=intro, heads=heads, rows=rows, endpoint=endpoint,
                           id_arg=id_arg, empty=empty, error=error)


@bp.route("/scraped-bills")
def scraped_bills():
    return _odoo_list("Scraped bills", "Every supplier invoice the service sent to Odoo, as Odoo holds it now.",
                      odoo_pages.SCRAPED_BILL["model"], odoo_pages.record_ids(g.db, "bill_matching"),
                      odoo_pages.BILL_LIST, "web.scraped_bill", "bill_id", "Scraped bills appear here once the service has "
                      "sent an invoice from a Bill Matching mailbox to Odoo.", purpose="bill_matching")


@bp.route("/scraped-bills/<int:bill_id>")
def scraped_bill(bill_id):
    return _odoo_record(odoo_pages.SCRAPED_BILL, bill_id, [("Scraped bills", url_for("web.scraped_bills"))],
                        purpose="bill_matching")


@bp.route("/customs")
def customs_batches():
    entry_ids = odoo_pages.record_ids(g.db, "customs_entry")
    odoo, _error = _odoo_or_error()
    batch_ids = set()
    if odoo and entry_ids:
        try:
            batch_ids = {r["batch_id"][0] for r in odoo.call(
                odoo_pages.CUSTOMS_ENTRY["model"], "search_read", [("id", "in", sorted(entry_ids))],
                fields=["batch_id"]) if r["batch_id"]}
        except Exception:
            pass  # the list below reports Odoo's error
    return _odoo_list("Customs", "The customs audit batches holding the entry summaries the service uploaded, "
                      "as Odoo holds them now. Open a batch for its entries, and an entry for its findings.",
                      odoo_pages.CUSTOMS_BATCH["model"], batch_ids, odoo_pages.BATCH_LIST, "web.customs_batch", "batch_id",
                      "Batches appear here once the service has uploaded an entry summary from a Customs Entry "
                      "Summaries mailbox.")


@bp.route("/customs/batches/<int:batch_id>")
def customs_batch(batch_id):
    return _odoo_record(odoo_pages.CUSTOMS_BATCH, batch_id, [("Customs", url_for("web.customs_batches"))])


@bp.route("/customs/entries/<int:entry_id>")
def customs_entry(entry_id):
    crumbs = [("Customs", url_for("web.customs_batches"))]
    odoo, _error = _odoo_or_error()
    if odoo:
        try:
            found = odoo.call(odoo_pages.CUSTOMS_ENTRY["model"], "search_read", [("id", "=", entry_id)],
                              fields=["batch_id"])
            if found and found[0]["batch_id"]:
                crumbs.append((found[0]["batch_id"][1], url_for("web.customs_batch", batch_id=found[0]["batch_id"][0])))
        except Exception:
            pass  # the page reports Odoo's error
    return _odoo_record(odoo_pages.CUSTOMS_ENTRY, entry_id, crumbs, purpose="customs_entry")


# ── bookings: the service's record against Odoo ─────────────────────

def _odoo_or_error():
    try:
        return Odoo.from_store(g.db), None
    except Exception as exc:
        return None, str(exc)


@bp.route("/bookings")
def booking_list():
    ids = sorted(bookings.booking_ids(g.db), reverse=True)
    written = bookings.service_values(g.db, ids)
    odoo, error = _odoo_or_error()
    odoo_rows, info = {}, {}
    if odoo and ids:
        try:
            info = bookings.fields_info(odoo, bookings.BOOKING)
            fields = [f for f in ("booking_number", "state", "review_state", "factory_id", "forwarder_id", "etd", "eta",
                                  "port_of_loading", "port_of_discharge", "open_conflict_count")
                      + tuple(written_field for row in written.values() for written_field in row) if f in info]
            odoo_rows = {r["id"]: r for r in odoo.call(bookings.BOOKING, "search_read", [("id", "in", ids)],
                                                       fields=sorted(set(fields)), context={"active_test": False})}
        except Exception as exc:
            error = str(exc)
    emails_per = dict(g.db.execute(
        "SELECT record_id, COUNT(DISTINCT message_id) FROM odoo_change WHERE model = ? GROUP BY record_id",
        (bookings.BOOKING,)).fetchall())
    rows = []
    for booking_id in ids:
        mine, theirs = written.get(booking_id, {}), odoo_rows.get(booking_id)
        differs = [f for f, w in mine.items() if theirs and f in info and not bookings.same(w["value"], theirs.get(f), info[f])]
        label = (theirs or {}).get("booking_number") or (mine.get("booking_number") or {}).get("value") or f"#{booking_id}"
        rows.append({"id": booking_id, "label": label, "odoo": theirs, "recorded": bool(mine), "differs": differs,
                     "emails": emails_per.get(booking_id, 0), "missing_in_odoo": bool(odoo_rows) and theirs is None,
                     **{f"{f}_shown": bookings.shown((theirs or {}).get(f), info.get(f))
                        for f in ("state", "factory_id", "forwarder_id", "etd", "eta", "port_of_loading", "port_of_discharge")}})
    return render_template("bookings.html", title="Bookings", rows=rows, error=error)


@bp.route("/bookings/<int:booking_id>")
def booking(booking_id):
    written = bookings.service_values(g.db, [booking_id]).get(booking_id, {})
    feeding = bookings.emails(g.db, booking_id)
    if not written and not feeding:
        abort(404)
    odoo, error = _odoo_or_error()
    info, odoo_row, children = {}, None, {}
    if odoo:
        try:
            info, odoo_row, children = bookings.read_odoo(odoo, booking_id)
        except Exception as exc:
            error = str(exc)
    sections = bookings.compare(info, odoo_row, written) if info else []
    odoo_url = (store.get_setting(g.db, "odoo_url") or "").rstrip("/")
    return render_template(
        "booking.html", title=f"Booking {(odoo_row or {}).get('booking_number') or booking_id}", booking_id=booking_id,
        odoo_row=odoo_row, info=info, sections=sections, written=written, children=children, emails=feeding,
        made_by_service=bookings.created_children(g.db, booking_id), error=error,
        state_label=bookings.shown((odoo_row or {}).get("state"), info.get("state")),
        odoo_link=f"{odoo_url}/web#model={bookings.BOOKING}&id={booking_id}&view_type=form",
        differs=sum(1 for _t, rows in sections for r in rows if r["state"] == "differs"))


@bp.route("/messages/<int:message_id>")
def message(message_id):
    row = g.db.execute("SELECT m.*, b.name AS mailbox_name FROM message m JOIN mailbox b "
                       "ON b.id = m.mailbox_id WHERE m.id = ?", (message_id,)).fetchone() or abort(404)
    files = g.db.execute("SELECT * FROM file WHERE message_id = ? ORDER BY id", (message_id,)).fetchall()
    changes = g.db.execute("SELECT c.*, f.filename FROM odoo_change c LEFT JOIN file f ON f.id = c.file_id "
                           "WHERE c.message_id = ? ORDER BY c.id", (message_id,)).fetchall()
    return render_template("message.html", message=row, files=files, changes=changes, model_names=MODEL_NAMES,
                           in_odoo=odoo_records([message_id]).get(message_id))


@bp.route("/messages/<int:message_id>/retry", methods=["POST"])
def retry(message_id):
    """Process the email again; its files keep their cached extraction."""
    row = g.db.execute("SELECT subject FROM message WHERE id = ?", (message_id,)).fetchone() or abort(404)
    audit("processing", f"Queued again by hand: {row['subject'] or 'No subject'}",
          link=url_for("web.message", message_id=message_id))
    with g.db:
        # email_done = 0: the email text is read again too, so the record of what it changes in Odoo is complete.
        g.db.execute("UPDATE message SET state = 'new', attempts = 0, note = NULL, email_done = 0 WHERE id = ?",
                     (message_id,))
        g.db.execute("UPDATE file SET state = 'new', note = NULL WHERE message_id = ?", (message_id,))
    flash("Queued again. It is processed on the worker's next check.")
    return redirect(url_for("web.message", message_id=message_id))
