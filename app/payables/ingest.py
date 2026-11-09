"""Reading a Freight Payables mailbox into its own tables.

It extends the shared MailboxSync: the paging, resume point, run history and mailbox status
are inherited unchanged; only where the mail is stored and which files are kept differ.
The mail goes into payables_message / payables_file / payables_message_file, which the Odoo
pipeline never reads.

What the audit needs beyond the booking flow: Excel statements, zipped invoices, forwarded
emails, screenshots pasted into the email (signature logos under 20 KB are left out), the part
of each reply that is new, and every email a file arrived on, not only the first.
"""

import base64
import hashlib
from collections import Counter

from ..core import store
from ..mail.ingest import MailboxSync, dedupe_key, domain_of, parse_graph_datetime, sender_of
from .kinds import MAX_FILE_BYTES, MIN_INLINE_IMAGE_BYTES, file_kind

ITEM_ATTACHMENT = "#microsoft.graph.itemAttachment"


def is_skipped_sender(mailbox, msg):
    skipped = {d.strip().lower() for d in (mailbox.get("skip_sender_domains") or "").split(",") if d.strip()}
    return domain_of(sender_of(msg)) in skipped


class PayablesSync(MailboxSync):
    def _known_keys(self, keys):
        if not keys:
            return set()
        marks = ", ".join("?" * len(keys))
        return {row["dedupe_key"] for row in self.db.execute(
            f"SELECT dedupe_key FROM payables_message WHERE dedupe_key IN ({marks})", keys)}

    def _has_stored_mail(self):
        return self.db.execute("SELECT 1 FROM payables_message WHERE mailbox_id = ? LIMIT 1",
                               (self.mailbox["id"],)).fetchone() is not None

    def ingest(self, msg):
        """Save one email, its kept files and their links in one transaction."""
        counters = Counter()
        skipped, files = [], []
        state = "new"
        if is_skipped_sender(self.mailbox, msg):
            state = "skipped"
            skipped.append("all attachments: sender domain is on Skip Senders")
            self.log("  Kept, not downloaded: the sender is on Skip Senders.")
        else:
            metadata = self.graph.all_attachments(msg["id"])
            counters["attachments"] = len(metadata)
            self.log(f"  {len(metadata)} attachment(s), inline pictures included.")
            for att in metadata:
                try:
                    kept = self._download(msg["id"], att, skipped)
                except Exception as exc:
                    counters["invalid"] += 1
                    skipped.append(f"{att.get('name')}: download failed - {exc}")
                    self.log(f"  {att.get('name')}: download failed - {exc}")
                    continue
                if kept:
                    files.append(kept)
                else:
                    counters["invalid"] += 1
        unique_body = self.graph.unique_body(msg["id"]) if state == "new" else None

        sender = sender_of(msg)
        with self.db:
            message_id = self.db.execute(
                "INSERT INTO payables_message (mailbox_id, dedupe_key, graph_message_id, internet_message_id,"
                " conversation_id, subject, sender, sender_domain, received, body_html, unique_body_html,"
                " skipped_attachments, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.mailbox["id"], dedupe_key(msg, self.mailbox["purpose"]), msg["id"],
                 msg.get("internetMessageId"), msg.get("conversationId"), msg.get("subject") or "",
                 sender, domain_of(sender), parse_graph_datetime(msg.get("receivedDateTime")) or None,
                 (msg.get("body") or {}).get("content") or "", unique_body, "\n".join(skipped) or None, state),
            ).lastrowid
            for name, raw, kind, is_inline in files:
                file_id, created = self._store_file(raw, kind)
                counters["created" if created else "duplicates"] += 1
                self.db.execute("INSERT OR IGNORE INTO payables_message_file (message_id, file_id, filename, is_inline)"
                                " VALUES (?, ?, ?, ?)", (message_id, file_id, name, int(is_inline)))
        counters["created_emails"] += 1
        return counters

    def _download(self, message_id, att, skipped):
        """(name, bytes, kind, is_inline) for a file Freight Payables keeps, else None with the reason noted."""
        name = (att.get("name") or "").strip() or "attachment"
        if att.get("@odata.type") == ITEM_ATTACHMENT:
            raw = self.graph.attachment_bytes(message_id, att["id"])
            name = name if name.lower().endswith(".eml") else f"{name}.eml"
        else:
            content = self.graph.attachment(message_id, att["id"]).get("contentBytes")
            raw = base64.b64decode(content) if content else b""
        reason = None
        kind = file_kind(raw, name) if raw else None
        if not raw:
            reason = "no content returned by Graph"
        elif len(raw) > MAX_FILE_BYTES:
            reason = f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"
        elif att.get("isInline") and len(raw) < MIN_INLINE_IMAGE_BYTES:
            reason = "inline image under 20 KB (signature / logo)"
        elif kind is None:
            reason = "not a file type Freight Payables keeps"
        if reason:
            skipped.append(f"{name}: {reason}")
            self.log(f"  {name}: not saved - {reason}")
            return None
        self.log(f"  {name}: kept ({kind}).")
        return name, raw, kind, bool(att.get("isInline"))

    def _store_file(self, raw, kind):
        """The payables_file id for these bytes, writing the file once. Returns (id, created)."""
        sha = hashlib.sha256(raw).hexdigest()
        row = self.db.execute("SELECT id FROM payables_file WHERE sha256 = ?", (sha,)).fetchone()
        if row:
            return row["id"], False
        path = store.files_dir(self.db) / "payables" / sha[:2] / sha
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        file_id = self.db.execute("INSERT INTO payables_file (sha256, path, kind, size) VALUES (?, ?, ?, ?)",
                                  (sha, str(path), kind, len(raw))).lastrowid
        return file_id, True
