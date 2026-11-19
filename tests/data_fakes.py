"""An Odoo with a freight booking model, for the import and export tests: it answers fields_get, search_read, read, search,
name_search, default_get, create and write as Odoo does, and remembers every write."""

import json


def _f(string, kind, readonly=False, **extra):
    return {"string": string, "type": kind, "readonly": readonly, "required": False, "store": True, **extra}


FIELDS = {
    "b2b.ocr.booking": {
        "id": _f("ID", "integer", True),
        "booking_number": _f("Booking Number", "char"),
        "state": _f("State", "selection", True, selection=[["incomplete", "Incomplete"], ["ready_for_po", "Ready for PO"]]),
        "forwarder_id": _f("Forwarder (Payable Vendor)", "many2one", relation="res.partner"),
        "factory_id": _f("Factory", "many2one", relation="res.partner"),
        "etd": _f("ETD", "date"), "vessel": _f("Vessel", "char"), "cost_per_container": _f("Cost Per Container", "monetary"),
        "no_additional_charges": _f("No Additional Charges", "boolean"),
        "freight_mode": _f("Freight Mode", "selection", selection=[["ocean", "Ocean"], ["inland", "Inland"]]),
        "missing_fields": _f("Missing Fields", "char", True),
        "company_id": {**_f("Company", "many2one", relation="res.company"), "required": True},
        "container_ids": _f("Containers", "one2many", relation="b2b.ocr.booking.container", relation_field="booking_id"),
        "message_ids": _f("Messages", "one2many", relation="mail.message", relation_field="res_id"),
        "message_follower_ids": _f("Followers", "one2many", relation="mail.followers", relation_field="res_id"),
    },
    "b2b.ocr.booking.container": {"id": _f("ID", "integer", True), "booking_id": _f("Booking", "many2one", relation="b2b.ocr.booking"),
                                  "container_no": _f("Container No", "char")},
    "b2b.ocr.scraped.bill": {"id": _f("ID", "integer", True), "name": _f("Bill", "char", True), "vendor_name": _f("Vendor", "char"),
                             "state": _f("State", "selection", True, selection=[["waiting", "Waiting"]])},
}


