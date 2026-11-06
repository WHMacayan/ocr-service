"""Freight Booking: one document consolidated into its b2b.ocr.booking in Odoo.

Port of b2b_freight_ocr's booking rules. A value fills an empty field; a
different value is kept as an open Conflict for a person; a field a person
confirmed is never touched. Purchase history is written as suggestions only:
linking or drafting the freight PO is a button in Odoo.
"""

import json
import re

from ..documents.text import find_labelled_booking_number, normalize_identifier, parse_date

BOOKING = "b2b.ocr.booking"
CONTAINER = "b2b.ocr.booking.container"
INVOICE = "b2b.ocr.booking.invoice"
BL = "b2b.ocr.booking.bl"
CONFLICT = "b2b.ocr.booking.conflict"
CHANGE = "b2b.ocr.booking.container.change"

_SCHEDULE = ("booking_confirmation", "mbl_hbl", "shipping_notice")
# Which document types state which values. Every type prints other parties'
# details, so a value is taken only from a type whose job is to state it.
TRUSTED = {
    "schedule": _SCHEDULE,
    "email_schedule": ("email_message",),
    "bl": _SCHEDULE + ("freight_invoice", "email_message"),
    "forwarder": _SCHEDULE + ("freight_invoice",),
    "factory": ("commercial_invoice", "packing_list", "mbl_hbl"),
    "containers": _SCHEDULE + ("packing_list", "freight_invoice", "email_message"),
    # A packing list prints the order number where an invoice number would be: never an invoice.
    "invoices": ("commercial_invoice", "email_message"),
    "amounts": ("freight_invoice",),
}
# A newer booking confirmation replaces these: carriers reissue them as the schedule moves.
UPDATED_BY_CONFIRMATION = ("vessel", "voyage", "etd", "eta")
SCHEDULE_FIELDS = ("vessel", "voyage", "port_of_loading", "port_of_discharge", "etd", "eta")
_READ = (
    "booking_number", "forwarder_id", "forwarder_name", "factory_id", "factory_name", "master_bl",
    "master_bl_confirmed", "master_bl_assumed", "house_bl", "house_bl_confirmed", "house_bl_assumed",
    *SCHEDULE_FIELDS, "freight_currency_id", "freight_cost", "additional_charges", "freight_mode",
    "freight_product_id", "mapping_freight_product_id", "purchase_order_id", "warehouse_id",
)


def m2o_id(value):
    """The id of a many2one as XML-RPC returns it ([id, name] or False)."""
    return value[0] if value else False


def resolve_mapping(mappings, role, sender_domain="", name=""):
    """The vendor mapping row for a sender domain, else for a printed name."""
    rows = [m for m in mappings if m["active"] and m["role"] == role]
    domain = (sender_domain or "").strip().lower()
    if domain:
        for row in rows:
            if (row["sender_domain"] or "").strip().lower() == domain:
                return row
    printed = " ".join((name or "").upper().split())
    if printed:
        # The longest fragment wins: "SENTURY TIRE MOROCCO" over "SENTURY TIRE".
        for row in sorted(rows, key=lambda r: -len((r["document_name"] or "").strip())):
            fragment = " ".join((row["document_name"] or "").upper().split())
            # Lookarounds, not \b: a name may start or end with punctuation, e.g. "SENTURY TIRE (THAILAND)".
            if fragment and re.search(rf"(?<!\w){re.escape(fragment)}(?!\w)", printed):
                return row
    return None


