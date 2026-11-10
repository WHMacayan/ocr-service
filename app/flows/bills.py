"""Scraped Bills: one b2b.ocr.scraped.bill per supplier invoice (vendor +
invoice number), matched to the vendor's draft bill, else purchase order,
else parked as Ready to Create PO.

Port of b2b_freight_ocr's supplier invoice rules. The service only suggests:
confirming the bill or order found is a button in Odoo.
"""

import re

from .freight import m2o_id, resolve_mapping
from ..documents.text import normalize_identifier, parse_date, split_list

SCRAPED_BILL = "b2b.ocr.scraped.bill"
BILL_LINE = "b2b.ocr.scraped.bill.line"
_SCALARS = (("invoice_date", "Invoice date"), ("currency_name", "Currency"), ("amount_total", "Total"),
            ("master_bl", "Master B/L"), ("house_bl", "House B/L"))
_LISTS = (("po_numbers", "po_numbers"), ("container_numbers", "containers"))
_READ = ["name", "external_key", "partner_id", "vendor_name", "vendor_vat", "invoice_date", "currency_name",
         "amount_total", "po_numbers", "master_bl", "house_bl", "container_numbers", "source", "conflict_notes",
         "state"]
# A person moved these on; the service leaves them alone.
SETTLED_STATES = ("needs_review", "bill_confirmed", "po_confirmed")


def _keys(text):
    return {normalize_identifier(part) for part in re.split(r"[,;/&\n]", text or "")} - {""}


# A tyre size (235/40ZR19, LT225/75R16, 295/75R22.5) or a wheel size (18X8.5), as printed.
_SIZE = re.compile(r"(?<![\w/])(?:LT|P)?(\d{2,3}(?:/\d{2,3})?)\s*Z?R\s*(\d{2}(?:\.\d)?)(?!\d)|(?<![\w.])(\d{2}(?:\.\d)?)\s*X\s*(\d{1,2}(?:\.\d{1,2})?)(?![\d.])",
                   re.IGNORECASE)


def sizes(text):
    """The sizes a text prints, written one way: 235/40R19 for 235/40ZR19, 18X8.5 for 18 x 8.5."""
    return {f"{m[0]}R{m[1]}" if m[0] else f"{m[2]}X{m[3]}" for m in _SIZE.findall(text or "")}