class DataOdoo:
    url = "http://odoo.test"
    name = "Staging (1115)"
    activity_db = object()  # the real client logs each write while this is set; an import turns it off

    def __init__(self):
        self.rows = {
            "b2b.ocr.booking": {
                41: {"id": 41, "booking_number": "BK123", "state": "incomplete", "forwarder_id": [5, "Quartz Logistics (NY) Inc."],
                     "factory_id": False, "etd": "2026-11-04", "vessel": "MSC ANNA", "cost_per_container": 4900.0,
                     "no_additional_charges": False, "freight_mode": "ocean", "missing_fields": "Deliver To", "company_id": [1, "WTD"]},
                42: {"id": 42, "booking_number": "BK200", "state": "ready_for_po", "forwarder_id": [6, "Apex Maritime Co.,Inc."],
                     "factory_id": [8, "General Rubber (Thailand) Co., LTD"], "etd": False, "vessel": False, "cost_per_container": 0.0,
                     "no_additional_charges": True, "freight_mode": False, "missing_fields": False, "company_id": [1, "WTD"]},
                43: {"id": 43, "booking_number": "BK300", "state": "incomplete", "forwarder_id": False, "factory_id": False, "etd": False,
                     "vessel": "CMA SHIP", "cost_per_container": 0.0, "no_additional_charges": False, "freight_mode": False,
                     "missing_fields": False, "company_id": [1, "WTD"]},
            },
            "b2b.ocr.booking.container": {1: {"id": 1, "booking_id": [41, "BK123"], "container_no": "MSCU1234566"},
                                          2: {"id": 2, "booking_id": [41, "BK123"], "container_no": "TGHU7654320"}},
            "b2b.ocr.scraped.bill": {7: {"id": 7, "name": "INV-1", "vendor_name": "TIMAC", "state": "waiting"}},
            "res.partner": {5: {"name": "Quartz Logistics (NY) Inc."}, 6: {"name": "Apex Maritime Co.,Inc."},
                            8: {"name": "General Rubber (Thailand) Co., LTD"}, 9: {"name": "Apex Shipping Ltd"},
                            10: {"name": "Apex Shipping Ltd"}},
        }
        self.writes = []
        self.refuse = {}  # {booking number: message}: a write of that booking fails, as Odoo's constraints can make it
        self.defaults = {"company_id": 1}  # what default_get gives a new record
        self._next = 100

    def _match(self, model, record, domain):
        for field, op, value in domain:
            have = record.get(field)
            have = have[0] if isinstance(have, list) else have
            if op == "in" and have not in value:
                return False
            if op == "=" and have != value:
                return False
        return True

    def _out(self, model, record, fields):
        out = {"id": record["id"]}
        for f in fields or []:
            out[f] = record.get(f, False)
        return out

    def call(self, model, method, *args, **kwargs):
        if method == "fields_get":
            return FIELDS[model]
        table = self.rows.get(model, {})
        if method in ("search_read", "read"):
            if method == "read":
                return [self._out(model, table[i], args[1]) for i in args[0] if i in table]
            records = [r for r in table.values() if self._match(model, r, args[0])]
            return [self._out(model, r, kwargs.get("fields")) for r in sorted(records, key=lambda r: r["id"])]
        if method == "search":
            return [r["id"] for r in table.values() if self._match(model, r, args[0])][:kwargs.get("limit") or 1000]
        if method == "name_search":
            text, operator = args[0], kwargs.get("operator", "ilike")
            hits = [[i, r["name"]] for i, r in table.items()
                    if (r["name"] == text if operator == "=" else text.lower() in r["name"].lower())]
            return hits[:kwargs.get("limit", 100)]
        if method == "default_get":
            return dict(self.defaults)
        if method == "create":
            values = dict(args[0])
            if values.get("booking_number") in self.refuse:
                raise RuntimeError(self.refuse[values["booking_number"]])
            self._next += 1
            record = {"id": self._next, "state": "incomplete", "company_id": [1, "WTD"], **{k: self._store(model, k, v) for k, v in values.items()}}
            table[self._next] = record
            self.writes.append(("create", model, values))
            return self._next
        if method == "write":
            for record_id in args[0]:
                record = table[record_id]
                if record.get("booking_number") in self.refuse:
                    raise RuntimeError(self.refuse[record["booking_number"]])
                record.update({k: self._store(model, k, v) for k, v in args[1].items()})
            self.writes.append(("write", model, list(args[0]), dict(args[1])))
            return True
        raise AssertionError(f"unexpected call {model}.{method}")

    def _store(self, model, field, value):
        info = FIELDS[model].get(field, {})
        if info.get("type") == "many2one":
            return [value, self.rows[info["relation"]].get(value, {}).get("name", "")]
        return value

    def search_read(self, model, domain, fields, **kwargs):  # as the real client's helper
        return self.call(model, "search_read", domain, fields=fields, **kwargs)


def patch_odoo(monkeypatch, odoo):
    monkeypatch.setattr("app.shared.odoo.client.Odoo.from_store", lambda db: odoo)
    monkeypatch.setattr("app.shared.odoo.client.Odoo.from_connection", lambda db, connection: odoo)
    return odoo


def seed_bookings(db, ids=(41, 42), purpose_box=None):
    """The service's record that it wrote these bookings (the list pages and the export read it)."""
    from fakes import mailbox
    box = purpose_box or mailbox(db)
    with db:
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key, subject, state) VALUES (1, ?, 'a', 'BKG BK123', 'done')", (box["id"],))
        for record_id in ids:
            db.execute("INSERT INTO odoo_change (message_id, action, model, record_id, values_json) VALUES (1, 'created', 'b2b.ocr.booking', ?, ?)",
                       (record_id, json.dumps({})))
