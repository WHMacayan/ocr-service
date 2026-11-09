"""The Freight Payables loop: read every due Freight Payables mailbox, sleep.

It runs as its own process (the payables-worker service), so a slow payables run never
delays the booking, bill and customs mailboxes, and it needs no Odoo connection.
"""

import logging
import time

from ..core import store
from ..odoo.client import utc_now
from ..worker import DEFAULT_POLL_SECONDS, PAYABLES, is_due

_logger = logging.getLogger(__name__)

# The booking pipeline keeps the write lock while it waits for the AI; wait for it rather than fail.
BUSY_TIMEOUT_MS = 120_000


def connect():
    db = store.connect()
    db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return db


def _reader():
    from ..mail.graph import Graph
    from .ingest import PayablesSync
    return PayablesSync, Graph


def read_payables(db, now=None, sync_class=None):
    """One pass over the due Freight Payables mailboxes. A failing mailbox never stops the others."""
    now = now or utc_now()
    mailboxes = [box for box in store.active_mailboxes(db) if box["purpose"] == PAYABLES and is_due(box, now)]
    if not mailboxes:
        return
    if sync_class:
        from ..mail.graph import Graph
    else:
        sync_class, Graph = _reader()
    for mailbox in mailboxes:
        try:
            def save_tokens(values, mailbox_id=mailbox["id"]):
                store.update_mailbox(db, mailbox_id, values)
            sync_class(db, Graph(mailbox, save_tokens), mailbox).run()
        except Exception:
            _logger.exception("Freight Payables mailbox %s (%s) failed", mailbox["id"], mailbox["name"])


def run_payables_once(db, sync_class=None):
    read_payables(db, sync_class=sync_class)


def run_payables_forever():
    db = connect()
    while True:
        try:
            run_payables_once(db)
        except Exception:
            _logger.exception("Freight Payables pass failed; trying again after the pause")
        time.sleep(store.get_setting(db, "poll_seconds", int) or DEFAULT_POLL_SECONDS)
