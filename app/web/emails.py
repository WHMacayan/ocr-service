"""The emails: the list beside every email, an email with its conversation, processing it again."""

from flask import abort, flash, g, redirect, render_template, request, url_for

from . import bp, search
from .base import audit, OFF_ROUTE, reference_url, ROUTE, STATE_LABELS
from ..core import store
from ..odoo.client import MODEL_NAMES


@bp.route("/")
def home():
    return redirect(url_for("web.messages"))


# ── emails ───────────────────────────────────────────────────────────

EMAILS_PAGE = 100


def email_list():
    """The email list beside every Emails page: ?state= and ?q= filter it, ?offset= is the next page."""
    state = request.args.get("state")
    q = (request.args.get("q") or "").strip()
    offset = max(request.args.get("offset", 0, type=int), 0)
    where, args = search.email_where(search.parse(q))
    if state in STATE_LABELS:
        where, args = f"{where} AND m.state = ?", (*args, state)
    rows = g.db.execute(
        "SELECT m.*, b.name AS mailbox_name, (SELECT COUNT(*) FROM file f WHERE f.message_id = m.id) AS files, "
        "(SELECT COUNT(DISTINCT t.internet_message_id) FROM message t WHERE t.conversation_id = m.conversation_id) AS thread "
        f"FROM message m JOIN mailbox b ON b.id = m.mailbox_id WHERE {where} "
        "ORDER BY m.received DESC, m.id DESC LIMIT ? OFFSET ?", (*args, EMAILS_PAGE + 1, offset)).fetchall()
    shown = rows[:EMAILS_PAGE]
    counts = dict(g.db.execute("SELECT state, COUNT(*) FROM message GROUP BY state").fetchall())
    return {"rows": shown, "more": offset + EMAILS_PAGE if len(rows) > EMAILS_PAGE else None, "offset": offset,
            "state": state if state in STATE_LABELS else None, "q": q, "counts": counts, "total": sum(counts.values()),
            "route": ROUTE, "off_route": OFF_ROUTE, "in_odoo": odoo_records([r["id"] for r in shown]),
            "filters": {"state": state if state in STATE_LABELS else None, "q": q or None}}


@bp.route("/messages")
def messages():
    mailboxes = g.db.execute("SELECT COUNT(*) FROM mailbox").fetchone()[0]
    return render_template("emails/messages.html", mailboxes=mailboxes, **email_list())


# The Odoo records an email is shown with; changes to their lines (containers, conflicts…) are in its detail.
MAIN_MODELS = {"b2b.ocr.booking": "Booking", "b2b.ocr.scraped.bill": "Scraped bill"}
ACTION_RANK = {"created": 3, "updated": 2, "linked": 1, "deleted": 0}


def odoo_records(message_ids):
    """Per email id: {"records": [{kind, label, link, action}], "other": n changes to their lines}."""
    if not message_ids:
        return {}
    marks = ", ".join("?" * len(message_ids))
    out = {}
    mine, args = store.reference_changes(g.db)
    for r in g.db.execute(f"SELECT message_id, model, record_id, label, link, action FROM odoo_change c "
                          f"WHERE message_id IN ({marks}) AND {mine} ORDER BY id", (*message_ids, *args)):
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
    odoo_url = reference_url()
    for r in g.db.execute(f"SELECT f.message_id, f.odoo_id, b.purpose FROM file f JOIN message m ON m.id = f.message_id "
                          f"JOIN mailbox b ON b.id = m.mailbox_id WHERE f.message_id IN ({marks}) "
                          "AND f.odoo_id IS NOT NULL", message_ids):
        model = "b2b.ocr.booking" if r["purpose"] == "freight_booking" else "b2b.ocr.scraped.bill"
        entry = out.setdefault(r["message_id"], {"records": {}, "other": 0})
        entry["records"].setdefault((model, r["odoo_id"]), {
            "kind": MAIN_MODELS[model], "model": model, "id": r["odoo_id"], "label": f"#{r['odoo_id']}", "action": "linked",
            "link": f"{odoo_url}/web#model={model}&id={r['odoo_id']}&view_type=form" if odoo_url else None})
    return {mid: {"records": list(e["records"].values()), "other": e["other"]} for mid, e in out.items()}


