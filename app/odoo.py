"""Odoo 18 External API (XML-RPC). The only way the service reaches Odoo."""

import json
import xmlrpc.client
from contextlib import contextmanager
from datetime import datetime, timezone

from . import store

TIMEOUT_SECONDS = 120
# Odoo stores datetimes as naive UTC in this format.
ODOO_DATETIME = "%Y-%m-%d %H:%M:%S"


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _TimeoutTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = TIMEOUT_SECONDS
        return connection


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = TIMEOUT_SECONDS
        return connection


def _proxy(url):
    transport = _TimeoutSafeTransport() if url.startswith("https") else _TimeoutTransport()
    return xmlrpc.client.ServerProxy(url, transport=transport, allow_none=True)


WRITE_METHODS = {"create": "Created", "write": "Updated", "unlink": "Deleted"}
MODEL_NAMES = {
    "b2b.ocr.booking": "booking", "b2b.ocr.booking.container": "booking container",
    "b2b.ocr.booking.invoice": "booking invoice", "b2b.ocr.booking.bl": "booking B/L",
    "b2b.ocr.booking.conflict": "booking conflict", "b2b.ocr.booking.container.change": "container change",
    "b2b.ocr.scraped.bill": "scraped bill", "b2b.ocr.scraped.bill.line": "scraped bill line",
}
# Fields that name the record in the activity log, first found wins.
NAME_FIELDS = ("booking_number", "external_key", "invoice_number", "container_no", "container_number", "bl_number", "name")


def _short(value):
    text = repr(value) if not isinstance(value, str) else value
    return text if len(text) <= 120 else text[:117] + "..."


class Odoo:
    """activity_db: when set, every create, write and unlink is recorded in the activity log,
    and, inside working_on(), against the email (and file) being processed."""

    def __init__(self, url, db, user, api_key, activity_db=None, connection_id=None, name=None):
        self.url = url
        self.db = db
        self.api_key = api_key
        self.activity_db = activity_db
        self.connection_id = connection_id  # the odoo_connection its writes are recorded against
        self.name = name
        self.source = None  # (message id, file id) the writes are for
        self.uid = _proxy(f"{url}/xmlrpc/2/common").authenticate(db, user, api_key, {})
        if not self.uid:
            raise RuntimeError(f"Odoo refused the login '{user}' on database {db}. Use the user's login "
                               "(Settings > Users), not the display name, and an API key made by that user.")
        self._object = _proxy(f"{url}/xmlrpc/2/object")

    @classmethod
    def from_connection(cls, db, connection):
        """Log in with one connection from the Settings page (a row of store.connections)."""
        if not store.is_complete(connection):
            raise RuntimeError(f"The Odoo connection {connection['name']} is not filled in on the Settings page.")
        return cls(connection["url"].strip().rstrip("/"), connection["database"].strip(), connection["login"].strip(),
                   connection["api_key"].strip(), activity_db=db, connection_id=connection["id"],
                   name=connection["name"])

    @classmethod
    def from_store(cls, db):
        """Log in with the connection the pages read Odoo through (store.reference_connection)."""
        connection = store.reference_connection(db)
        if not connection:
            raise RuntimeError("No Odoo connection is switched on. Turn one on in Settings.")
        return cls.from_connection(db, connection)

    def call(self, model, method, *args, **kwargs):
        result = self._object.execute_kw(self.db, self.uid, self.api_key, model, method,
                                         list(args), kwargs)
        if self.activity_db is not None and method in WRITE_METHODS:
            self._log_write(model, method, args, result)
        return result

    @contextmanager
    def working_on(self, message_id, file_id=None):
        """Writes inside this block are recorded against that email and file."""
        previous, self.source = self.source, (message_id, file_id)
        try:
            yield
        finally:
            self.source = previous

    def _label(self, model, ids, values):
        """The record's name: from the values written, else read back from Odoo."""
        label = next((str(values[f]) for f in NAME_FIELDS if values.get(f)), "")
        if label or len(ids) != 1:
            return label
        try:
            [row] = self._object.execute_kw(self.db, self.uid, self.api_key, model, "read",
                                            [ids, ["display_name"]], {})
            return row["display_name"] or ""
        except Exception:
            return ""

    def _log_write(self, model, method, args, result):
        if method == "create":
            ids, values = (result if isinstance(result, list) else [result]), args[0] if args else {}
        else:
            ids, values = list(args[0]) if args else [], (args[1] if method == "write" and len(args) > 1 else {})
        if isinstance(values, list):  # create of several records
            values = values[0] if values else {}
        label = self._label(model, ids, values) if method != "unlink" else ""
        name = MODEL_NAMES.get(model, model)
        prefix = f"[{self.name}] " if getattr(self, "name", None) else ""
        summary = f"{prefix}{WRITE_METHODS[method]} {name} {', '.join(f'#{i}' for i in ids)}" + (f" ({label})" if label else "")
        detail = "\n".join(f"{field}: {_short(value)}" for field, value in values.items()) if values else None
        link = f"{self.url}/web#model={model}&id={ids[0]}&view_type=form" if len(ids) == 1 and method != "unlink" else None
        store.log_activity(self.activity_db, "odoo", summary, detail, actor="worker", link=link)
        if self.source:
            # No commit here: it lands with the pipeline's own commit for this email.
            self.activity_db.executemany(
                "INSERT INTO odoo_change (message_id, file_id, connection_id, action, model, record_id, label, detail,"
                " link, values_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(*self.source, getattr(self, "connection_id", None), WRITE_METHODS[method].lower(), model, record_id,
                  label, detail,
                  f"{self.url}/web#model={model}&id={record_id}&view_type=form" if method != "unlink" else None,
                  json.dumps(values, default=str) if values else None)
                 for record_id in ids])

    def search_read(self, model, domain, fields, **kwargs):
        return self.call(model, "search_read", domain, fields=fields, **kwargs)

    def create(self, model, values):
        return self.call(model, "create", values)

    def write(self, model, ids, values):
        return self.call(model, "write", ids, values)
