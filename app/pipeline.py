"""After ingest: read each new email's files, then write the freight booking
or scraped bill they belong to into Odoo.

An email is marked done only after Odoo accepted its records. A file's AI
reading is saved as soon as it arrives, so a retry never asks the AI again.
Waiting emails are tried again each run from those saved readings.
"""

import json
import logging
from contextlib import nullcontext

from . import extract, store
from .bills import BillMatcher
from .booking import Booker
from .customs import CustomsUploader
from .text import newest_message_text

_logger = logging.getLogger(__name__)

SETTING_KEYS = ("openai_api_key", "openai_model", "freight_prompt", "invoice_prompt", "max_chars", "max_pages",
                "max_retries", "documents_per_run")


def process(db, odoo):
    settings = {key: store.get_setting(db, key) for key in SETTING_KEYS}
    if not settings["openai_api_key"]:
        _logger.warning("No OpenAI key on the settings page: documents are not read.")
        return
    limit, max_retries = int(settings["documents_per_run"]), int(settings["max_retries"])
    run = _Run(db, odoo, settings)
    # New emails first, each state with its own limit: emails waiting for review must never fill
    # the run and starve the newer emails whose bookings they are waiting for.
    messages = [message for state in ("new", "review") for message in db.execute(
        "SELECT m.*, mb.purpose, mb.company_id, mb.booking_number_pattern FROM message m "
        "JOIN mailbox mb ON mb.id = m.mailbox_id WHERE m.state = ? "
        "ORDER BY m.received, m.id LIMIT ?", (state, limit)).fetchall()]
    for message in messages:
        try:
            with _source(odoo, message["id"]):
                run.message(message)
        except Exception as exc:
            _logger.exception("Email %s failed", message["id"])
            attempts = message["attempts"] + 1
            db.execute("UPDATE message SET attempts = ?, note = ?, state = ? WHERE id = ?",
                       (attempts, str(exc)[:2000], "failed" if attempts >= max_retries else message["state"],
                        message["id"]))
        db.commit()
    for company_id in {row["company_id"] for row in db.execute(
            "SELECT company_id FROM mailbox WHERE purpose = 'bill_matching'")}:
        run.matcher(company_id).rematch_waiting(limit)


def _source(odoo, message_id, file_id=None):
    """Tell the Odoo client which email (and file) its next writes are for, so the service keeps a
    record of what each email changed in Odoo. Test stand-ins without working_on() are left alone."""
    working_on = getattr(odoo, "working_on", None)
    return working_on(message_id, file_id) if working_on else nullcontext()


class _Run:
    def __init__(self, db, odoo, settings):
        self.db = db
        self.odoo = odoo
        self.settings = settings
        self.mappings = db.execute("SELECT * FROM vendor_mapping WHERE active = 1").fetchall()
        self.formats = db.execute(
            "SELECT * FROM subject_format WHERE active = 1 ORDER BY sequence, id").fetchall()
        self._bookers, self._matchers = {}, {}
        self.customs = CustomsUploader(db, odoo, int(settings["max_pages"]))

    def booker(self, company_id):
        if company_id not in self._bookers:
            self._bookers[company_id] = Booker(self.odoo, company_id, self.mappings, self.newest_confirmation)
        return self._bookers[company_id]

    def matcher(self, company_id):
        if company_id not in self._matchers:
            self._matchers[company_id] = BillMatcher(self.odoo, company_id, self.mappings)
        return self._matchers[company_id]

    def newest_confirmation(self, doc, booking_id):
        """Whether no booking confirmation already on this booking was received later."""
        later = self.db.execute(
            "SELECT 1 FROM file f JOIN message m ON m.id = f.message_id WHERE f.odoo_id = ? AND f.id != ? "
            "AND f.doc_class = 'booking_confirmation' AND f.state = 'done' AND m.received > ? LIMIT 1",
            (booking_id, doc.get("file_id") or 0, doc["received"] or "")).fetchone()
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
        for file in self.db.execute("SELECT * FROM file WHERE message_id = ? AND state IN ('new', 'waiting')",
                                    (message["id"],)).fetchall():
            if message["purpose"] == "customs_entry":
                with _source(self.odoo, message["id"], file["id"]):
                    self._file(file, *self.customs.take(file, message["received"]))
                continue
            doc_class, values = file["doc_class"], json.loads(file["extracted"] or "null")
            if values is None:
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
                self.db.execute("UPDATE message SET email_done = 1 WHERE id = ?", (message["id"],))
        self.db.execute("UPDATE message SET state = ?, note = ? WHERE id = ?",
                        ("review" if waiting else "done", "\n".join(waiting) or None, message["id"]))

    def route(self, message, doc):
        if message["purpose"] == "freight_booking":
            if doc["doc_class"] in extract.NOT_RELEVANT:
                return "skipped", f"Not relevant: {doc['doc_class']}.", None
            return self.booker(message["company_id"]).take(doc)
        if doc["doc_class"] != "supplier_invoice":
            return "skipped", "Not an invoice.", None
        return self.matcher(message["company_id"]).take(doc)

    def _file(self, file, state, note, odoo_id=None):
        self.db.execute("UPDATE file SET state = ?, note = ?, odoo_id = ? WHERE id = ?",
                        (state, note, odoo_id, file["id"]))