@bp.route("/messages/<int:message_id>")
def message(message_id):
    row = g.db.execute("SELECT m.*, b.name AS mailbox_name FROM message m JOIN mailbox b "
                       "ON b.id = m.mailbox_id WHERE m.id = ?", (message_id,)).fetchone() or abort(404)
    files = g.db.execute("SELECT * FROM file WHERE message_id = ? ORDER BY id", (message_id,)).fetchall()
    changes = g.db.execute("SELECT c.*, f.filename, o.name AS connection_name FROM odoo_change c "
                           "LEFT JOIN file f ON f.id = c.file_id LEFT JOIN odoo_connection o ON o.id = c.connection_id "
                           "WHERE c.message_id = ? ORDER BY c.id", (message_id,)).fetchall()
    targets = g.db.execute(
        "SELECT o.id, o.name, o.is_production, o.active, t.state, t.attempts, t.note FROM odoo_connection o "
        "LEFT JOIN message_target t ON t.connection_id = o.id AND t.message_id = ? "
        f"WHERE t.message_id IS NOT NULL OR (o.active = 1 AND COALESCE(?, '') >= COALESCE(o.start_from, '') "
        f"AND ? != 'skipped') ORDER BY {store.CONNECTION_ORDER}",
        (message_id, row["received"], row["state"])).fetchall()
    return render_template("emails/message.html", message=row, files=files, changes=changes, model_names=MODEL_NAMES,
                           written=odoo_records([message_id]).get(message_id), targets=targets,
                           thread=conversation(row), current_id=message_id, **email_list())


THREAD_LIMIT = 200


def conversation(row):
    """The email's whole Outlook conversation, oldest first: one entry per email, with every
    mailbox's copy of it (the same email is read once by each mailbox it reaches)."""
    rows = g.db.execute(
        "SELECT m.id, m.subject, m.sender, m.received, m.state, m.internet_message_id, b.name AS mailbox_name "
        "FROM message m JOIN mailbox b ON b.id = m.mailbox_id "
        "WHERE (m.conversation_id = ? AND m.conversation_id != '') OR m.internet_message_id = ? OR m.id = ? "
        "ORDER BY m.received, m.id LIMIT ?",
        (row["conversation_id"], row["internet_message_id"] or None, row["id"], THREAD_LIMIT)).fetchall()
    emails = {}
    for r in rows:
        key = r["internet_message_id"] or f"id:{r['id']}"
        emails.setdefault(key, {"sender": r["sender"], "subject": r["subject"], "received": r["received"],
                                "copies": []})["copies"].append(r)
    return list(emails.values())


@bp.route("/messages/<int:message_id>/retry", methods=["POST"])
def retry(message_id):
    """Process the email again, in one connection or in all; its files keep their cached extraction."""
    row = g.db.execute("SELECT subject FROM message WHERE id = ?", (message_id,)).fetchone() or abort(404)
    connection_id = request.form.get("connection_id", type=int)
    only = store.connection(g.db, connection_id) if connection_id else None
    audit("processing", f"Queued again by hand{' in ' + only['name'] if only else ''}: {row['subject'] or 'No subject'}",
          link=url_for("web.message", message_id=message_id))
    scope, args = ("AND connection_id = ?", (connection_id,)) if only else ("", ())
    with g.db:
        # email_done = 0: the email text is read again too, so the record of what it changes in Odoo is complete.
        g.db.execute("UPDATE message SET state = 'new', attempts = 0, note = NULL, email_done = 0 WHERE id = ?",
                     (message_id,))
        g.db.execute(f"UPDATE message_target SET state = 'new', attempts = 0, note = NULL, email_done = 0 "
                     f"WHERE message_id = ? {scope}", (message_id, *args))
        g.db.execute(f"UPDATE file_target SET state = 'new', note = NULL WHERE file_id IN "
                     f"(SELECT id FROM file WHERE message_id = ?) {scope}", (message_id, *args))
        g.db.execute("UPDATE file SET state = 'new', note = NULL WHERE message_id = ?", (message_id,))
        store.refresh_messages(g.db, [message_id])
    flash("Queued again. It is processed on the worker's next check.")
    keep = {k: request.args[k] for k in ("state", "q") if request.args.get(k)}
    return redirect(url_for("web.message", message_id=message_id, **keep))
