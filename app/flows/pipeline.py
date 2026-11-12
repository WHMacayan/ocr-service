"""After ingest: read each new email's files, then write the freight booking
or scraped bill they belong to into Odoo.

An email is marked done only after Odoo accepted its records. A file's AI
reading is saved as soon as it arrives, so a retry never asks the AI again.
Waiting emails are tried again each run from those saved readings.

The writes run once per Odoo connection that is on: the AI reading is shared, while
each connection keeps its own state in message_target and file_target, so a failure
in one connection is retried there alone.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext

from ..documents import extract
from ..core import store
from .bills import BillMatcher
from .uploads import read_uploads
from .freight import Booker
from .customs import CustomsUploader
from ..documents.text import newest_message_text, normalize_identifier

_logger = logging.getLogger(__name__)

SETTING_KEYS = ("openai_api_key", "openai_model", "freight_prompt", "invoice_prompt", "max_chars", "max_pages",
                "max_retries", "documents_per_run", "ai_parallel")
# The purposes whose files the AI reads (customs reads its own).
READ_BY_AI = ("freight_booking", "bill_matching")


def process(db, odoo, connection_id, purposes=None):
    """One pass for one connection: ``odoo`` is logged in to odoo_connection ``connection_id``.
    ``purposes``: only the emails of these mailbox purposes (one worker per purpose); all when None."""
    settings = {key: store.get_setting(db, key) for key in SETTING_KEYS}
    if not settings["openai_api_key"]:
        _logger.warning("No OpenAI key on the settings page: documents are not read.")
        return
    limit, max_retries = int(settings["documents_per_run"]), int(settings["max_retries"])
    start_from = db.execute("SELECT start_from FROM odoo_connection WHERE id = ?", (connection_id,)).fetchone()[0]
    run = _Run(db, odoo, connection_id, settings)
    # New emails first, each state with its own limit: emails waiting for review must never fill
    # the run and starve the newer emails whose bookings they are waiting for.
    # No row in message_target yet: never processed in this connection, so new there.
    rows = [row for state in ("new", "review") for row in db.execute(
        "SELECT m.*, mb.purpose, mb.company_id, mb.booking_number_pattern, t.state AS t_state, "
        "t.attempts AS t_attempts, t.email_done AS t_email_done FROM message m "
        "JOIN mailbox mb ON mb.id = m.mailbox_id "
        "LEFT JOIN message_target t ON t.message_id = m.id AND t.connection_id = ? "
        "WHERE COALESCE(t.state, CASE m.state WHEN 'skipped' THEN 'skipped' ELSE 'new' END) = ? "
        "AND COALESCE(m.received, '') >= COALESCE(?, '') "
        f"AND {_purposes_clause(purposes)} "
        "ORDER BY m.received, m.id LIMIT ?", (connection_id, state, start_from, *(purposes or ()), limit)).fetchall()]
    read_ahead(db, rows, settings)
    for row in rows:
        message = dict(row, state=row["t_state"] or "new", attempts=row["t_attempts"] or 0,
                       email_done=row["t_email_done"] or 0)
        try:
            with _source(odoo, message["id"]):
                run.message(message)
        except Exception as exc:
            _logger.exception("Email %s failed in connection %s", message["id"], connection_id)
            attempts = message["attempts"] + 1
            set_target(db, message["id"], connection_id, attempts=attempts, note=str(exc)[:2000],
                       state="failed" if attempts >= max_retries else message["state"])
        store.refresh_messages(db, [message["id"]])
        db.commit()
    if purposes is None or "bill_matching" in purposes:
        for company_id in {row["company_id"] for row in db.execute(
                "SELECT company_id FROM mailbox WHERE purpose = 'bill_matching'")}:
            run.matcher(company_id).rematch_waiting(limit)
        read_uploads(run, limit)


def _purposes_clause(purposes):
    return f"mb.purpose IN ({', '.join('?' * len(purposes))})" if purposes else "1 = 1"


def read_ahead(db, rows, settings):
    """The AI reads this run's new freight and invoice files several at once (ai_parallel), before
    anything is written: a slow reading never holds the database, and the run waits for its slowest file
    instead of the sum of them. A file not read here (an error, no text) is read in its turn, as before."""
    parallel = int(settings.get("ai_parallel") or 1)
    ids = [row["id"] for row in rows if row["purpose"] in READ_BY_AI]
    if parallel < 2 or not ids:
        return
    purpose_of = {row["id"]: row["purpose"] for row in rows}
    files = db.execute(f"SELECT id, path, message_id FROM file WHERE extracted IS NULL AND message_id IN "
                       f"({', '.join('?' * len(ids))})", ids).fetchall()

    def read(file):
        with open(file["path"], "rb") as handle:
            return extract.read_file(handle.read(), purpose_of[file["message_id"]], settings)

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(read, file): file for file in files}
        for future in as_completed(futures):
            file = futures[future]
            try:
                doc_class, values = future.result()
            except Exception:
                _logger.warning("File %s could not be read ahead; it is read in its turn", file["id"], exc_info=True)
                continue
            if doc_class is None:
                continue  # no text: its turn says so
            db.execute("UPDATE file SET doc_class = ?, extracted = ? WHERE id = ?",
                       (doc_class, json.dumps(values), file["id"]))
            db.commit()  # the AI is paid for


def set_target(db, message_id, connection_id, **values):
    """Write an email's state in one connection, making its row there on first use. No commit."""
    db.execute(f"INSERT INTO message_target (message_id, connection_id, {', '.join(values)}) "
               f"VALUES (?, ?, {', '.join('?' * len(values))}) ON CONFLICT (message_id, connection_id) DO UPDATE SET "
               + ", ".join(f"{key} = excluded.{key}" for key in values), (message_id, connection_id, *values.values()))


