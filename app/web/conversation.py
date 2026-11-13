"""A record's side of the story, for its page: the emails that fed it oldest first, each with its files and
what it wrote to this record; every file with what became of it; and the emails of a purpose still waiting
on a person. Read only."""

from flask import g

from ..core import store


def conversation(db, message_ids, model, record_id):
    """(emails, files): the emails, oldest first, each with .files and .wrote (its changes to this record);
    and every file of those emails with .fed (the change it made here, or None)."""
    ids = sorted({m if isinstance(m, int) else m["id"] for m in message_ids})
    if not ids:
        return [], []
    marks = ", ".join("?" * len(ids))
    # The rest of their threads too: a reply still waiting for review has written nothing yet, but belongs here.
    ids = sorted(set(ids) | {r[0] for r in db.execute(
        f"SELECT id FROM message WHERE conversation_id IN (SELECT conversation_id FROM message "
        f"WHERE id IN ({marks}) AND conversation_id IS NOT NULL AND conversation_id != '')", ids)})
    marks = ", ".join("?" * len(ids))
    emails = [dict(r, files=[], wrote=[]) for r in db.execute(
        f"SELECT m.id, m.subject, m.sender, m.received, m.state, m.note, m.body_text, b.name AS mailbox_name "
        f"FROM message m JOIN mailbox b ON b.id = m.mailbox_id WHERE m.id IN ({marks}) "
        f"ORDER BY m.received, m.id", ids)]
    by_id = {e["id"]: e for e in emails}
    mine, args = store.reference_changes(db)
    changes = db.execute(f"SELECT message_id, file_id, action, detail FROM odoo_change c WHERE model = ? AND "
                         f"record_id = ? AND message_id IN ({marks}) AND {mine} ORDER BY id",
                         (model, record_id, *ids, *args)).fetchall()
    fed = {}
    for c in changes:
        by_id[c["message_id"]]["wrote"].append(c)
        if c["file_id"]:
            fed.setdefault(c["file_id"], c["action"])
    files = []
    for f in db.execute(f"SELECT id, message_id, filename, doc_class, state, note, odoo_id FROM file "
                        f"WHERE message_id IN ({marks}) ORDER BY message_id, id", ids):
        # A file whose record is this one but with no recorded change: written before changes were kept.
        f = dict(f, fed=fed.get(f["id"]) or ("written" if f["odoo_id"] == record_id else None))
        by_id[f["message_id"]]["files"].append(f)
        files.append(f)
    for e in emails:
        e["preview"] = " ".join((e["body_text"] or "").split())[:180]
    return emails, files


def waiting(db, purpose):
    """{state: count}: this purpose's emails that need a person (review, failed)."""
    return dict(db.execute(
        "SELECT m.state, COUNT(*) FROM message m JOIN mailbox b ON b.id = m.mailbox_id "
        "WHERE b.purpose = ? AND m.state IN ('review', 'failed') GROUP BY m.state", (purpose,)).fetchall())


def pane_context(items, page, error, purpose, current=None):
    """What the list beside a record needs, the same for bookings, scraped bills and customs."""
    for item in items:
        item["current"] = item["id"] == current
    return {"items": items, "pane_pager": page, "pane_error": error, "waiting": waiting(g.db, purpose) if purpose else {}}
