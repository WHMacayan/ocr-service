"""In-memory stand-ins for Microsoft Graph, and a mailbox in a temporary store."""

import base64

from app.core import store

PDF = b"%PDF-1.4 test document"


def message(n, received="2026-10-26T08:00:00Z", **extra):
    msg = {
        "id": f"graph-{n}",
        "internetMessageId": f"<msg-{n}@forwarder.com>",
        "subject": f"Booking {n}",
        "bodyPreview": "",
        "from": {"emailAddress": {"address": "ops@forwarder.com"}},
        "receivedDateTime": received,
        "toRecipients": [{"emailAddress": {"name": "WTD - PH", "address": "wtd@company.com"}}],
        "ccRecipients": [],
        "body": {"content": "hello"},
    }
    msg.update(extra)
    return msg


def attachment(att_id, name="bl.pdf", data=PDF, **extra):
    return dict({"id": att_id, "name": name, "size": len(data), "isInline": False,
                 "contentBytes": base64.b64encode(data).decode()}, **extra)


class FakeGraph:
    def __init__(self, messages=(), files=None, fail_attachments_for=()):
        self.messages = list(messages)
        self.files = files or {}
        self.fail_attachments_for = set(fail_attachments_for)
        self.downloads = 0

    def messages_page(self, url, params=None):
        return list(self.messages), None

    def attachments(self, message_id):
        if message_id in self.fail_attachments_for:
            raise RuntimeError("Graph down")
        return [{k: v for k, v in att.items() if k != "contentBytes"}
                for att in self.files.get(message_id, [])]

    def attachment(self, message_id, attachment_id):
        self.downloads += 1
        return next(a for a in self.files[message_id] if a["id"] == attachment_id)


def mailbox(db, **extra):
    """Insert a mailbox and return it as the worker sees it."""
    values = {
        "name": "Bookings", "company_id": 1, "purpose": "freight_booking",
        "tenant_id": "t", "client_id": "c", "batch_size": 100, "fetch_window_days": 14,
    }
    values.update(extra)
    with db:
        mailbox_id = db.execute(
            f"INSERT INTO mailbox ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
            tuple(values.values())).lastrowid
    store.update_mailbox(db, mailbox_id, {"client_secret": "s", "refresh_token": "r"})
    return store.mailbox(db, mailbox_id)


def connection(db, name="Production", **extra):
    """An Odoo connection that is on, made on first use; returns its id."""
    found = db.execute("SELECT id FROM odoo_connection WHERE name = ?", (name,)).fetchone()
    if found:
        return found["id"]
    values = {"name": name, "url": "http://odoo.test", "database": "1025", "login": "ocr", "api_key": "k",
              "is_production": 1, "active": 1}
    values.update(extra)
    return store.save_connection(db, None, values)