def _source(odoo, message_id, file_id=None):
    """Tell the Odoo client which email (and file) its next writes are for, so the service keeps a
    record of what each email changed in Odoo. Test stand-ins without working_on() are left alone."""
    working_on = getattr(odoo, "working_on", None)
    return working_on(message_id, file_id) if working_on else nullcontext()


class BookingHistory:
    """What a booking's documents said before, from this connection's own records (see freight._History)."""

    _FILES = ("FROM file f JOIN message m ON m.id = f.message_id JOIN mailbox b ON b.id = m.mailbox_id "
              "AND b.purpose = 'freight_booking' JOIN file_target t ON t.file_id = f.id AND t.connection_id = ? "
              "WHERE t.odoo_id = ? AND t.state = 'done'")

    def __init__(self, db, connection_id):
        self.db = db
        self.connection_id = connection_id

    def received(self, booking_id, source):
        if not source:
            return None
        row = self.db.execute(f"SELECT MAX(m.received) {self._FILES} AND f.filename = ?",
                              (self.connection_id, booking_id, source)).fetchone()
        return row[0] if row else None

    def pre_carriers(self, booking_id):
        found = set()
        for row in self.db.execute(f"SELECT f.extracted {self._FILES}", (self.connection_id, booking_id)):
            values = json.loads(row["extracted"] or "null") or {}
            found |= {normalize_identifier(values[k]) for k in ("pre_carrier_vessel", "pre_carrier_voyage") if values.get(k)}
        return found


