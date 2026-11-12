"""Freight Payables: the mail its mailboxes stored, and its settings (the flows switched on, the export folder).

Reads only the payables_* tables; the Odoo pages never show this mail."""

from datetime import datetime, timezone
from pathlib import Path

from flask import abort, flash, g, redirect, render_template, request, send_file, url_for

from . import bp
from .base import audit
from .ui import group_by, group_list, group_rows, pager
from ..documents.text import newest_message_text
from ..payables import exports, schedule
from ..worker import PAYABLES


def payables_mailboxes():
    return g.db.execute("SELECT id, name, active, status, last_sync FROM mailbox WHERE purpose = ? ORDER BY name",
                        (PAYABLES,)).fetchall()


@bp.app_template_global()
def payables_counts():
    """What the Freight Payables tabs show beside their names."""
    return {"emails": g.db.execute("SELECT COUNT(*) FROM payables_message").fetchone()[0],
            "mailboxes": len(payables_mailboxes())}


@bp.route("/payables")
def payables():
    return redirect(url_for("web.payables_emails"))


# ── emails ───────────────────────────────────────────────────────────

# Group By: the column offered, and the SQL its emails are grouped and counted on.
PAYABLES_GROUPS = {"mailbox": "Mailbox", "sender": "Sender domain", "day": "Day received"}
GROUP_SQL = {"mailbox": "b.name", "sender": "m.sender_domain", "day": "date(m.received)"}
EMAIL_SQL = ("SELECT m.id, m.subject, m.sender, m.received, m.state, m.created_at, m.updated_at, b.name AS mailbox_name, "
             "(SELECT COUNT(*) FROM payables_message_file x WHERE x.message_id = m.id) AS files "
             "FROM payables_message m JOIN mailbox b ON b.id = m.mailbox_id WHERE ")


def _where(query):
    """Every word must be in the subject, the sender or a file's name."""
    clauses, args = ["1 = 1"], []
    for word in query.split():
        clauses.append("(m.subject LIKE ? OR m.sender LIKE ? OR EXISTS (SELECT 1 FROM payables_message_file x "
                       "WHERE x.message_id = m.id AND x.filename LIKE ?))")
        args += [f"%{word}%"] * 3
    return " AND ".join(clauses), args


@bp.route("/payables/emails")
def payables_emails():
    query = (request.args.get("q") or "").strip()
    where, args = _where(query)
    groupby = group_by(PAYABLES_GROUPS)
    page, rows = None, []
    if groupby:
        column = f"NULLIF({GROUP_SQL[groupby]}, '')"
        totals = dict(g.db.execute(f"SELECT {column}, COUNT(*) FROM payables_message m JOIN mailbox b "
                                   f"ON b.id = m.mailbox_id WHERE {where} GROUP BY 1", args).fetchall())
        groups = group_list(totals, lambda value, offset, limit: g.db.execute(
            f"{EMAIL_SQL}{where} AND {column} IS ? ORDER BY m.received DESC, m.id DESC LIMIT ? OFFSET ?",
            (*args, value, limit, offset)).fetchall(), reverse=groupby == "day")
    else:
        page = pager(g.db.execute(f"SELECT COUNT(*) FROM payables_message m JOIN mailbox b ON b.id = m.mailbox_id "
                                  f"WHERE {where}", args).fetchone()[0])
        rows = g.db.execute(f"{EMAIL_SQL}{where} ORDER BY m.received DESC, m.id DESC LIMIT ? OFFSET ?",
                            (*args, page["limit"], page["offset"])).fetchall()
        groups = group_rows(rows)
    return render_template("payables/emails.html", title="Freight Payables", tab="emails", groups=groups, rows=rows,
                           query=query, pager=page, groupby=groupby, group_options=PAYABLES_GROUPS,
                           mailboxes=payables_mailboxes())


@bp.route("/payables/emails/<int:message_id>")
def payables_email(message_id):
    row = g.db.execute("SELECT m.*, b.name AS mailbox_name FROM payables_message m JOIN mailbox b "
                       "ON b.id = m.mailbox_id WHERE m.id = ?", (message_id,)).fetchone() or abort(404)
    files = g.db.execute("SELECT f.id, f.kind, f.size, x.filename, x.is_inline FROM payables_message_file x "
                         "JOIN payables_file f ON f.id = x.file_id WHERE x.message_id = ? ORDER BY x.is_inline, x.filename",
                         (message_id,)).fetchall()
    thread = g.db.execute("SELECT id, subject, sender, received FROM payables_message WHERE conversation_id = ? "
                          "AND conversation_id != '' ORDER BY received, id", (row["conversation_id"] or "",)).fetchall()
    return render_template("payables/email.html", title=row["subject"] or "No subject", tab="emails", message=row,
                           files=files, thread=thread, text=newest_message_text(row["unique_body_html"] or row["body_html"]))


@bp.route("/payables/emails/<int:message_id>/files/<int:file_id>")
def payables_file(message_id, file_id):
    """A file as it came on this email, under the name it had there."""
    row = g.db.execute("SELECT f.path, f.kind, f.sha256, x.filename FROM payables_message_file x JOIN payables_file f "
                       "ON f.id = x.file_id WHERE x.message_id = ? AND x.file_id = ?",
                       (message_id, file_id)).fetchone() or abort(404)
    path = Path(row["path"])
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=row["filename"] or f"{row['sha256'][:12]}.{row['kind']}")


