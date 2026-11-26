"""Reading one Outlook mailbox into the service's store.

Port of the OCR module's fetch and ingest mixins, same rules: the mail of the
fetch window is paged oldest first from where the last run stopped, mail
already stored is recognised by its dedupe key, and each new email is saved
with its files in one transaction - fully saved or not at all.
"""

import base64
import email
import email.policy
import hashlib
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from app.shared.core import store
from app.shared.jobs import job_for
from app.shared.mail.graph import GRAPH_BASE
from app.shared.odoo.client import ODOO_DATETIME, utc_now

_logger = logging.getLogger(__name__)

# Headers per Graph page: a request-size choice, not a business rule.
PAGE_SIZE = 100
# Page budget per day of window, under a hard ceiling against an endless nextLink.
PAGES_PER_WINDOW_DAY = 20
MAX_HEADER_PAGES = 400
MIN_WINDOW_DAYS = 1
DEFAULT_WINDOW_DAYS = 14
DEFAULT_BATCH_SIZE = 25

MESSAGE_FIELDS = ("id,subject,bodyPreview,from,receivedDateTime,hasAttachments,"
                  "conversationId,internetMessageId,body,toRecipients,ccRecipients")

# Kept file types: PDFs and images (invoices often arrive as photographs).
_PRESERVABLE_MAGIC = (b"%PDF-", b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a")


# ── message helpers ──────────────────────────────────────────────────

def sender_of(msg):
    return (msg.get("from", {}).get("emailAddress", {}).get("address") or "").strip().lower()


def domain_of(address):
    return (address or "").strip().lower().rpartition("@")[2]


def _unprefixed(purpose):
    job = job_for(purpose)
    return bool(job and job.unprefixed_keys)


def dedupe_key(msg, purpose):
    """Message-ID where present (survives folder moves), else the Graph id.
    A purpose prefixes its name, but for the first job's (its Job's unprefixed_keys)."""
    key = (msg.get("internetMessageId") or "").strip() or (msg.get("id") or "").strip()
    if not key or _unprefixed(purpose):
        return key
    return f"{purpose}:{key}"


def file_key(sha, purpose):
    """A file's content hash, prefixed like dedupe_key: each purpose imports a file once,
    so a PDF read for a booking is still read again for bill matching."""
    return sha if _unprefixed(purpose) else f"{purpose}:{sha}"


def delivery_key(base, mailbox):
    """The key a new email or file is stored under: ``base`` and the company of the mailbox that received it, always
    (``<base>#company<id>``). A company's delivery is its own row whichever other rows exist, so clearing or
    deleting another company's mailbox never changes it (review v003 R01). Rows saved before keep their plain
    ``base`` key; ``legacy_owned`` finds them by the company of the mailbox they came from."""
    return f"{base}#company{mailbox.get('company_id') or 0}"


def plain_key(key):
    return key.rsplit("#company", 1)[0]


def is_valid_email(msg):
    if not msg.get("id"):
        return False, "missing message id"
    if not sender_of(msg):
        return False, "missing sender address"
    return True, ""


def recipients(msg):
    return [
        ((r.get("emailAddress", {}).get("name") or "").strip().lower(),
         (r.get("emailAddress", {}).get("address") or "").strip().lower())
        for r in (msg.get("toRecipients") or []) + (msg.get("ccRecipients") or [])
    ]


def sent_to_group(mailbox, msg):
    wanted = (mailbox.get("group_address") or "").strip().lower()
    return not wanted or any(wanted in pair for pair in recipients(msg))


def group_miss_summary(messages):
    seen = Counter(
        f"{(r.get('emailAddress', {}).get('name') or '').strip()} "
        f"<{(r.get('emailAddress', {}).get('address') or '').strip()}>"
        for msg in messages
        for r in (msg.get("toRecipients") or []) + (msg.get("ccRecipients") or [])
    )
    return "; ".join(f"{who} x{count}" for who, count in seen.most_common(8))


def filter_reason(mailbox, msg):
    """Why this message is left out, or None to keep it."""
    valid, reason = is_valid_email(msg)
    if not valid:
        return f"invalid ({reason})"
    if not sent_to_group(mailbox, msg):
        return "not sent to the Distribution Group"
    sender_filter = (mailbox.get("sender_filter") or "").lower()
    if sender_filter and sender_of(msg) != sender_filter:
        return "sender filter"
    domains = {d.strip().lower() for d in (mailbox.get("sender_domains") or "").split(",") if d.strip()}
    if domains and domain_of(sender_of(msg)) not in domains:
        return "sender domain"
    keyword = (mailbox.get("subject_keyword") or "").lower()
    searchable = f"{msg.get('subject') or ''}\n{msg.get('bodyPreview') or ''}".lower()
    if keyword and keyword not in searchable:
        return "subject keyword"
    return None


def is_skipped_sender(mailbox, msg):
    """A sender domain listed in Skip Senders, for a job whose mailboxes skip senders (Freight Booking)."""
    job = job_for(mailbox["purpose"])
    if not (job and job.skips_senders):
        return False
    skipped = {d.strip().lower() for d in (mailbox.get("skip_sender_domains") or "").split(",")
               if d.strip()}
    return domain_of(sender_of(msg)) in skipped


def files_in_email(mime):
    """The attached files of a raw email (and of emails attached inside it), shaped like Graph file
    attachments so they are saved like any other."""
    parsed = email.message_from_bytes(mime, policy=email.policy.default)
    found = []
    for part in parsed.walk():
        name = part.get_filename()
        if not name or part.is_multipart():
            continue
        raw = part.get_payload(decode=True) or b""
        found.append({"name": name, "isInline": False, "contentBytes": base64.b64encode(raw).decode()})
    return found


def is_supported_file(raw):
    if not raw or len(raw) < 8:
        return False
    if raw.startswith(_PRESERVABLE_MAGIC):
        return True
    if raw[:4] in (b"II*\x00", b"MM\x00*"):  # TIFF
        return True
    return raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"


def parse_graph_datetime(value):
    """Graph ISO time as an Odoo UTC datetime string, or False when unusable."""
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        _logger.warning("Ignoring unparseable Graph timestamp %r", value)
        return False
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime(ODOO_DATETIME)


def effective_window_days(mailbox):
    days = mailbox.get("fetch_window_days") or DEFAULT_WINDOW_DAYS
    return DEFAULT_WINDOW_DAYS if days < MIN_WINDOW_DAYS else days


def window_start(mailbox, now):
    """Monday on or before fetch_window_days ago, 00:00 UTC."""
    start = now - timedelta(days=effective_window_days(mailbox))
    start -= timedelta(days=start.weekday())
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def window_since(mailbox, now):
    """Where a read starts: the mailbox's start date when it has one (however far back), else the
    look-back window; moved up to the resume point."""
    start = mailbox.get("start_from")
    since = datetime.strptime(start, ODOO_DATETIME) if start else window_start(mailbox, now)
    resume = mailbox.get("fetch_resume_from")
    if resume:
        since = max(since, datetime.strptime(resume, ODOO_DATETIME))
    return since


def window_days(mailbox, now):
    """The days a read covers, for its page limit: at least the look-back."""
    return max(effective_window_days(mailbox), (now - window_since(mailbox, now)).days)


def window_filter(mailbox, now):
    return f"receivedDateTime ge {window_since(mailbox, now).strftime('%Y-%m-%dT%H:%M:%SZ')}"


def messages_url(mailbox):
    folder = (mailbox.get("folder") or "").strip()
    if not folder:
        return f"{GRAPH_BASE}/me/messages"
    return f"{GRAPH_BASE}/me/mailFolders/{quote(folder, safe='')}/messages"


# ── one run over one mailbox ─────────────────────────────────────────

class AttachmentDownloadError(Exception):
    """An attachment could not be downloaded: the email is not saved and is read again next run."""


class MailboxSync:
    def __init__(self, db, graph, mailbox, now=None):
        self.db = db
        self.graph = graph
        self.mailbox = mailbox
        self.now = now or utc_now
        self.run_id = None
        self.started = time.monotonic()
        self.lines = []
        self.mailbox_values = {}
        self.earliest_failure = None  # received time of the earliest email this run could not save
        self._known_rows = {}         # delivery key -> (message id, mailbox id) of the emails stored already
        self._duplicate_files = []    # (file id, filename) of files of the email being saved that are stored already

    def _message_key(self, msg):
        return delivery_key(dedupe_key(msg, self.mailbox["purpose"]), self.mailbox)

    def _file_key(self, sha):
        return delivery_key(file_key(sha, self.mailbox["purpose"]), self.mailbox)

    # activity log, kept on the run row

    def log(self, text):
        seconds = int(time.monotonic() - self.started)
        self.lines.append(f"+{seconds // 60}:{seconds % 60:02d}  {text}")

    def flush(self, values=None):
        values = dict(values or {}, log="\n".join(self.lines))
        with self.db:
            self.db.execute(f"UPDATE run SET {', '.join(f'{key} = ?' for key in values)} WHERE id = ?",
                            (*values.values(), self.run_id))

    def _stamp(self):
        return self.now().strftime(ODOO_DATETIME)

    # run

    def run(self):
        """Read the mailbox and save what is new. Returns the run status."""
        mailbox = self.mailbox
        self._begin()
        try:
            messages, fetch_values, truncated = self.fetch_messages()
        except Exception as exc:
            _logger.exception("Mailbox %s: could not read the mailbox", mailbox["id"])
            self.log(f"STOPPED - could not read the mailbox: {exc}")
            self.flush({"status": "error", "finished_at": self._stamp()})
            store.update_mailbox(self.db, mailbox["id"], {"status": "error", "last_error": str(exc),
                                                          "last_sync": self._stamp()})
            store.log_activity(self.db, "processing", f"Could not read mailbox {mailbox['name']}", str(exc),
                               level="error", link=f"/mailboxes/{mailbox['id']}/runs")
            return "error"

        counters = Counter()
        if not messages and self._cancel_asked():
            return self._cancel(counters, fetch_values)
        for index, message in enumerate(messages, start=1):
            if self._cancel_asked():
                return self._cancel(counters, fetch_values, message)
            subject = (message.get("subject") or "(no subject)")[:60]
            self.log(f"Email {index}/{len(messages)}: '{subject}' from {sender_of(message) or '?'}")
            try:
                counters += self.ingest(message)
            except Exception as exc:
                _logger.exception("Mailbox %s: message %s failed", mailbox["id"], message.get("id"))
                counters["failed"] += 1
                self._record_failure(message, exc, fetch_values)
            self.flush()

        return self._finish(counters, fetch_values, truncated)

    def _cancel_asked(self):
        row = self.db.execute("SELECT cancel_requested FROM mailbox WHERE id = ?", (self.mailbox["id"],)).fetchone()
        return bool(row and row["cancel_requested"])

    def _cancel(self, counters, fetch_values, next_message=None):
        """Stop on the user's request. Emails already saved stay; the next read carries on from the first
        one not saved (``next_message``), or starts again when the stop came while listing the mailbox."""
        if next_message is not None:
            received = parse_graph_datetime(next_message.get("receivedDateTime"))
            # An earlier email that failed is still unsaved: never step past it.
            if self.earliest_failure and (not received or self.earliest_failure < received):
                received = self.earliest_failure
            if received:
                fetch_values["fetch_resume_from"] = received
        else:
            fetch_values = {}
            if self.earliest_failure:
                fetch_values["fetch_resume_from"] = self.earliest_failure
        failed = counters["failed"]
        self.log(f"Run cancelled. {counters['created_emails']} email(s) were saved before it stopped; "
                 "the next read carries on from there."
                 + (f" {failed} email(s) failed earlier in this run and are read again." if failed else ""))
        self.flush({"status": "partial", "finished_at": self._stamp(),
                    "attachments": counters["attachments"], "documents_created": counters["created"],
                    "duplicates": counters["duplicates"], "invalid": counters["invalid"],
                    "failed": counters["failed"]})
        store.update_mailbox(self.db, self.mailbox["id"], dict(
            fetch_values, status="partial", last_sync=self._stamp(), cancel_requested=0,
            last_error=self.mailbox_values.get("last_error")))
        store.log_activity(self.db, "processing", f"Read of mailbox {self.mailbox['name']} cancelled",
                           f"{counters['created_emails']} new email(s) saved before it stopped.",
                           level="warning", link=f"/mailboxes/{self.mailbox['id']}/runs")
        return "partial"

    def _begin(self):
        with self.db:
            for stale in self.db.execute("SELECT id, log FROM run WHERE mailbox_id = ? AND status = 'running'",
                                         (self.mailbox["id"],)).fetchall():
                note = "Stopped: the service ended and this run did not finish."
                self.db.execute("UPDATE run SET status = 'error', log = ? WHERE id = ?",
                                (f"{stale['log']}\n{note}" if stale["log"] else note, stale["id"]))
            self.run_id = self.db.execute("INSERT INTO run (mailbox_id) VALUES (?)",
                                          (self.mailbox["id"],)).lastrowid
        store.update_mailbox(self.db, self.mailbox["id"],
                             {"status": "running", "last_error": None, "sync_requested": 0,
                              "cancel_requested": 0})
        group = (self.mailbox.get("group_address") or "").strip()
        self.log(
            f"Run started: {self.mailbox['purpose']}; "
            + (f"emails sent to '{group}'" if group else "every email")
            + f"; {(self.mailbox.get('folder') or '').strip() or 'all folders'}; "
            + f"up to {self.mailbox.get('batch_size') or DEFAULT_BATCH_SIZE} new emails.")
        self.flush()

    def _known_keys(self, keys):
        """The delivery keys among ``keys`` already stored: under that key, or under the plain key of a row saved
        before keys carried the company, by a mailbox of this company. Notes where each was stored."""
        if not keys:
            return set()
        wanted = list(keys) + [plain_key(k) for k in keys]
        company = self.mailbox.get("company_id") or 0
        found = {}
        for row in self.db.execute(
                f"SELECT m.id, m.dedupe_key, m.mailbox_id, b.company_id FROM message m JOIN mailbox b ON b.id = m.mailbox_id "
                f"WHERE m.dedupe_key IN ({', '.join('?' * len(wanted))})", wanted):
            found[row["dedupe_key"]] = row
        known = set()
        self._known_rows = {}
        for key in keys:
            row = found.get(key) or (found.get(plain_key(key)) if (found.get(plain_key(key)) is not None and
                                     (found[plain_key(key)]["company_id"] or 0) == company) else None)
            if row is not None:
                known.add(key)
                self._known_rows[key] = (row["id"], row["mailbox_id"])
        return known

    def _note_sources(self, messages, keys):
        """An email this mailbox sees that another mailbox of the company stored: kept as one more place it came from."""
        with self.db:
            for message, key in zip(messages, keys):
                stored = self._known_rows.get(key)
                if stored and stored[1] != self.mailbox["id"]:
                    self.db.execute("INSERT OR IGNORE INTO message_source (message_id, mailbox_id, graph_message_id) "
                                    "VALUES (?, ?, ?)", (stored[0], self.mailbox["id"], message.get("id")))

    def _has_stored_mail(self):
        return self.db.execute("SELECT 1 FROM message WHERE mailbox_id = ? LIMIT 1",
                               (self.mailbox["id"],)).fetchone() is not None

    def fetch_messages(self):
        """Up to batch_size messages not stored yet.

        Returns (messages, mailbox values to write, truncated)."""
        mailbox = self.mailbox
        batch_size = mailbox.get("batch_size") or DEFAULT_BATCH_SIZE
        url = messages_url(mailbox)
        params = {
            "$select": MESSAGE_FIELDS,
            "$orderby": "receivedDateTime asc",
            "$filter": window_filter(mailbox, self.now()),
            "$top": PAGE_SIZE,
        }
        max_pages = min(MAX_HEADER_PAGES,
                        PAGES_PER_WINDOW_DAY * (window_days(mailbox, self.now()) + 6))
        collected, seen_keys, unstored = [], set(), []
        left_out = Counter()
        headers_read = already_stored = pages = 0
        last_seen = None
        truncated = False

        self.log(f"Reading the mailbox: {params['$filter']}")
        while pages < max_pages:
            if self._cancel_asked():
                return [], {}, False
            messages, next_link = self.graph.messages_page(url, params)
            pages += 1
            headers_read += len(messages)
            keys = [self._message_key(m) for m in messages]
            known = self._known_keys(keys)
            self._note_sources(messages, keys)

            for message, key in zip(messages, keys):
                last_seen = message.get("receivedDateTime") or last_seen
                if key in known or key in seen_keys:
                    already_stored += 1
                    continue
                unstored.append(message)
                reason = filter_reason(mailbox, message)
                if reason:
                    left_out[reason] += 1
                    continue
                seen_keys.add(key)
                collected.append(message)
                if len(collected) >= batch_size:
                    break

            self.log(f"Page {pages}: {len(messages)} emails, {already_stored} already stored "
                     f"so far, {len(collected)} new to process so far.")
            if len(collected) >= batch_size or not next_link:
                break
            url, params = next_link, None
        else:
            truncated = True

        values = {"group_miss_note": None}
        group = (mailbox.get("group_address") or "").strip()
        # A quiet run is normal once group mail has been stored: warn only when
        # the setting has never matched anything in this mailbox.
        if group and unstored and not collected and not self._has_stored_mail() \
                and not any(sent_to_group(mailbox, m) for m in unstored):
            values["group_miss_note"] = (f"No email was sent to '{group}'. Most common "
                                         f"recipients: {group_miss_summary(unstored)}")
        resume = parse_graph_datetime(last_seen)
        if resume:
            values["fetch_resume_from"] = resume

        for reason, count in left_out.most_common():
            self.log(f"Left out, {reason}: {count}")
        if values["group_miss_note"]:
            self.log(values["group_miss_note"])
        if truncated:
            self.log(f"Page limit reached ({pages} pages): the rest of the window is read next run.")
        self.log(f"Mailbox read: {headers_read} emails on {pages} page(s), {already_stored} "
                 f"already stored, {len(collected)} new email(s) to process.")
        self.flush({"emails_found": headers_read, "emails_skipped": already_stored,
                    "emails_matched": len(collected)})
        return collected, values, truncated

    def ingest(self, msg):
        """Save one email and its files in one transaction."""
        counters = Counter()
        skipped = []
        documents = []
        state = "new"

        if is_skipped_sender(self.mailbox, msg):
            state = "skipped"
            skipped.append("all attachments: sender domain is on Skip Senders")
            self.log("  Kept, not downloaded: the sender is on Skip Senders.")
        else:
            payloads = []
            metadata = self.graph.attachments(msg["id"])
            counters["attachments"] = len(metadata)
            self.log(f"  {len(metadata)} attachment(s).")
            for att in metadata:
                try:
                    if att.get("@odata.type", "").endswith("itemAttachment"):
                        # An email forwarded as an attachment: its invoice PDFs are inside it (72 lost before 17 Nov 2026).
                        inner = files_in_email(self.graph.attached_email(msg["id"], att["id"]))
                        self.log(f"  {att.get('name') or 'attached email'}: an attached email with {len(inner)} file(s).")
                        payloads.extend(inner)
                    else:
                        payloads.append(self.graph.attachment(msg["id"], att["id"]))
                except Exception as exc:
                    # A download that failed is not an unsupported file: saving the email without it would
                    # lose the file for good (the email is then known and never read again). Not saved,
                    # the whole email is read again next run (review F01).
                    self.log(f"  {att.get('name')}: download failed - {exc}")
                    raise AttachmentDownloadError(f"{att.get('name') or 'attachment'}: download failed - {exc}") from exc
            documents = self._save_files(payloads, skipped, counters)

        sender = sender_of(msg)
        with self.db:
            message_id = self.db.execute(
                "INSERT INTO message (mailbox_id, dedupe_key, graph_message_id, internet_message_id,"
                " conversation_id, subject, sender, sender_domain, received, body_html,"
                " skipped_attachments, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.mailbox["id"], self._message_key(msg), msg["id"],
                 msg.get("internetMessageId"), msg.get("conversationId"), msg.get("subject") or "",
                 sender, domain_of(sender), parse_graph_datetime(msg.get("receivedDateTime")) or None,
                 (msg.get("body") or {}).get("content") or "", "\n".join(skipped) or None, state),
            ).lastrowid
            self.db.executemany(
                "INSERT INTO file (message_id, filename, sha256, path) VALUES (?, ?, ?, ?)",
                [(message_id, name, sha, path) for name, sha, path in documents])
            # A file this company has already: its first email keeps it; this email is another place it came from.
            self.db.executemany("INSERT OR IGNORE INTO file_source (file_id, message_id, filename) VALUES (?, ?, ?)",
                                [(file_id, message_id, name) for file_id, name in self._duplicate_files])
        self._duplicate_files = []
        counters["created"] += len(documents)
        counters["created_emails"] += 1
        return counters

    def _save_files(self, payloads, skipped, counters):
        """Write the new files to the volume. Returns (filename, sha256, path) per file."""
        candidates = []
        for att in payloads:
            name = (att.get("name") or "").strip() or "attachment"
            reason = None
            raw = b""
            if att.get("isInline"):
                reason = "inline image (signature / logo)"
            elif not att.get("contentBytes"):
                reason = "no content returned by Graph"
            else:
                try:
                    raw = base64.b64decode(att["contentBytes"])
                except ValueError:
                    reason = "content could not be decoded"
                else:
                    if not is_supported_file(raw):
                        reason = "not a PDF or image this pipeline can preserve"
            if reason:
                skipped.append(f"{name}: {reason}")
                counters["invalid"] += 1
                self.log(f"  {name}: not saved - {reason}")
                continue
            candidates.append((name, raw, hashlib.sha256(raw).hexdigest()))

        keys = [self._file_key(sha) for _name, _raw, sha in candidates]
        stored = self._known_files(keys)
        self._duplicate_files = []

        documents = []
        for (name, raw, sha), key in zip(candidates, keys):
            if key in stored:
                counters["duplicates"] += 1
                skipped.append(f"{name}: already imported")
                self.log(f"  {name}: already imported, not read again.")
                if stored[key] is not None:
                    self._duplicate_files.append((stored[key], name))
                continue
            stored[key] = None  # saved just now, in this email
            path = store.files_dir(self.db) / sha[:2] / sha
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            documents.append((name, key, str(path)))
            self.log(f"  {name}: saved.")
        return documents

    def _known_files(self, keys):
        """{delivery key: id of the stored file} for the keys already stored, as for emails (plain key of a file
        saved before keys carried the company: when its email came by a mailbox of this company)."""
        if not keys:
            return {}
        wanted = list(keys) + [plain_key(k) for k in keys]
        company = self.mailbox.get("company_id") or 0
        found = {}
        for row in self.db.execute(
                "SELECT f.id, f.sha256, b.company_id FROM file f JOIN message m ON m.id = f.message_id "
                f"JOIN mailbox b ON b.id = m.mailbox_id WHERE f.sha256 IN ({', '.join('?' * len(wanted))})", wanted):
            found[row["sha256"]] = row
        known = {}
        for key in keys:
            row = found.get(key)
            if row is None:
                legacy = found.get(plain_key(key))
                row = legacy if legacy is not None and (legacy["company_id"] or 0) == company else None
            if row is not None:
                known[key] = row["id"]
        return known

    def _record_failure(self, message, error, fetch_values):
        """Step the resume point back so the next run reads this message again."""
        self.log(f"  ERROR: {error} - not saved, it will be retried next run.")
        self.mailbox_values["last_error"] = str(error)
        received = parse_graph_datetime(message.get("receivedDateTime"))
        if received and (not self.earliest_failure or received < self.earliest_failure):
            self.earliest_failure = received
        resume = fetch_values.get("fetch_resume_from")
        if received and (not resume or received < resume):
            fetch_values["fetch_resume_from"] = received

    def _finish(self, counters, fetch_values, truncated):
        if counters["failed"]:
            status = "error"
        elif truncated:
            status = "partial"
        else:
            status = "success"
        self.log(f"Run finished: {status}. {counters['created']} new document(s), "
                 f"{counters['duplicates']} already imported, {counters['invalid']} rejected, "
                 f"{counters['failed']} error(s).")
        self.flush({
            "status": status,
            "finished_at": self._stamp(),
            "attachments": counters["attachments"],
            "documents_created": counters["created"],
            "duplicates": counters["duplicates"],
            "invalid": counters["invalid"],
            "failed": counters["failed"],
        })
        store.update_mailbox(self.db, self.mailbox["id"], dict(
            fetch_values, **self.mailbox_values, status=status, last_sync=self._stamp()))
        emails = counters["created_emails"]
        if emails or status != "success":  # a quiet read that found nothing is not worth an entry
            store.log_activity(
                self.db, "processing",
                f"Read mailbox {self.mailbox['name']}: {emails} new email{'s' if emails != 1 else ''}, "
                f"{counters['created']} file{'s' if counters['created'] != 1 else ''} saved"
                + (f", {counters['failed']} failed" if counters["failed"] else ""),
                self.mailbox_values.get("last_error"),
                level={"error": "error", "partial": "warning"}.get(status, "info"),
                link=f"/mailboxes/{self.mailbox['id']}/runs")
        return status