class BillMatcher:
    def __init__(self, odoo, company_id, mappings):
        self.odoo = odoo
        self.company_id = company_id
        self.mappings = mappings
        self._po_fields = None
        self._line_fields = None
        self._products = {}

    # ------------------------------------------------------------------
    # From a document
    # ------------------------------------------------------------------

    def take(self, doc):
        """``(state, note, scraped_bill_id)``: ``done`` once the invoice is in
        Odoo, ``waiting`` when it prints no invoice number."""
        values = doc["values"]
        number = normalize_identifier(values.get("invoice_number"))
        if not number:
            return "waiting", "No invoice number printed.", None
        sender_domain = (doc["sender"] or "").rpartition("@")[2]
        # The printed vendor, not the resolved partner: a mapping added later
        # must not turn the same invoice into a new one.
        vendor_key = " ".join((values.get("vendor_name") or "").upper().split()) or f"@{sender_domain}"
        external_key = f"{vendor_key}|{number}"
        found = self.odoo.search_read(SCRAPED_BILL, [("company_id", "=", self.company_id),
                                                     ("external_key", "=", external_key)], _READ)
        if found:
            bill = found[0]
            # Still no vendor: a mapping added since may name it now.
            partner = not bill["partner_id"] and self.resolve_vendor(
                values.get("vendor_name"), values.get("vendor_vat"), sender_domain)
            if partner:
                self.odoo.write(SCRAPED_BILL, [bill["id"]], {"partner_id": partner})
        else:
            partner = self.resolve_vendor(values.get("vendor_name"), values.get("vendor_vat"), sender_domain)
            bill_id = self.odoo.create(SCRAPED_BILL, {
                "name": values["invoice_number"], "external_key": external_key, "company_id": self.company_id,
                "partner_id": partner, "sender_email": doc["sender"]})
            bill = self.odoo.call(SCRAPED_BILL, "read", [bill_id], _READ)[0]
        self.merge(bill, values, doc["source"])
        self.add_lines(bill["id"], values.get("lines"))
        if bill["state"] not in SETTLED_STATES:
            self.match(self.odoo.call(SCRAPED_BILL, "read", [bill["id"]], _READ)[0])
        return "done", None, bill["id"]

    def resolve_vendor(self, name, vat, sender_domain):
        """Vendor mapping by sender domain or the name it prints, then partner by printed name,
        tax id, sender domain. A step finding several vendors finds none; the own company never counts."""
        for role in ("factory", "forwarder"):
            row = resolve_mapping(self.mappings, role, sender_domain=sender_domain, name=name)
            if row:
                return row["partner_id"]
        own = self.odoo.call("res.company", "read", [self.company_id], ["partner_id"])[0]["partner_id"][0]
        vat_key = normalize_identifier(vat)
        steps = []
        if (name or "").strip():
            steps.append(([("name", "=ilike", name.strip())], None))
        if vat_key:
            steps.append(([("vat", "ilike", vat_key[-6:])], lambda p: normalize_identifier(p["vat"]) == vat_key))
        if sender_domain:
            steps.append(([("email", "=ilike", f"%@{sender_domain}")], None))
        for domain, keep in steps:
            partners = self.odoo.search_read("res.partner", domain, ["commercial_partner_id", "vat"])
            found = {m2o_id(p["commercial_partner_id"]) for p in partners if not keep or keep(p)} - {own}
            if len(found) == 1:
                return found.pop()
        return False

    def merge(self, bill, values, source):
        """Fill what is empty; a different value goes to Conflict Notes."""
        updates, conflicts = {}, []
        for name in ("vendor_name", "vendor_vat"):
            if values.get(name) and not bill[name]:
                updates[name] = values[name]
        incoming = dict(values, currency_name=values.get("currency"), invoice_date=parse_date(values.get("invoice_date")))
        for name, label in _SCALARS:
            new = incoming.get(name)
            if new in (None, "", False):
                continue
            old = bill[name]
            if not old:
                updates[name] = new
            elif (abs(old - float(new)) > self.rounding(bill["currency_name"]) if name == "amount_total"
                  else normalize_identifier(old) != normalize_identifier(new)):
                line = f"{label}: kept {old}, {source} says {new}"
                if line not in (bill["conflict_notes"] or ""):
                    conflicts.append(line)
        for name, key in _LISTS:
            known = split_list(bill[name])
            added = [v for v in values.get(key) or [] if v not in known]
            if added:
                updates[name] = ", ".join(known + added)
        sources = split_list(bill["source"])
        if source not in sources:
            updates["source"] = ", ".join(sources + [source])
        if conflicts:
            updates["conflict_notes"] = "\n".join(filter(None, [bill["conflict_notes"]] + conflicts))
        if updates:
            self.odoo.write(SCRAPED_BILL, [bill["id"]], updates)

    def add_lines(self, bill_id, lines, replace=False):
        """The invoice's lines, from the first document that prints them: a copy adds none. replace:
        the lines read again take the place of the ones there (an invoice re-read)."""
        lines = [line for line in lines or [] if line.get("description") or line.get("amount")]
        if not lines:
            return
        old = self.odoo.call(BILL_LINE, "search", [("bill_scrape_id", "=", bill_id)])
        if old and not replace:
            return
        if old:
            self.odoo.call(BILL_LINE, "unlink", old)
        has_products = "product_id" in self.line_fields()  # the Odoo module before Part Number and Product
        for line in lines:
            values = {"bill_scrape_id": bill_id, "description": line.get("description") or "",
                      "quantity": line.get("quantity") or 0, "price_unit": line.get("unit_price") or 0,
                      "amount": line.get("amount") or 0}
            if has_products:
                values.update(product_code=line.get("part_number") or False,
                              product_id=self.product_of(line.get("part_number"), line.get("description")))
            self.odoo.create(BILL_LINE, values)

    def line_fields(self):
        if self._line_fields is None:
            self._line_fields = self.odoo.call(BILL_LINE, "fields_get", attributes=["type"])
        return self._line_fields

    def product_of(self, part_number, description):
        """The one product an invoice line names: the internal reference is the part number, or ends with
        it (ARROYOAGS089 for AGS089); when the line prints a size, the product's name must print it too.
        False when none fits, or several do."""
        code = normalize_identifier(part_number)
        if len(code) < 4:
            return False
        key = (code, frozenset(sizes(description)))
        if key not in self._products:
            found = [p for p in self.odoo.search_read("product.product", [("default_code", "=ilike", f"%{code}")],
                                                      ["default_code", "name"], limit=50)
                     if normalize_identifier(p["default_code"]).endswith(code)]
            if key[1]:
                found = [p for p in found if sizes(p["name"]) & key[1]]
            self._products[key] = found[0]["id"] if len(found) == 1 else False
        return self._products[key]

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def rematch_waiting(self, limit):
        """Bills sent back by Rematch or Resolve in Odoo, or still without a match."""
        for bill in self.odoo.search_read(SCRAPED_BILL, [("company_id", "=", self.company_id),
                                                         ("state", "in", ("waiting", "ready_for_po"))],
                                          _READ, limit=limit, order="write_date"):
            self.match(bill)

    def match(self, bill):
        cleared = {"bill_id": False, "purchase_id": False, "match_notes": False, "waiting_reason": False}
        if bill["conflict_notes"]:
            return self._write(bill, dict(cleared, state="waiting", waiting_reason="conflict"))
        if not bill["partner_id"]:
            return self._write(bill, dict(cleared, state="waiting", waiting_reason="vendor_not_found"))
        number = normalize_identifier(bill["name"])
        vendor = [("partner_id", "child_of", m2o_id(bill["partner_id"])), ("company_id", "=", self.company_id)]
        pos = _keys(bill["po_numbers"])
        shipment = _keys(bill["master_bl"]) | _keys(bill["house_bl"]) | _keys(bill["container_numbers"])
        # Already billed: the vendor's posted bill carries this invoice number.
        posted = self._posted(vendor, number)
        if posted:
            return self._write(bill, dict(cleared, state="bill_confirmed", bill_id=posted[0]["id"],
                                          posted_bill_warning=False, match_notes=(
                                              f"Already billed: {posted[0]['display_name']} carries this invoice number.")))
        values = dict(cleared, posted_bill_warning=False)

        drafts = self.odoo.search_read("account.move", vendor + [("move_type", "=", "in_invoice"),
                                                                 ("state", "=", "draft")], ["ref", "display_name",
                                                                                            "amount_total"])
        orders_of = {}
        if drafts and pos:
            for line in self.odoo.search_read("account.move.line", [("move_id", "in", [d["id"] for d in drafts]),
                                                                    ("purchase_line_id", "!=", False)],
                                              ["move_id", "purchase_order_id"]):
                if line["purchase_order_id"]:
                    orders_of.setdefault(m2o_id(line["move_id"]), set()).add(
                        normalize_identifier(line["purchase_order_id"][1]))
        drafts = [d for d in drafts if normalize_identifier(d["ref"]) == number or pos & orders_of.get(d["id"], set())]
        taken = {m2o_id(b["bill_id"]) for b in self.odoo.search_read(
            SCRAPED_BILL, [("id", "!=", bill["id"]), ("bill_id", "in", [d["id"] for d in drafts])], ["bill_id"])}
        free = [d for d in drafts if d["id"] not in taken]
        if len(free) == 1:
            return self._write(bill, dict(values, state="bill_found", bill_id=free[0]["id"],
                                          match_notes=self._total_note(bill, free[0]["amount_total"])))
        if drafts:
            return self._write(bill, dict(values, state="needs_review", match_notes=(
                f"Draft bills {', '.join(d['display_name'] for d in drafts)} fit (or are already matched): "
                f"choose one.")))

        # The database narrows by the identifiers' tails; the exact comparison decides.
        shipment_fields = [f for f in ("master_bl", "house_bl", "container_number") if f in self.po_fields()]
        clauses = [("partner_ref", "ilike", number[-6:])]
        clauses += [("name", "ilike", key[-6:]) for key in pos]
        clauses += [(f, "ilike", key[-6:]) for key in shipment for f in shipment_fields]
        orders = self.odoo.search_read(
            "purchase.order", vendor + [("state", "!=", "cancel")] + ["|"] * (len(clauses) - 1) + clauses,
            ["name", "partner_ref", "amount_total"] + shipment_fields)
        orders = [o for o in orders if normalize_identifier(o["name"]) in pos
                  or normalize_identifier(o["partner_ref"]) == number
                  or shipment & set().union(*(_keys(o[f] or "") for f in shipment_fields))]
        if len(orders) == 1:
            return self._write(bill, self._with_po_bill(values, bill, orders[0], number))
        if orders:
            return self._write(bill, dict(values, state="needs_review", match_notes=(
                f"Purchase orders {', '.join(o['name'] for o in orders)} fit: choose one.")))
        return self._write(bill, dict(values, state="ready_for_po",
                                      match_notes="No draft bill or purchase order of this vendor fits."))

    def _with_po_bill(self, values, bill, order, number):
        """The purchase order found, with its vendor bill when it has one: a draft is found for
        confirming, a posted one means the invoice is already billed."""
        values = dict(values, state="po_found", purchase_id=order["id"],
                      match_notes=self._total_note(bill, order["amount_total"]))
        moves = [m for m in self.odoo.call("account.move", "read", self._po_bills(order["id"]),
                                           ["ref", "display_name", "state", "move_type", "amount_total"])
                 if m["move_type"] == "in_invoice" and m["state"] in ("draft", "posted")]
        # This invoice's own bill first, else the order's only bill.
        own = [m for m in moves if normalize_identifier(m["ref"]) == number] or (moves if len(moves) == 1 else [])
        if not own:
            if moves:
                values["match_notes"] = (f"Purchase order {order['name']} has several bills "
                                         f"({', '.join(m['display_name'] for m in moves)}): choose one.")
            return values
        move = own[0]
        if move["state"] == "draft":
            return dict(values, state="bill_found", bill_id=move["id"],
                        match_notes=self._total_note(bill, move["amount_total"]))
        return dict(values, state="bill_confirmed", bill_id=move["id"], match_notes=(
            f"Already billed: {move['display_name']} is the bill of purchase order {order['name']}."))

    def _po_bills(self, order_id):
        return self.odoo.call("purchase.order", "read", [order_id], ["invoice_ids"])[0]["invoice_ids"] or []

    def po_fields(self):
        if self._po_fields is None:
            self._po_fields = self.odoo.call("purchase.order", "fields_get", attributes=["type"])
        return self._po_fields

    def _posted(self, vendor, number):
        """The vendor's posted bills whose reference is this invoice number."""
        return [m for m in self.odoo.search_read("account.move", vendor + [
            ("move_type", "=", "in_invoice"), ("state", "=", "posted"), ("ref", "ilike", number[-6:])],
            ["ref", "display_name"]) if normalize_identifier(m["ref"]) == number]

    def rounding(self, currency_name):
        """The printed currency's rounding, else the company currency's."""
        currency = self.odoo.search_read("res.currency", [("name", "=", (currency_name or "").upper())],
                                         ["rounding"], context={"active_test": False}, limit=1)
        if not currency:
            company = self.odoo.call("res.company", "read", [self.company_id], ["currency_id"])[0]
            currency = self.odoo.call("res.currency", "read", [company["currency_id"][0]], ["rounding"])
        return currency[0]["rounding"]

    def _total_note(self, bill, odoo_total):
        if bill["amount_total"] and abs(odoo_total - bill["amount_total"]) > self.rounding(bill["currency_name"]):
            return f"Totals differ: invoice {bill['amount_total']}, Odoo {odoo_total}."
        return False

    def _write(self, bill, values):
        self.odoo.write(SCRAPED_BILL, [bill["id"]], values)