# ── settings ─────────────────────────────────────────────────────────

FLOW_LABELS = {"invoices_enabled": "Invoices", "audit_enabled": "Audit"}
DEFAULTS = {"invoices_enabled": 1, "audit_enabled": 1, "export_folder": None, "export_auto": 0, "export_every": 1,
            "export_unit": "days", "export_next": None, "export_last_at": None, "export_last_run": None}
SAVED = ("invoices_enabled", "audit_enabled", "export_folder", "export_auto", "export_every", "export_unit", "export_next")


def setting(mailbox_id):
    """A payables mailbox's settings: both flows on and automatic export off until changed."""
    row = g.db.execute("SELECT * FROM payables_setting WHERE mailbox_id = ?", (mailbox_id,)).fetchone()
    return dict(row) if row else dict(DEFAULTS, mailbox_id=mailbox_id)


def _repeat(s):
    """The schedule as people read it: Every 1 Days, Once only."""
    return "Once only" if s["export_unit"] == "once" else f"Every {s['export_every']} {schedule.UNITS[s['export_unit']]}"


def _schedule(form):
    """(the schedule from the form, None) or (None, why not). Switched on with no next export: one
    interval from now. The next export comes as UTC from the page script, else as typed (UTC)."""
    unit = form.get("export_unit") if form.get("export_unit") in schedule.UNITS else "days"
    every = form.get("export_every", 1, type=int)
    if unit != "once" and not 1 <= every <= 999:
        return None, "Repeat every must be a whole number from 1 to 999."
    typed = form.get("export_next") or form.get("export_next_local")
    next_at = schedule.parse(typed)
    if typed and not next_at:
        return None, "The next export must be a date and time."
    auto = int("export_auto" in form)
    if auto and not next_at:
        next_at = schedule.first(max(every, 1), unit, datetime.now(timezone.utc).replace(tzinfo=None))
    return {"export_auto": auto, "export_every": max(every, 1), "export_unit": unit,
            "export_next": next_at.strftime(schedule.STORED) if next_at else None}, None


@bp.route("/payables/settings", methods=["GET", "POST"])
def payables_settings():
    mailboxes = payables_mailboxes()
    typed = {}
    if request.method == "POST":
        mailbox = next((m for m in mailboxes if m["id"] == request.form.get("mailbox_id", type=int)), None) or abort(404)
        old = setting(mailbox["id"])
        new = {"invoices_enabled": int("invoices_enabled" in request.form),
               "audit_enabled": int("audit_enabled" in request.form),
               "export_folder": (request.form.get("export_folder") or "").strip() or None}
        error = None
        if new["export_folder"]:
            new["export_folder"], error = exports.check(new["export_folder"])
        planned, schedule_error = _schedule(request.form)
        error = error or schedule_error
        if error:
            new.update(export_folder=request.form.get("export_folder"), export_auto=int("export_auto" in request.form),
                       export_every=request.form.get("export_every"), export_unit=request.form.get("export_unit"),
                       export_next=None)
            flash(f"Not saved: {error}", "error")
            typed[mailbox["id"]] = dict(old, **new)
        else:
            new.update(planned)
            with g.db:
                g.db.execute(f"INSERT INTO payables_setting (mailbox_id, {', '.join(SAVED)}) "
                             f"VALUES (?, {', '.join('?' * len(SAVED))}) ON CONFLICT (mailbox_id) DO UPDATE SET "
                             + ", ".join(f"{c} = excluded.{c}" for c in SAVED),
                             (mailbox["id"], *(new[c] for c in SAVED)))
            changes = [f"{FLOW_LABELS[k]}: {'on' if old[k] else 'off'} → {'on' if new[k] else 'off'}"
                       for k in FLOW_LABELS if bool(old[k]) != bool(new[k])]
            if old["export_folder"] != new["export_folder"]:
                changes.append(f"Export folder: {exports.full_path(old['export_folder']) if old['export_folder'] else '(none)'}"
                               f" → {exports.full_path(new['export_folder']) if new['export_folder'] else '(none)'}")
            if bool(old["export_auto"]) != bool(new["export_auto"]):
                changes.append(f"Automatic export: {'on' if old['export_auto'] else 'off'} → {'on' if new['export_auto'] else 'off'}")
            if _repeat(old) != _repeat(new):
                changes.append(f"Repeat: {_repeat(old)} → {_repeat(new)}")
            if old["export_next"] != new["export_next"]:
                changes.append(f"Next export (UTC): {old['export_next'] or '(none)'} → {new['export_next'] or '(none)'}")
            if changes:
                audit("settings", f"Updated Freight Payables settings of {mailbox['name']}", "\n".join(changes),
                      link=url_for("web.payables_settings"))
            flash("Freight Payables settings saved.")
            return redirect(url_for("web.payables_settings"))
    return render_template("payables/settings.html", title="Freight Payables settings", tab="settings",
                           mailboxes=[(m, typed.get(m["id"]) or setting(m["id"])) for m in mailboxes],
                           shared=exports.root_name(), default_folder=exports.default_folder(), units=schedule.UNITS,
                           suggestions=exports.suggestions() if exports.host_root() else [], full_path=exports.full_path)