class Booker:
    """Consolidates documents for one company. ``newest_confirmation(doc,
    booking_id)`` tells whether ``doc`` is the latest booking confirmation the
    booking has received; the pipeline answers it from its own records."""

    def __init__(self, odoo, company_id, mappings, newest_confirmation):
        self.odoo = odoo
        self.company_id = company_id
        self.mappings = mappings
        self.newest_confirmation = newest_confirmation
        self._labels = None
        self._own_names = None

    def labels(self):
        if self._labels is None:
            fields = self.odoo.call(BOOKING, "fields_get", attributes=["string"])
            self._labels = {name: info["string"] for name, info in fields.items()}
        return self._labels

    def own_names(self):
        if self._own_names is None:
            self._own_names = [normalize_identifier(c["name"])
                               for c in self.odoo.search_read("res.company", [], ["name"])]
        return self._own_names

    def _search(self, domain):
        return self.odoo.call(BOOKING, "search", [("company_id", "=", self.company_id)] + domain)

    # ------------------------------------------------------------------
    # Finding the booking of a document
    # ------------------------------------------------------------------

    def take(self, doc):
        """``(state, note, booking_id)`` for ``doc``: ``done`` when it was
        consolidated, ``waiting`` when no single booking fits yet."""
        values = doc["values"]
        keys = {name: normalize_identifier(values.get(name)) for name in ("booking_number", "master_bl", "house_bl")}
        ids = set()
        if keys["booking_number"]:
            ids |= set(self._search([("booking_number", "=", keys["booking_number"])]))
        for name in ("master_bl", "house_bl"):
            if keys[name]:
                ids |= set(self._search(["|", (name, "=", keys[name]), ("bl_ids.bl_number", "=", keys[name])]))
        if not ids and values.get("containers"):
            # A container is reused shipment after shipment: only open bookings count.
            ids |= set(self._search([("container_ids.container_no", "in", values["containers"]),
                                     ("purchase_order_id", "=", False)]))
        if len(ids) > 1:
            names = [b["booking_number"] for b in self.odoo.call(BOOKING, "read", sorted(ids), ["booking_number"])]
            return "waiting", f"Its identifiers point to bookings {', '.join(names)}.", None
        if not ids and self._may_create(doc, keys["booking_number"]):
            booking_id = self.odoo.create(BOOKING, {"booking_number": keys["booking_number"],
                                                    "company_id": self.company_id})
            if doc["doc_class"] == "email_message":
                self.odoo.write(BOOKING, [booking_id], {"has_uncertain_match": True})
            ids = {booking_id}
        if not ids:
            if any(keys.values()) or values.get("containers"):
                return "waiting", "No booking has its booking number, B/L or containers yet.", None
            return "waiting", "It names no booking number, B/L or container.", None
        booking_id = ids.pop()
        self.consolidate(booking_id, doc)
        return "done", None, booking_id

    def _may_create(self, doc, number):
        """A booking confirmation creates its booking; another document only
        from a booking number its subject labels; an email only in a Subject
        Format that confirms bookings, from a mapped forwarder."""
        if not number:
            return False
        if doc["doc_class"] == "booking_confirmation":
            return True
        if doc["doc_class"] == "email_message":
            domain = (doc["sender"] or "").rpartition("@")[2]
            return bool(doc["values"].get("confirms_booking")
                        and resolve_mapping(self.mappings, "forwarder", sender_domain=domain))
        return find_labelled_booking_number(doc["subject"]) == number

    # ------------------------------------------------------------------
    # Consolidation
    # ------------------------------------------------------------------

    def consolidate(self, booking_id, doc):
        booking = self.odoo.call(BOOKING, "read", [booking_id], list(_READ))[0]
        run = _Consolidation(self, booking, doc)
        run.apply()


