"""The team's own freight POs: finding the one for a booking, and copying what it knows."""

import re
from datetime import date, timedelta

from app.shared.documents.text import normalize_identifier, plausible_identifier, valid_container_number
from app.shared.odoo.mappings import m2o_id
from app.freight_booking.rules.identifiers import _SCAC, reference_number
from app.freight_booking.rules.models import BOOKING, CONTAINER


def copy_po_details(odoo, booking_id, order_id):
    """The team's freight PO knows the shipment: its MBL, HBL and containers fill the booking's empty ones, and an
    Ocean Freight PO makes the mode ocean (booking 35254926: linked to P68349, yet no B/L and mode Unknown,
    17 Nov 2026). Returns what was copied."""
    po = odoo.call("purchase.order", "read", [order_id], ["name", "master_bl", "house_bl", "container_number"])[0]
    booking = odoo.call(BOOKING, "read", [booking_id], ["master_bl", "house_bl", "freight_mode", "invoice_ids"])[0]
    source = f"team PO {po['name']}"
    labels = odoo.call(BOOKING, "fields_get", attributes=["type"])
    updates, copied = {}, []
    for name in ("master_bl", "house_bl"):
        first = re.split(r"[,;/&\n]", po.get(name) or "")[0]
        value = normalize_identifier(first)
        if value and plausible_identifier(value) and not booking[name]:
            updates[name] = value
            if f"{name}_source" in labels:
                updates[f"{name}_source"] = source
            copied.append(f"{'MBL' if name == 'master_bl' else 'HBL'} {value}")
    if booking["freight_mode"] in (False, "unknown"):
        updates["freight_mode"] = "ocean"
        copied.append("mode Ocean")
    if updates:
        odoo.write(BOOKING, [booking_id], updates)
    numbers = [c for c in dict.fromkeys(normalize_identifier(c) for c in re.split(r"[,;/\s]+", po.get("container_number") or ""))
               if valid_container_number(c)]
    if numbers and not odoo.call(CONTAINER, "search_count", [("booking_id", "=", booking_id), ("state", "=", "active")]):
        invoice = booking["invoice_ids"][0] if len(booking["invoice_ids"]) == 1 else False
        for number in numbers:
            values = {"booking_id": booking_id, "container_no": number, "source": source}
            if invoice:
                values["invoice_id"] = invoice
            odoo.create(CONTAINER, values)
        copied.append(f"containers {', '.join(numbers)}")
    return copied


class _PurchaseHistory:
    """What Odoo's purchases already say about this shipment, as suggestions."""

    def __init__(self, run):
        self.run = run
        self.odoo = run.odoo

    _FIELDS = ["name", "partner_id", "partner_ref", "master_bl", "house_bl", "invoice_ids", "picking_type_id",
               "date_order"]

    def by_containers(self, held, open_orders):
        """POs listing every container this booking holds, raised around its sailing. A container is reused shipment
        after shipment, so only a PO dated from 30 days before the ETD to 150 days after it counts."""
        try:
            sailed = date.fromisoformat(str(self.run.current("etd") or "")[:10])
        except ValueError:
            return []
        if not held:
            return []
        clauses = [("container_number", "ilike", c) for c in held]
        out = []
        for o in self.odoo.search_read("purchase.order", open_orders + ["|"] * (len(clauses) - 1) + clauses,
                                       self._FIELDS + ["container_number"]):
            listed = {normalize_identifier(c) for c in re.split(r"[,;/&\s]+", o["container_number"] or "")}
            try:
                day = date.fromisoformat(str(o["date_order"] or "")[:10])
            except ValueError:
                continue
            if set(held) <= listed and sailed - timedelta(days=30) <= day <= sailed + timedelta(days=150):
                out.append(o)
        return out

    def suggestions(self):
        keys = {k for k in (self.run.current("master_bl"), self.run.current("house_bl")) if k}
        number = _SCAC.sub("", normalize_identifier(self.run.current("booking_number")))
        po_fields = self.odoo.call("purchase.order", "fields_get", attributes=["type"])
        held = [c["container_no"] for c in self.odoo.search_read(
            CONTAINER, [("booking_id", "=", self.run.id), ("state", "=", "active")], ["container_no"])]
        if (not keys and len(number) < 6 and not held) or "master_bl" not in po_fields:
            return {}
        open_orders = [("state", "!=", "cancel"), ("company_id", "=", self.run.booker.company_id)]
        orders = []
        if keys:
            # The carrier code may stand before the number on either side: WHLC035GX69537 is 035GX69537 (P70039).
            bare = {_SCAC.sub("", normalize_identifier(k)) for k in keys}
            clauses = [c for k in keys for c in (("master_bl", "ilike", k[-6:]), ("house_bl", "ilike", k[-6:]))]
            orders = self.odoo.search_read("purchase.order", open_orders + ["|"] * (len(clauses) - 1) + clauses,
                                           self._FIELDS)
            orders = [o for o in orders if bare & {_SCAC.sub("", normalize_identifier(p)) for p in re.split(
                r"[,;/&\n]|\bAND\b", f"{o['master_bl'] or ''},{o['house_bl'] or ''}", flags=re.I)}]
        by_container = self.by_containers(held, open_orders) if "container_number" in po_fields else []
        # The team's Vendor Reference is the booking number on most freight POs (Hapag 94659872; De Well
        # 31978034-2 for a split PO): it finds the PO first, the B/L when it holds something else (user, 17 Nov 2026).
        by_reference = []
        if len(number) >= 6:
            by_reference = [o for o in self.odoo.search_read("purchase.order", open_orders + [("partner_ref", "ilike", number)],
                                                              self._FIELDS)
                            if reference_number(o["partner_ref"]) == number]
        if not orders and not by_reference and not by_container:
            return {}
        lines = self.odoo.search_read("purchase.order.line",
                                      [("order_id", "in", sorted({o["id"] for o in orders + by_reference + by_container}))],
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
        if orders:  # the goods POs are found by B/L only
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
        def ocean(o, whole=True):
            return any(m2o_id(line["order_id"]) == o["id"] and m2o_id(line["product_id"]) in freight_products
                       and (not whole or not containers or line["product_qty"] == containers) for line in lines)

        def same_vendor(o):
            return not forwarder or m2o_id(o["partner_id"]) == forwarder

        found = {o["id"] for o in orders + by_container}
        # The PO named after the booking is its PO; under another vendor too when a B/L or container agrees, the
        # vendor on the papers being the one wrong (K-APEX papers, Quartz PO P78483 for BKKM68473200).
        freight = ([o for o in by_reference if ocean(o) and (same_vendor(o) or o["id"] in found)]
                   or [o for o in orders if same_vendor(o) and ocean(o)]
                   # A PO raised for two bookings holds both shipments' containers (P78536: 22492448 and 37643170).
                   or [o for o in by_container if same_vendor(o) and ocean(o, whole=False)])
        if freight:
            order = max(freight, key=lambda o: o["date_order"])
            values["history_purchase_order_id"] = order["id"]
            self.run.history_order = order
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
