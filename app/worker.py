"""The service loop: read every mailbox that is due, process the new files, sleep."""

import logging
import os
import time
from datetime import datetime, timedelta

from .core import store
from .mail.graph import Graph
from .mail.ingest import MailboxSync
from .odoo.client import ODOO_DATETIME, Odoo, utc_now
from .flows.pipeline import process

_logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 60
# Read by its own worker (app/payables/worker.py) into its own tables, never by this loop:
# here its mail would reach the Odoo pipeline.
PAYABLES = "freight_payables"
# Several workers can run side by side, one per mailbox purpose: WORKER_PURPOSES (comma-separated) says
# which this one reads and processes; unset, it does them all, as a single worker.
BUSY_TIMEOUT_MS = 120_000


def worker_purposes():
    """The mailbox purposes this worker handles, from WORKER_PURPOSES; None for all."""
    purposes = [p.strip() for p in os.environ.get("WORKER_PURPOSES", "").split(",") if p.strip()]
    return tuple(purposes) or None


def is_due(mailbox, now):
    if mailbox["sync_requested"]:
        return True
    if not mailbox["auto_sync"]:
        return False
    if not mailbox["last_sync"]:
        return True
    last = datetime.strptime(mailbox["last_sync"], ODOO_DATETIME)
    return last + timedelta(minutes=mailbox["sync_interval"] or 0) <= now


def read_mailboxes(db, now=None, after_each=None, purposes=None):
    """One pass over the active mailboxes (of ``purposes``, when given). A failing mailbox never stops
    the others. ``after_each`` runs after every mailbox read, so a long read never holds up processing."""
    now = now or utc_now()
    for mailbox in store.active_mailboxes(db):
        if mailbox["purpose"] == PAYABLES or not is_due(mailbox, now):
            continue
        if purposes and mailbox["purpose"] not in purposes:
            continue
        try:
            def save_tokens(values, mailbox_id=mailbox["id"]):
                store.update_mailbox(db, mailbox_id, values)
            MailboxSync(db, Graph(mailbox, save_tokens), mailbox).run()
        except Exception:
            _logger.exception("Mailbox %s (%s) failed", mailbox["id"], mailbox["name"])
        if after_each:
            after_each()


def process_files(db, purposes=None):
    """Hand the new files (of ``purposes``, when given) to the pipeline once per Odoo connection that is
    on. A connection that cannot be reached is left for the next run; the others carry on."""
    mine = f"AND b.purpose IN ({', '.join('?' * len(purposes))})" if purposes else ""
    before = {row["id"]: (row["state"], row["note"]) for row in db.execute(
        "SELECT m.id, m.state, m.note FROM message m JOIN mailbox b ON b.id = m.mailbox_id "
        f"WHERE m.state IN ('new', 'review') {mine}", purposes or ())}
    for connection in store.connections(db, on=True):
        if not store.is_complete(connection):
            continue
        try:
            odoo = Odoo.from_connection(db, connection)
        except Exception as exc:
            _logger.warning("Cannot reach Odoo %s, its files wait for the next run: %s", connection["name"], exc)
            continue
        process(db, odoo, connection["id"], purposes)
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


def run_once(db, purposes=None):
    purposes = purposes or worker_purposes()
    if purposes:
        read_mailboxes(db, after_each=lambda: process_files(db, purposes), purposes=purposes)
        process_files(db, purposes)
    else:  # a single worker for every mailbox, as before
        read_mailboxes(db, after_each=lambda: process_files(db))
        process_files(db)
    store.prune_activity(db)


def run_forever():
    db = store.connect()
    # Other workers write too: wait for their short transactions rather than fail.
    db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    _logger.info("Worker for %s", ", ".join(worker_purposes() or ("every mailbox",)))
    while True:
        try:
            run_once(db)
        except Exception:
            _logger.exception("Pass failed; trying again after the pause")
        time.sleep(store.get_setting(db, "poll_seconds", int) or DEFAULT_POLL_SECONDS)
