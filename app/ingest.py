"""Reading one Outlook mailbox into the service's store.

Port of the OCR module's fetch and ingest mixins, same rules: the mail of the
fetch window is paged oldest first from where the last run stopped, mail
already stored is recognised by its dedupe key, and each new email is saved
with its files in one transaction - fully saved or not at all.
"""

import base64
import hashlib
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import store
from .graph import GRAPH_BASE
from .odoo import ODOO_DATETIME, utc_now

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


def dedupe_key(msg, purpose):
    """Message-ID where present (survives folder moves), else the Graph id.
    Purposes other than Freight Booking prefix their name."""
    key = (msg.get("internetMessageId") or "").strip() or (msg.get("id") or "").strip()
    if not key or purpose == "freight_booking":
        return key
    return f"{purpose}:{key}"


def file_key(sha, purpose):
    """A file's content hash, prefixed like dedupe_key: each purpose imports a file once,
    so a PDF read for a booking is still read again for bill matching."""
    return sha if purpose == "freight_booking" else f"{purpose}:{sha}"


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
    """Freight Booking only: sender domain listed in Skip Senders."""
    if mailbox["purpose"] != "freight_booking":
        return False
    skipped = {d.strip().lower() for d in (mailbox.get("skip_sender_domains") or "").split(",")
               if d.strip()}
    return domain_of(sender_of(msg)) in skipped


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


def window_filter(mailbox, now):
    """The window start, moved up to the mailbox's start date and resume point."""
    since = window_start(mailbox, now)
    for later in (mailbox.get("start_from"), mailbox.get("fetch_resume_from")):
        if later:
            since = max(since, datetime.strptime(later, ODOO_DATETIME))
    return f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def messages_url(mailbox):
    folder = (mailbox.get("folder") or "").strip()
    if not folder:
        return f"{GRAPH_BASE}/me/messages"
    return f"{GRAPH_BASE}/me/mailFolders/{quote(folder, safe='')}/messages"


# ── one run over one mailbox ─────────────────────────────────────────

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
        for index, message in enumerate(messages, start=1):
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
                             {"status": "running", "last_error": None, "sync_requested": 0})
        group = (self.mailbox.get("group_address") or "").strip()
        self.log(
            f"Run started: {self.mailbox['purpose']}; "
            + (f"emails sent to '{group}'" if group else "every email")
            + f"; {(self.mailbox.get('folder') or '').strip() or 'all folders'}; "
            + f"up to {self.mailbox.get('batch_size') or DEFAULT_BATCH_SIZE} new emails.")
        self.flush()

    def _known_keys(self, keys):
        if not keys:
            return set()
        marks = ", ".join("?" * len(keys))
        return {row["dedupe_key"] for row in self.db.execute(
            f"SELECT dedupe_key FROM message WHERE dedupe_key IN ({marks})", keys)}

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
                        PAGES_PER_WINDOW_DAY * (effective_window_days(mailbox) + 6))
        collected, seen_keys, unstored = [], set(), []
        left_out = Counter()
        headers_read = already_stored = pages = 0
        last_seen = None
        truncated = False

        self.log(f"Reading the mailbox: {params['$filter']}")
        while pages < max_pages:
            messages, next_link = self.graph.messages_page(url, params)
            pages += 1
            headers_read += len(messages)
            keys = [dedupe_key(m, mailbox["purpose"]) for m in messages]
            known = self._known_keys(keys)

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
                    payloads.append(self.graph.attachment(msg["id"], att["id"]))
                except Exception as exc:
                    counters["invalid"] += 1
                    self.log(f"  {att.get('name')}: download failed - {exc}")
            documents = self._save_files(payloads, skipped, counters)

        sender = sender_of(msg)
        with self.db:
            message_id = self.db.execute(
                "INSERT INTO message (mailbox_id, dedupe_key, graph_message_id, internet_message_id,"
                " conversation_id, subject, sender, sender_domain, received, body_html,"
                " skipped_attachments, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.mailbox["id"], dedupe_key(msg, self.mailbox["purpose"]), msg["id"],
                 msg.get("internetMessageId"), msg.get("conversationId"), msg.get("subject") or "",
                 sender, domain_of(sender), parse_graph_datetime(msg.get("receivedDateTime")) or None,
                 (msg.get("body") or {}).get("content") or "", "\n".join(skipped) or None, state),
            ).lastrowid
            self.db.executemany(
                "INSERT INTO file (message_id, filename, sha256, path) VALUES (?, ?, ?, ?)",
                [(message_id, name, sha, path) for name, sha, path in documents])
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

        purpose = self.mailbox["purpose"]
        keys = [file_key(sha, purpose) for _name, _raw, sha in candidates]
        stored = {row["sha256"] for row in self.db.execute(
            f"SELECT sha256 FROM file WHERE sha256 IN ({', '.join('?' * len(keys))})", keys)
        } if keys else set()

        documents = []
        for (name, raw, sha), key in zip(candidates, keys):
            if key in stored:
                counters["duplicates"] += 1
                skipped.append(f"{name}: already imported")
                self.log(f"  {name}: already imported, not read again.")
                continue
            stored.add(key)
            path = store.files_dir(self.db) / sha[:2] / sha
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            documents.append((name, key, str(path)))
            self.log(f"  {name}: saved.")
        return documents

    def _record_failure(self, message, error, fetch_values):
        """Step the resume point back so the next run reads this message again."""
        self.log(f"  ERROR: {error} - not saved, it will be retried next run.")
        self.mailbox_values["last_error"] = str(error)
        received = parse_graph_datetime(message.get("receivedDateTime"))
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