class _Run:
    def __init__(self, db, odoo, connection_id, settings):
        self.db = db
        self.odoo = odoo
        self.connection_id = connection_id
        self.settings = settings
        self.mappings = db.execute("SELECT * FROM vendor_mapping WHERE active = 1").fetchall()
        self.formats = db.execute(
            "SELECT * FROM subject_format WHERE active = 1 ORDER BY sequence, id").fetchall()
        self._bookers, self._matchers = {}, {}
        self.customs = CustomsUploader(db, odoo, connection_id, int(settings["max_pages"]))

    def booker(self, company_id):
        if company_id not in self._bookers:
            self._bookers[company_id] = Booker(self.odoo, company_id, self.mappings, self.newest_confirmation,
                                               BookingHistory(self.db, self.connection_id))
        return self._bookers[company_id]

    def matcher(self, company_id):
        if company_id not in self._matchers:
            self._matchers[company_id] = BillMatcher(self.odoo, company_id, self.mappings)
        return self._matchers[company_id]

    def newest_confirmation(self, doc, booking_id):
        """Whether no booking confirmation already on this booking was received later."""
        later = self.db.execute(
            "SELECT 1 FROM file f JOIN message m ON m.id = f.message_id JOIN file_target t ON t.file_id = f.id "
            "AND t.connection_id = ? WHERE t.odoo_id = ? AND f.id != ? AND f.doc_class = 'booking_confirmation' "
            "AND t.state = 'done' AND m.received > ? LIMIT 1",
            (self.connection_id, booking_id, doc.get("file_id") or 0, doc["received"] or "")).fetchone()
        return not later

    def message(self, message):
        freight = message["purpose"] == "freight_booking"
        body = message["body_text"]
        if body is None:
            body = newest_message_text(message["body_html"])
            self.db.execute("UPDATE message SET body_text = ? WHERE id = ?", (body, message["id"]))
        base = {"sender": message["sender"] or "", "subject": message["subject"] or "",
                "received": message["received"]}
        waiting = []
        for file in self.db.execute(
                "SELECT f.* FROM file f LEFT JOIN file_target t ON t.file_id = f.id AND t.connection_id = ? "
                "WHERE f.message_id = ? AND COALESCE(t.state, 'new') IN ('new', 'waiting') ORDER BY f.id",
                (self.connection_id, message["id"])).fetchall():
            if message["purpose"] == "customs_entry":
                with _source(self.odoo, message["id"], file["id"]):
                    self._file(file, *self.customs.take(file, message["received"]))
                continue
            doc_class, values = file["doc_class"], json.loads(file["extracted"] or "null")
            if values is None:
                self.db.commit()  # never hold the database while the AI reads
                with open(file["path"], "rb") as handle:
                    doc_class, values = extract.read_file(handle.read(), message["purpose"], self.settings)
                if doc_class is None:
                    self._file(file, "skipped", "No readable text in this file.")
                    continue
                self.db.execute("UPDATE file SET doc_class = ?, extracted = ? WHERE id = ?",
                                (doc_class, json.dumps(values), file["id"]))
                self.db.commit()  # the AI is paid for: keep its reading whatever happens next
            doc = dict(base, doc_class=doc_class, values=values, source=file["filename"], file_id=file["id"])
            with _source(self.odoo, message["id"], file["id"]):
                state, note, odoo_id = self.route(message, doc)
            self._file(file, state, note, odoo_id)
            if state == "waiting":
                waiting.append(f"{file['filename']}: {note}")
        if freight and not message["email_done"]:
            values = extract.email_values(self.formats, message["booking_number_pattern"], base["subject"], body,
                                          base["sender"], message["received"])
            state = "done"
            if values:
                doc = dict(base, doc_class="email_message", values=values,
                           source=f"Email: {base['subject']}"[:120], file_id=None)
                state, note, _odoo_id = self.booker(message["company_id"]).take(doc)
                if state == "waiting":
                    waiting.append(f"Email body: {note}")
            if state == "done":
                set_target(self.db, message["id"], self.connection_id, email_done=1)
        set_target(self.db, message["id"], self.connection_id, state="review" if waiting else "done",
                   note="\n".join(waiting) or None)

    def route(self, message, doc):
        if message["purpose"] == "freight_booking":
            if doc["doc_class"] in extract.NOT_RELEVANT:
                return "skipped", f"Not relevant: {doc['doc_class']}.", None
            return self.booker(message["company_id"]).take(doc)
        if doc["doc_class"] != "supplier_invoice":
            return "skipped", "Not an invoice.", None
        return self.matcher(message["company_id"]).take(doc)

    def _file(self, file, state, note, odoo_id=None):
        self.db.execute(
            "INSERT INTO file_target (file_id, connection_id, state, note, odoo_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (file_id, connection_id) DO UPDATE SET state = excluded.state, note = excluded.note, "
            "odoo_id = excluded.odoo_id", (file["id"], self.connection_id, state, note, odoo_id))
