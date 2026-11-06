"""Stand-ins shared by the tests: Microsoft Graph, Odoo, the AI's answer, and records in a
temporary store; signing in to the pages."""

import base64
import re

from app.core import store
from app.flows.freight import Booker

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


# ── Odoo ──

# One2many paths the domains below use: (model, field) -> (child model, inverse field).
O2M = {
    ("b2b.ocr.booking", "bl_ids"): ("b2b.ocr.booking.bl", "booking_id"),
    ("b2b.ocr.booking", "container_ids"): ("b2b.ocr.booking.container", "booking_id"),
}


M2O = {"partner_id", "company_id", "booking_id", "freight_currency_id", "forwarder_id", "factory_id",
       "freight_product_id", "mapping_freight_product_id", "purchase_order_id", "warehouse_id", "bill_id",
       "purchase_id", "commercial_partner_id", "currency_id", "move_id", "purchase_order_id", "order_id",
       "product_id", "picking_type_id"}


DEFAULTS = {
    "b2b.ocr.booking": {"freight_mode": "unknown"},
    "b2b.ocr.booking.container": {"state": "active"},
    "b2b.ocr.booking.conflict": {"state": "open"},
    "b2b.ocr.booking.container.change": {"state": "open"},
    "b2b.ocr.scraped.bill": {"state": "waiting"},
}


class FakeOdoo:
    """Just enough of the External API for the pipeline's calls."""

    def __init__(self):
        self.tables = {}
        self.posts = []
        self._next = 100

    def rows(self, model):
        return self.tables.setdefault(model, {})

    def add(self, model, **values):
        self._next += 1
        self.rows(model)[self._next] = dict(DEFAULTS.get(model, {}), **values)
        return self._next

    # domain evaluation ------------------------------------------------
    def _value(self, model, row_id, path):
        head, _, rest = path.partition(".")
        if rest:
            child, inverse = O2M[(model, head)]
            return [r.get(rest) for r in self.rows(child).values() if r.get(inverse) == row_id]
        if head == "id":
            return row_id
        return self.rows(model)[row_id].get(head, False)

    def _leaf(self, model, row_id, leaf):
        field, op, target = leaf
        value = self._value(model, row_id, field)
        values = value if isinstance(value, list) else [value]
        for v in values:
            if op in ("=", "child_of") and v == target:
                return True
            if op == "!=" and v != target:
                return True
            if op == "in" and v in target:
                return True
            if op == "ilike" and v and str(target).lower() in str(v).lower():
                return True
            if op == "=ilike" and v and (str(v).lower().endswith(target[1:].lower()) if target.startswith("%")
                                         else str(v).lower() == target.lower()):
                return True
        return False

    def _eval(self, model, row_id, domain):
        def term(items):
            item = items.pop(0)
            if item == "|":
                left, right = term(items), term(items)
                return left or right
            return self._leaf(model, row_id, item)
        items, result = list(domain), True
        while items:
            result = term(items) and result
        return result

    def _ids(self, model, domain):
        return [i for i in self.rows(model) if self._eval(model, i, domain)]

    def _out(self, model, row_id, fields):
        row = self.rows(model)[row_id]
        out = {"id": row_id}
        for f in fields:
            v = row.get(f, False)
            out[f] = [v, f"#{v}"] if f in M2O and v else v
        return out

    # External API -----------------------------------------------------
    def search_read(self, model, domain, fields, limit=None, context=None, order=None):
        return [self._out(model, i, fields) for i in self._ids(model, domain)][:limit]

    def create(self, model, values):
        values = dict(values)
        for key, value in list(values.items()):
            if isinstance(value, list) and value and isinstance(value[0], tuple):
                values[key] = value[0][2]  # (6, 0, ids)
        return self.add(model, **values)

    def write(self, model, ids, values):
        for i in ids:
            self.rows(model)[i].update(values)
        return True

    def call(self, model, method, *args, **kwargs):
        if method == "search":
            return self._ids(model, args[0])
        if method == "search_count":
            return len(self._ids(model, args[0]))
        if method == "read":
            return [self._out(model, i, args[1]) for i in args[0]]
        if method == "fields_get":
            names = {f for row in self.rows(model).values() for f in row} | {"master_bl_source", "vessel_source",
                                                                             "etd_source", "eta_source"}
            if model == "purchase.order":
                names |= {"master_bl", "house_bl", "container_number"}
            return {n: {"string": n.replace("_", " ").title()} for n in names}
        if method == "message_post":
            self.posts.append((model, args, kwargs))
            return True
        raise AssertionError(f"unexpected call {model}.{method}")


def mapping(**values):
    row = {"active": 1, "role": "forwarder", "sender_domain": None, "document_name": None, "partner_id": 7,
           "freight_mode": None, "freight_product_id": None, "cost_per_container": None}
    row.update(values)
    return row


def doc(doc_class, source="a.pdf", subject="", sender="ops@fwd.com", received="2026-10-01T08:00:00", **values):
    return {"doc_class": doc_class, "values": values, "source": source, "subject": subject, "sender": sender,
            "received": received, "file_id": None}


def make_booker(odoo, mappings=(), newest=True):
    odoo.add("res.company", name="Wholesale Tire Distributors", partner_id=1, currency_id=2)
    return Booker(odoo, 1, list(mappings), lambda _doc, _booking: newest)


# ── emails in the store, and what the AI reads in them ──

CONFIRMATION = {"doc_class": "booking_confirmation", "booking_number": "AB123456", "vessel": "EVER GIVEN",
                "master_bl": "HLCU123456789"}


def odoo():
    fake = FakeOdoo()
    fake.rows("res.company")[1] = {"name": "Wholesale Tire Distributors", "partner_id": 1, "currency_id": 2}
    fake.rows("res.currency")[2] = {"name": "USD", "rounding": 0.01}
    return fake


def email(db, tmp_path, purpose="freight_booking", subject="Booking AB123456", sender="ops@fwd.com",
          received="2026-10-01 08:00:00", state="new"):
    box = mailbox(db, purpose=purpose)
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4 test")
    with db:
        message_id = db.execute(
            "INSERT INTO message (mailbox_id, dedupe_key, subject, sender, received, body_html, state) "
            "VALUES (?, ?, ?, ?, ?, '<p>See attached.</p>', ?)",
            (box["id"], f"<{purpose}-{received}@x>", subject, sender, received, state)).lastrowid
        db.execute("INSERT INTO file (message_id, filename, sha256, path) VALUES (?, 'doc.pdf', ?, ?)",
                   (message_id, f"sha-{purpose}-{received}", str(path)))
        if state != "new":  # already processed once in the connection
            db.execute("INSERT INTO message_target (message_id, connection_id, state) VALUES (?, ?, ?)",
                       (message_id, connection(db), state))
    return message_id


def row(db, table, row_id):
    return db.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()


# ── the pages ──

def csrf(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


def signed_in(client):
    token = csrf(client, "/setup")
    client.post("/setup", data={"csrf": token, "password": "long-password", "confirm": "long-password"})
    return csrf(client, "/settings")  # signing in issues a new form token
