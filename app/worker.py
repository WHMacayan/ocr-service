"""The service loop: read every mailbox that is due, process the new files, sleep."""

import logging
import time
from datetime import datetime, timedelta

from .core import store
from .mail.graph import Graph
from .mail.ingest import MailboxSync
from .odoo.client import ODOO_DATETIME, Odoo, utc_now
from .flows.pipeline import process

_logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 60


def is_due(mailbox, now):
    if mailbox["sync_requested"]:
        return True
    if not mailbox["auto_sync"]:
        return False
    if not mailbox["last_sync"]:
        return True
    last = datetime.strptime(mailbox["last_sync"], ODOO_DATETIME)
    return last + timedelta(minutes=mailbox["sync_interval"] or 0) <= now


def read_mailboxes(db, now=None, after_each=None):
    """One pass over the active mailboxes. A failing mailbox never stops the others.
    ``after_each`` runs after every mailbox read, so a long read never holds up processing."""
    now = now or utc_now()
    for mailbox in store.active_mailboxes(db):
        if not is_due(mailbox, now):
            continue
        try:
            def save_tokens(values, mailbox_id=mailbox["id"]):
                store.update_mailbox(db, mailbox_id, values)
            MailboxSync(db, Graph(mailbox, save_tokens), mailbox).run()
        except Exception:
            _logger.exception("Mailbox %s (%s) failed", mailbox["id"], mailbox["name"])
        if after_each:
            after_each()


def process_files(db):
    """Hand the new files to the pipeline once per Odoo connection that is on. A connection that
    cannot be reached is left for the next run; the others carry on."""
    before = {row["id"]: (row["state"], row["note"]) for row in db.execute(
        "SELECT id, state, note FROM message WHERE state IN ('new', 'review')")}
    for connection in store.connections(db, on=True):
        if not store.is_complete(connection):
            continue
        try:
            odoo = Odoo.from_connection(db, connection)
        except Exception as exc:
            _logger.warning("Cannot reach Odoo %s, its files wait for the next run: %s", connection["name"], exc)
            continue
        process(db, odoo, connection["id"])
    log_outcomes(db, before)


OUTCOMES = {"done": ("Processed", "info"), "review": ("Needs review", "warning"),
            "failed": ("Failed", "error"), "skipped": ("Skipped", "info")}


def log_outcomes(db, before):
    """An activity entry for every email whose state or note the pipeline changed."""
    if not before:
        return
    marks = ", ".join("?" * len(before))
    with db:
        db.execute(f"UPDATE message SET processed_at = CURRENT_TIMESTAMP WHERE id IN ({marks}) "
                   "AND state IN ('done', 'review', 'failed', 'skipped')", tuple(before))
    for row in db.execute(f"SELECT id, subject, state, note, attempts FROM message WHERE id IN ({marks})",
                          tuple(before)).fetchall():
        if (row["state"], row["note"]) == before[row["id"]] or row["state"] not in OUTCOMES:
            continue
        verb, level = OUTCOMES[row["state"]]
        if row["state"] == "review" and before[row["id"]][0] == "review":
            verb = "Still needs review"
        store.log_activity(db, "processing", f"{verb}: {row['subject'] or 'No subject'}", row["note"],
                           level=level, link=f"/messages/{row['id']}")


def run_once(db):
    read_mailboxes(db, after_each=lambda: process_files(db))
    process_files(db)
    store.prune_activity(db)


def run_forever():
    db = store.connect()
    while True:
        try:
            run_once(db)
        except Exception:
            _logger.exception("Pass failed; trying again after the pause")
        time.sleep(store.get_setting(db, "poll_seconds", int) or DEFAULT_POLL_SECONDS)