class _Consolidation:
    def __init__(self, booker, booking, doc):
        self.booker = booker
        self.odoo = booker.odoo
        self.booking = booking
        self.id = booking["id"]
        self.doc = doc
        self.values = doc["values"]
        self.source = doc["source"]
        self.updates = {}
        self.notes = []

    def current(self, name):
        return self.updates[name] if name in self.updates else self.booking[name]

    def apply(self):
        kind = self.doc["doc_class"]
        trusted = {group for group, kinds in TRUSTED.items() if kind in kinds}
        if "schedule" in trusted:
            for name in SCHEDULE_FIELDS:
                self.set(name, self.values.get(name))
        if "email_schedule" in trusted:
            for name in ("etd", "eta"):
                self.set(name, self.values.get(name))
        if "bl" in trusted:
            for name, bl_type in (("master_bl", "master"), ("house_bl", "house")):
                number = normalize_identifier(self.values.get(name))
                self.set(name, number)
                self.add_bl(bl_type, number)
        for role in ("forwarder", "factory"):
            if role in trusted:
                self.set_party(role, self.values.get(f"{role}_name"))
        if "containers" in trusted and self.values.get("containers"):
            self.add_containers(self.values["containers"])
        if "invoices" in trusted and self.values.get("invoices"):
            self.add_invoices(self.values["invoices"])
        if "amounts" in trusted:
            self.set_amounts()
        self.mirror_bls()
        if self.current("freight_mode") == "unknown" and (
                self.current("master_bl") or self.current("house_bl") or self.values.get("containers")):
            self.updates["freight_mode"] = "ocean"
        if not self.booking["purchase_order_id"] and (self.current("master_bl") or self.current("house_bl")):
            self.updates.update(_PurchaseHistory(self).suggestions())
        if self.updates:
            self.odoo.write(BOOKING, [self.id], self.updates)
        for note in self.notes:
            self.odoo.call(BOOKING, "message_post", [self.id], body=note)

    def take_value(self, name, value):
        self.updates[name] = value
        if f"{name}_source" in self.booker.labels():  # only some fields name their document
            self.updates[f"{name}_source"] = self.source

    def set(self, name, value):
        """Fill an empty field; keep the first value and open a conflict for a different one."""
        if self.booking.get(f"{name}_confirmed"):
            return
        value = parse_date(value) if name in ("etd", "eta") else str(value or "").strip()
        if not value:
            return
        old = self.current(name) or ""
        if not old or self.booking.get(f"{name}_assumed"):
            self.take_value(name, value)
            if f"{name}_assumed" in self.booking:
                self.updates[f"{name}_assumed"] = False
                self.booking[f"{name}_assumed"] = False
            return
        a, b = normalize_identifier(old), normalize_identifier(value)
        email = self.doc["doc_class"] == "email_message"
        if a == b or (b and b in a):
            return
        if a and a in b and not email:
            self.take_value(name, value)  # the same value, stated more fully
            return
        if name in UPDATED_BY_CONFIRMATION and self.doc["doc_class"] == "booking_confirmation" \
                and self.booker.newest_confirmation(self.doc, self.id):
            self.take_value(name, value)
            self.notes.append(f"{self.booker.labels()[name]} updated from {old} to {value} by {self.source}.")
            return
        if not email:  # an email never disputes a document
            self.conflict(name, old, value)

    def conflict(self, name, old, new):
        """One conflict per field and value, open or settled: reading the same
        file again, or a value a person already decided, adds nothing."""
        if self.odoo.call(CONFLICT, "search_count", [("booking_id", "=", self.id), ("field_name", "=", name),
                                                     ("new_value", "=", str(new))]):
            return
        self.odoo.create(CONFLICT, {"booking_id": self.id, "field_name": name,
                                    "field_label": self.booker.labels().get(name, name),
                                    "existing_value": str(old), "new_value": str(new), "source": self.source})

    def set_party(self, role, name):
        if not name:
            return
        if any(own and own in normalize_identifier(name) for own in self.booker.own_names()):
            return  # the importer printed as consignee or booking party
        self.set(f"{role}_name", name)
        if self.current(f"{role}_id"):
            return
        domain = (self.doc["sender"] or "").rpartition("@")[2]
        by_domain = resolve_mapping(self.booker.mappings, role, sender_domain=domain)
        by_printed = resolve_mapping(self.booker.mappings, role, name=name)
        # One sender domain can serve several factories (Sentury Thailand and Morocco, Jinfei and
        # Vossen): when the printed name names another mapped vendor, the document wins.
        mapping = by_printed if by_printed and by_domain and by_printed is not by_domain else (by_domain or by_printed)
        by_name = mapping is not by_domain
        if not mapping:
            return
        self.updates[f"{role}_id"] = mapping["partner_id"]
        if role == "forwarder":
            if by_name:
                self.updates["has_uncertain_match"] = True
            if mapping["freight_product_id"]:
                self.updates["mapping_freight_product_id"] = mapping["freight_product_id"]
            if mapping["cost_per_container"]:
                self.updates["mapping_cost_per_container"] = mapping["cost_per_container"]
            if mapping["freight_mode"]:
                self.updates["freight_mode"] = mapping["freight_mode"]

    def add_bl(self, bl_type, number):
        if number and not self.odoo.call(BL, "search_count", [("booking_id", "=", self.id), ("bl_type", "=", bl_type),
                                                              ("bl_number", "=", number)]):
            self.odoo.create(BL, {"booking_id": self.id, "bl_type": bl_type, "bl_number": number,
                                  "source": self.source})

    def mirror_bls(self):
        """A shipment with a single bill: the other B/L is assumed equal until a
        document states it or a person confirms it."""
        for name, other in (("master_bl", "house_bl"), ("house_bl", "master_bl")):
            if self.current(other) and not self.current(name):
                self.updates.update({name: self.current(other), f"{name}_assumed": True,
                                     f"{name}_source": self.updates.get(f"{other}_source") or self.source})

    def add_containers(self, containers):
        lines = self.odoo.search_read(CONTAINER, [("booking_id", "=", self.id)], ["container_no", "state"])
        known = {line["container_no"] for line in lines}
        new = [c for c in containers if c not in known]
        if not new:
            return
        active = [line["id"] for line in lines if line["state"] == "active"]
        if not active:
            for number in new:
                self.odoo.create(CONTAINER, {"booking_id": self.id, "container_no": number, "source": self.source})
            return
        # A replacement or an addition: a person decides in Odoo.
        if self.odoo.call(CHANGE, "search_count", [("booking_id", "=", self.id), ("source", "=", self.source),
                                                   ("state", "=", "open")]):
            return
        self.odoo.create(CHANGE, {
            "booking_id": self.id, "source": self.source, "existing_container_ids": [(6, 0, active)],
            "new_container_lines": json.dumps([{"container_no": number} for number in new])})

    def add_invoices(self, numbers):
        known = {line["invoice_number"] for line in self.odoo.search_read(
            INVOICE, [("booking_id", "=", self.id)], ["invoice_number"])}
        for number in numbers:
            if number not in known:
                known.add(number)
                self.odoo.create(INVOICE, {"booking_id": self.id, "invoice_number": number, "source": self.source})

    def set_amounts(self):
        amounts = {name: self.values[name] for name in ("freight_cost", "additional_charges") if self.values.get(name)}
        if not amounts:
            return
        currency = self.odoo.search_read("res.currency", [("name", "=", (self.values.get("currency") or "").upper())],
                                         ["name", "rounding"], context={"active_test": False}, limit=1)
        if not currency:
            return  # an amount in an unknown currency is not an amount
        currency = currency[0]
        held = self.booking["freight_currency_id"]
        if held and held[0] != currency["id"]:
            self.notes.append(f"{self.source} states freight in {currency['name']}, the booking holds {held[1]}: "
                              f"its amounts were not taken.")
            return
        self.updates["freight_currency_id"] = currency["id"]
        for name, amount in amounts.items():
            old = self.booking[name]
            if not old:
                self.updates[name] = amount
            elif abs(old - amount) > currency["rounding"]:
                self.conflict(name, old, amount)


