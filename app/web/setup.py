"""Mailboxes, vendor mappings and subject formats: their lists and forms; a mailbox's actions (read
now, duplicate, clear, connect Outlook) and run history."""

import secrets
import sqlite3
from pathlib import Path

from flask import abort, flash, g, redirect, render_template, request, session, url_for

from . import bp
from .base import audit
from .forms import (_form, column_defaults, describe_changes, mapping_problems, odoo_options, parse_form,
                    record_label, secret_hints, sections, validate)
from .ui import group_by, group_rows
from ..core import store
from ..mail import graph


STATUS_LABELS = {"new": "Never read", "running": "Reading now", "success": "Read", "partial": "Partly read",
                 "error": "Failed"}


def group_options(form):
    """Group By for a list: its choice fields, yes/no fields and Odoo fields (on the name kept beside
    them), and a mailbox's read status. {key: (label, value of a row, label of a value)}."""
    options = {}
    for f in form.fields:
        if f.kind == "select":
            options[f.name] = (f.label, lambda r, n=f.name: r[n], lambda v, o=dict(f.options): o.get(v, v))
        elif f.kind == "bool":
            options[f.name] = (f.label, lambda r, n=f.name: bool(r[n]), lambda v: "Yes" if v else "No")
        elif f.kind == "odoo" and f.name_column:
            options[f.name] = (f.label, lambda r, n=f.name_column: r[n], None)
    if form.table == "mailbox":
        options["status"] = ("Read status", lambda r: r["status"], lambda v: STATUS_LABELS.get(v, v))
    return options


@bp.app_template_global()
def record_list(key):
    """The rows listed beside a mailbox, vendor mapping or subject format, with their column labels,
    grouped by ?groupby=."""
    form = _form(key)
    order = "sequence, id" if form.table == "subject_format" else "id"
    rows = g.db.execute(f"SELECT * FROM {form.table} ORDER BY {order}").fetchall()
    options = group_options(form)
    groupby = group_by(options)
    groups = group_rows(rows, *options[groupby][1:]) if groupby else group_rows(rows)
    return {"rows": rows, "groups": groups, "groupby": groupby, "group_options": {k: o[0] for k, o in options.items()},
            "labels": {f.name: f.label for f in form.fields}, "form": form, "status_labels": STATUS_LABELS}


@bp.route("/<key>/")
def records(key):
    return render_template("setup/list.html", key=key, form=_form(key))


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
        if not error:
            changed = {f.name: values.get(f.name) for f in form.fields
                       if f.kind == "odoo" and values.get(f.name) != current.get(f.name)}
            problems = mapping_problems(form, changed, options)
            if problems:
                error = "Not saved: " + " ".join(problems)
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
    return render_template("setup/form.html", title=form.title, form=form, key=key, record_id=record_id,
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
    return render_template("setup/runs.html", mailbox=mailbox, runs=rows, key="mailboxes", record_id=mailbox_id)
