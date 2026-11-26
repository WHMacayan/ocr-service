"""After ingest: read each new email's files, then hand them to the job of the email's mailbox (its handler: app/<job>/
handler.py), which writes them into Odoo: a freight booking, a scraped bill, a customs entry.

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
from datetime import datetime, timezone

from app.shared.documents import extract
from app.shared.core import store
from app.shared.jobs import all_jobs, job_for
from app.shared.documents.text import newest_message_text

_logger = logging.getLogger(__name__)

SETTING_KEYS = ("openai_api_key", "openai_model", "freight_prompt", "invoice_prompt", "max_chars", "max_pages",
                "max_retries", "documents_per_run", "ai_parallel")


def read_by_ai():
    """{purpose: Reading} of the jobs whose files the AI reads (customs reads its own)."""
    return {job.purpose: job.reading for job in all_jobs() if job.reading}


def process(db, odoo, connection_id, purposes=None):
    """One pass for one connection: ``odoo`` is logged in to odoo_connection ``connection_id``.
    ``purposes``: only the emails of these mailbox purposes (one worker per purpose); all when None."""
    settings = {key: store.get_setting(db, key) for key in SETTING_KEYS}
    # Without the key only work that needs no AI goes on (customs reads its own files, review F08); the rest waits.
    has_key = bool(settings["openai_api_key"])
    if not has_key:
        _logger.warning("No OpenAI key on the settings page: documents are not read.")
    limit, max_retries = int(settings["documents_per_run"]), int(settings["max_retries"])
    start_from = db.execute("SELECT start_from FROM odoo_connection WHERE id = ?", (connection_id,)).fetchone()[0]
    run = _Run(db, odoo, connection_id, settings)
    # New emails first, each state with its own limit: emails waiting for review must never fill
    # the run and starve the newer emails whose bookings they are waiting for.
    # No row in message_target yet: never processed in this connection, so new there.
    # Waiting emails take turns, the least recently tried first: retried oldest-first, the same hundred that
    # never match came back every run and the rest never did (839 waiting in 1111, 17 Nov 2026).
    order = {"new": "m.received, m.id", "review": "COALESCE(t.processed_at, ''), m.received, m.id"}
    rows = [row for state in ("new", "review") for row in db.execute(
        "SELECT m.*, mb.purpose, mb.company_id, mb.booking_number_pattern, t.state AS t_state, "
        "t.attempts AS t_attempts, t.email_done AS t_email_done FROM message m "
        "JOIN mailbox mb ON mb.id = m.mailbox_id "
        "LEFT JOIN message_target t ON t.message_id = m.id AND t.connection_id = ? "
        "WHERE COALESCE(t.state, CASE m.state WHEN 'skipped' THEN 'skipped' ELSE 'new' END) = ? "
        "AND COALESCE(m.received, '') >= COALESCE(?, '') "
        f"AND {_purposes_clause(purposes)} "
        f"ORDER BY {order[state]} LIMIT ?", (connection_id, state, start_from, *(purposes or ()), limit)).fetchall()]
    if not has_key:
        rows = [row for row in rows if not _needs_ai(db, row)]
    plan = _plan_files(db, rows, connection_id, limit)
    rows = [row for row in rows if row["id"] in plan]
    if has_key:
        read_ahead(db, rows, settings, {fid for ids, _more in plan.values() for fid in ids})
    for row in rows:
        message = dict(row, state=row["t_state"] or "new", attempts=row["t_attempts"] or 0,
                       email_done=row["t_email_done"] or 0, file_ids=set(plan[row["id"]][0]),
                       more_files=plan[row["id"]][1])
        try:
            with _source(odoo, message["id"]):
                run.message(message)
        except Exception as exc:
            _logger.exception("Email %s failed in connection %s", message["id"], connection_id)
            attempts = message["attempts"] + 1
            set_target(db, message["id"], connection_id, attempts=attempts, note=str(exc)[:2000],
                       state="failed" if attempts >= max_retries else message["state"])
        set_target(db, message["id"], connection_id, processed_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        store.refresh_messages(db, [message["id"]])
        db.commit()
    for job in all_jobs():  # a job's own work after the emails (Scraped Bills: waiting bills, uploaded invoices)
        if job.handler and not job.own_worker and (purposes is None or job.purpose in purposes) \
                and (has_key or not job.reading):
            after_pass = getattr(run.handler(job.purpose), "after_pass", None)
            if after_pass:
                after_pass(limit)


def _needs_ai(db, row):
    """Whether processing this email needs the AI: its job has AI read its files and it has some."""
    job = job_for(row["purpose"])
    if not (job and job.reading):
        return False
    return db.execute("SELECT 1 FROM file WHERE message_id = ? LIMIT 1", (row["id"],)).fetchone() is not None


def actionable_files(db, connection_id, message_ids):
    """{message id: [file rows still to do in this connection]}: new files first, then those waiting for something,
    each in the order they were saved."""
    found = {}
    if not message_ids:
        return found
    for file in db.execute(
            "SELECT f.*, COALESCE(t.state, 'new') AS target_state FROM file f "
            "LEFT JOIN file_target t ON t.file_id = f.id AND t.connection_id = ? "
            f"WHERE f.message_id IN ({', '.join('?' * len(message_ids))}) AND COALESCE(t.state, 'new') IN ('new', 'waiting') "
            "ORDER BY f.message_id, CASE COALESCE(t.state, 'new') WHEN 'new' THEN 0 ELSE 1 END, f.id",
            (connection_id, *message_ids)).fetchall():
        found.setdefault(file["message_id"], []).append(file)
    return found


def _plan_files(db, rows, connection_id, limit):
    """What this pass does: {email id: (ids of the files to take, whether more files are left for a later pass)}.

    The setting counts files (review F20, v003 R05): in each state's queue, new and waiting-for-review, the emails are
    taken in order and every file taken is one of ``limit``; an email whose files do not all fit is taken for the
    ones that do, stays pending, and carries on next pass from where it stopped. Nothing reads or submits more than
    ``limit`` files per queue, an email with hundreds of attachments included. An email without files costs nothing
    (its text is read) and is always taken."""
    files = actionable_files(db, connection_id, [row["id"] for row in rows])
    plan, left = {}, {}
    for row in rows:
        queue = row["t_state"] or "new"
        todo = files.get(row["id"], [])
        left.setdefault(queue, limit)
        if todo and left[queue] <= 0:
            continue  # the queue's files for this pass are used up: this email is next time's
        taken = todo[:left[queue]]
        left[queue] -= len(taken)
        plan[row["id"]] = ([f["id"] for f in taken], len(taken) < len(todo))
    return plan


def _purposes_clause(purposes):
    return f"mb.purpose IN ({', '.join('?' * len(purposes))})" if purposes else "1 = 1"


def read_ahead(db, rows, settings, only=None):
    """The AI reads this run's new freight and invoice files several at once (ai_parallel), before
    anything is written: a slow reading never holds the database, and the run waits for its slowest file
    instead of the sum of them. A file not read here (an error, no text) is read in its turn, as before."""
    parallel = int(settings.get("ai_parallel") or 1)
    readings = read_by_ai()
    ids = [row["id"] for row in rows if row["purpose"] in readings]
    if parallel < 2 or not ids:
        return
    purpose_of = {row["id"]: row["purpose"] for row in rows}
    version = {purpose: extract.reading_version(reading, settings) for purpose, reading in readings.items()}
    # Unread, or read by other instructions (another reading_version): read now.
    files = [f for f in db.execute(f"SELECT id, path, message_id, extracted, read_with, read_limits FROM file WHERE "
                                   f"message_id IN ({', '.join('?' * len(ids))})", ids).fetchall()
             if (only is None or f["id"] in only)
             and (f["extracted"] is None or f["read_with"] != version[purpose_of[f["message_id"]]]
                  or extract.limits_raised(f["read_limits"], settings))]

    def read(file):
        with open(file["path"], "rb") as handle:
            return extract.read_file(handle.read(), readings[purpose_of[file["message_id"]]], settings)

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
            db.execute("UPDATE file SET doc_class = ?, extracted = ?, read_with = ?, read_limits = ? WHERE id = ?",
                       (doc_class, json.dumps(values), version[purpose_of[file["message_id"]]],
                        extract.reading_limits(settings), file["id"]))
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


class _Run:
    def __init__(self, db, odoo, connection_id, settings):
        self.db = db
        self.odoo = odoo
        self.connection_id = connection_id
        self.settings = settings
        self.mappings = db.execute("SELECT * FROM vendor_mapping WHERE active = 1").fetchall()
        self.formats = db.execute(
            "SELECT * FROM subject_format WHERE active = 1 ORDER BY sequence, id").fetchall()
        self._handlers = {}

    def handler(self, purpose):
        """This pass's handler of a job (app/<job>/handler.py), made on first use; None for an unknown purpose."""
        if purpose not in self._handlers:
            job = job_for(purpose)
            self._handlers[purpose] = job.handler(self) if job and job.handler else None
        return self._handlers[purpose]

    def message(self, message):
        handler = self.handler(message["purpose"])
        body = message["body_text"]
        if body is None:
            body = newest_message_text(message["body_html"])
            self.db.execute("UPDATE message SET body_text = ? WHERE id = ?", (body, message["id"]))
        base = {"sender": message["sender"] or "", "subject": message["subject"] or "",
                "received": message["received"]}
        waiting = []
        files = actionable_files(self.db, self.connection_id, [message["id"]]).get(message["id"], [])
        allowed = message.get("file_ids")
        for file in files:
            if allowed is not None and file["id"] not in allowed:
                continue  # beyond this pass's file budget: it is done next pass
            if hasattr(handler, "take_file"):  # a job reading its own files (Customs: Tesseract, no AI)
                self.db.commit()  # never hold the database while a scanned PDF is read
                with _source(self.odoo, message["id"], file["id"]):
                    self._file(file, *handler.take_file(message, file))
                continue
            reading = job_for(message["purpose"]).reading
            doc_class, values = file["doc_class"], json.loads(file["extracted"] or "null")
            version = extract.reading_version(reading, self.settings)
            if file["read_with"] != version or extract.limits_raised(file["read_limits"], self.settings):
                values = None  # read by other instructions: read again
            if values is None:
                self.db.commit()  # never hold the database while the AI reads
                with open(file["path"], "rb") as handle:
                    doc_class, values = extract.read_file(handle.read(), reading, self.settings)
                if doc_class is None:
                    self._file(file, "skipped", "No readable text in this file.")
                    continue
                self.db.execute("UPDATE file SET doc_class = ?, extracted = ?, read_with = ?, read_limits = ? "
                                "WHERE id = ?",
                                (doc_class, json.dumps(values), version, extract.reading_limits(self.settings),
                                 file["id"]))
                self.db.commit()  # the AI is paid for: keep its reading whatever happens next
            doc = dict(base, doc_class=doc_class, values=values, source=file["filename"], file_id=file["id"])
            self.db.commit()  # never hold a write lock across the Odoo calls that follow
            with _source(self.odoo, message["id"], file["id"]):
                state, note, odoo_id = handler.take(message, doc)
            self._file(file, state, note, odoo_id)
            if state == "waiting":
                waiting.append(f"{file['filename']}: {note}")
        if message.get("more_files"):
            return  # files are left for a later pass: the email stays pending, with what was done kept per file
        take_email = getattr(handler, "take_email", None)  # a job reading the email itself (Freight Booking)
        if take_email and not message["email_done"]:
            self.db.commit()
            state, note = take_email(message, base, body) or ("done", None)
            if state == "waiting":
                waiting.append(f"Email body: {note}")
            if state == "done":
                set_target(self.db, message["id"], self.connection_id, email_done=1)
        set_target(self.db, message["id"], self.connection_id, state="review" if waiting else "done",
                   note="\n".join(waiting) or None)

    def _file(self, file, state, note, odoo_id=None):
        self.db.execute(
            "INSERT INTO file_target (file_id, connection_id, state, note, odoo_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (file_id, connection_id) DO UPDATE SET state = excluded.state, note = excluded.note, "
            "odoo_id = excluded.odoo_id", (file["id"], self.connection_id, state, note, odoo_id))