class _PurchaseHistory:
    """What Odoo's purchases already say about this shipment, as suggestions."""

    def __init__(self, run):
        self.run = run
        self.odoo = run.odoo

    def suggestions(self):
        keys = {k for k in (self.run.current("master_bl"), self.run.current("house_bl")) if k}
        po_fields = self.odoo.call("purchase.order", "fields_get", attributes=["type"])
        if not keys or "master_bl" not in po_fields:
            return {}
        clauses = [c for k in keys for c in (("master_bl", "ilike", k[-6:]), ("house_bl", "ilike", k[-6:]))]
        orders = self.odoo.search_read(
            "purchase.order",
            [("state", "!=", "cancel"), ("company_id", "=", self.run.booker.company_id)]
            + ["|"] * (len(clauses) - 1) + clauses,
            ["name", "partner_id", "partner_ref", "master_bl", "house_bl", "invoice_ids", "picking_type_id",
             "date_order"])
        orders = [o for o in orders if keys & {normalize_identifier(p) for p in re.split(
            r"[,;/&\n]|\bAND\b", f"{o['master_bl'] or ''},{o['house_bl'] or ''}", flags=re.I)}]
        if not orders:
            return {}
        lines = self.odoo.search_read("purchase.order.line", [("order_id", "in", [o["id"] for o in orders])],
                                      ["order_id", "product_id", "product_qty"])
        product_ids = sorted({m2o_id(line["product_id"]) for line in lines if line["product_id"]})
        storable = {p["id"] for p in self.odoo.call("product.product", "read", product_ids, ["is_storable"])
                    if p["is_storable"]}
        freight_products = {m["freight_product_id"] for m in self.run.booker.mappings if m["freight_product_id"]}
        freight_products |= {m2o_id(self.run.booking["freight_product_id"]),
                             m2o_id(self.run.booking["mapping_freight_product_id"]),
                             self.run.updates.get("mapping_freight_product_id")} - {False, None}
        goods = [o for o in orders if any(m2o_id(line["order_id"]) == o["id"] and m2o_id(line["product_id"]) in storable
                                          for line in lines)]
        values = {}
        bill_ids = [i for o in goods for i in o["invoice_ids"]]
        bills = self.odoo.search_read("account.move", [("id", "in", bill_ids), ("state", "=", "posted"),
                                                       ("move_type", "=", "in_invoice")], ["ref"]) if bill_ids else []
        refs = sorted({normalize_identifier(r) for r in [o["partner_ref"] for o in goods] + [b["ref"] for b in bills]}
                      - {""})
        values["invoice_references"] = ",".join(refs) or False

        forwarder = self.run.current("forwarder_id")
        forwarder = forwarder[0] if isinstance(forwarder, list) else forwarder
        containers = self.odoo.call(CONTAINER, "search_count", [("booking_id", "=", self.run.id),
                                                                 ("state", "=", "active")])
        freight = [o for o in orders if (not forwarder or m2o_id(o["partner_id"]) == forwarder) and any(
            m2o_id(line["order_id"]) == o["id"] and m2o_id(line["product_id"]) in freight_products
            and (not containers or line["product_qty"] == containers) for line in lines)]
        if freight:
            order = max(freight, key=lambda o: o["date_order"])
            values["history_purchase_order_id"] = order["id"]
            if not forwarder:
                values["history_forwarder_id"] = m2o_id(order["partner_id"])
        factories = {m2o_id(o["partner_id"]) for o in goods}
        if len(factories) == 1 and not self.run.current("factory_id"):
            values["history_factory_id"] = factories.pop()
        types = {m2o_id(o["picking_type_id"]) for o in goods} - {False}
        if len(types) == 1 and not self.run.booking["warehouse_id"]:
            warehouse = self.odoo.call("stock.picking.type", "read", list(types), ["warehouse_id"])[0]["warehouse_id"]
            if warehouse:
                values["history_warehouse_id"] = warehouse[0]
        return values
