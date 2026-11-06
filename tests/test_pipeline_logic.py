"""Reading, booking consolidation and bill matching against an in-memory Odoo."""

from datetime import date

from app.flows.bills import BillMatcher
from app.flows.freight import Booker
from app.documents.extract import checked, email_values
from app.documents.text import newest_message_text, normalize_identifier, parse_month_date

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


# ── reading ──────────────────────────────────────────────────────────

def test_normalize_and_placeholders():
    assert normalize_identifier("bkkgm-682/2500") == "BKKGM6822500"
    assert normalize_identifier("N/A") == ""


def test_newest_message_text_drops_quoted_history():
    html = "<div>ETD 18/OCT</div><div>From: someone</div><div>old BKG 99999999</div>"
    assert newest_message_text(html) == "ETD 18/OCT"


def test_checked_keeps_only_printed_values():
    text = "Booking No: ABC123456 Container MSCU1234567 ETD 2026-10-18"
    values = checked({"booking_number": "ABC123456", "master_bl": "HLCU999999", "containers": ["MSCU1234567",
                      "TGHU7654321"], "etd": "2026-10-18", "eta": "2026-11-20", "vessel": None}, text)
    assert values == {"booking_number": "ABC123456", "containers": ["MSCU1234567"], "etd": "2026-10-18"}


def test_email_values_from_subject_format():
    formats = [{"sender_domains": "fwd.com", "subject_pattern": r"BKG (?P<booking_number>\w+) ETD (?P<etd>\S+)",
                "confirms_booking": 1}]
    values = email_values(formats, "", "BKG AB12345678 ETD 18/OCT", "", "ops@fwd.com", "2026-10-01T08:00:00")
    assert values == {"booking_number": "AB12345678", "etd": "2026-10-18", "confirms_booking": True}
    assert parse_month_date("OCT 9", date(2026, 12, 30)) == "2026-10-09"


# ── freight booking ──────────────────────────────────────────────────

def test_confirmation_creates_booking_and_mirrors_bl():
    odoo = FakeOdoo()
    booker = make_booker(odoo)
    state, _note, booking_id = booker.take(doc("booking_confirmation", booking_number="AB123456",
                                               master_bl="HLCU123456789", vessel="EVER GIVEN", etd="2026-10-18"))
    assert state == "done"
    row = odoo.rows("b2b.ocr.booking")[booking_id]
    assert row["vessel"] == "EVER GIVEN" and row["vessel_source"] == "a.pdf"
    assert row["house_bl"] == "HLCU123456789" and row["house_bl_assumed"] is True
    assert [r["bl_number"] for r in odoo.rows("b2b.ocr.booking.bl").values()] == ["HLCU123456789"]


def test_other_document_without_booking_waits():
    odoo = FakeOdoo()
    state, note, _ = make_booker(odoo).take(doc("commercial_invoice", booking_number="AB123456"))
    assert state == "waiting" and "No booking" in note
    assert not odoo.rows("b2b.ocr.booking")


def test_different_value_is_one_conflict_even_when_read_twice():
    odoo = FakeOdoo()
    booker = make_booker(odoo, newest=False)
    booker.take(doc("booking_confirmation", booking_number="AB123456", vessel="EVER GIVEN"))
    second = doc("mbl_hbl", "bl.pdf", booking_number="AB123456", vessel="MSC ANNA")
    booker.take(second)
    booker.take(second)
    conflicts = list(odoo.rows("b2b.ocr.booking.conflict").values())
    assert len(conflicts) == 1
    assert conflicts[0]["existing_value"] == "EVER GIVEN" and conflicts[0]["new_value"] == "MSC ANNA"


def test_confirmed_field_is_never_touched():
    odoo = FakeOdoo()
    booker = make_booker(odoo)
    _s, _n, booking_id = booker.take(doc("booking_confirmation", booking_number="AB123456",
                                         master_bl="HLCU111111111"))
    odoo.write("b2b.ocr.booking", [booking_id], {"master_bl_confirmed": True})
    booker.take(doc("mbl_hbl", "bl.pdf", booking_number="AB123456", master_bl="HLCU222222222"))
    assert odoo.rows("b2b.ocr.booking")[booking_id]["master_bl"] == "HLCU111111111"
    assert not odoo.rows("b2b.ocr.booking.conflict")


def test_settled_conflict_does_not_come_back():
    odoo = FakeOdoo()
    booker = make_booker(odoo, newest=False)
    booker.take(doc("booking_confirmation", booking_number="AB123456", vessel="EVER GIVEN"))
    second = doc("mbl_hbl", "bl.pdf", booking_number="AB123456", vessel="MSC ANNA")
    booker.take(second)
    for conflict in odoo.rows("b2b.ocr.booking.conflict").values():
        conflict["state"] = "resolved"  # a person pressed Keep Existing
    booker.take(second)
    assert len(odoo.rows("b2b.ocr.booking.conflict")) == 1


def test_only_a_commercial_invoice_adds_a_factory_invoice():
    odoo = FakeOdoo()
    booker = make_booker(odoo)
    booker.take(doc("booking_confirmation", booking_number="56003104"))
    booker.take(doc("packing_list", "F26AR10MO552 PL.pdf", booking_number="56003104", invoices=["CC11804"]))
    booker.take(doc("commercial_invoice", "F26AR10MO552 CI.pdf", booking_number="56003104",
                    invoices=["F26AR10MO552"]))
    assert [r["invoice_number"] for r in odoo.rows("b2b.ocr.booking.invoice").values()] == ["F26AR10MO552"]


def test_new_containers_become_one_change_for_a_person():
    odoo = FakeOdoo()
    booker = make_booker(odoo)
    booker.take(doc("booking_confirmation", booking_number="AB123456", containers=["MSCU1234567"]))
    later = doc("packing_list", "pl.pdf", booking_number="AB123456", containers=["TGHU7654321"])
    booker.take(later)
    booker.take(later)
    assert len(odoo.rows("b2b.ocr.booking.container")) == 1
    changes = list(odoo.rows("b2b.ocr.booking.container.change").values())
    assert len(changes) == 1 and "TGHU7654321" in changes[0]["new_container_lines"]


def test_forwarder_from_mapping_and_own_company_ignored():
    odoo = FakeOdoo()
    booker = make_booker(odoo, [mapping(sender_domain="fwd.com", partner_id=7, freight_product_id=9,
                                        cost_per_container=1500.0)])
    _s, _n, booking_id = booker.take(doc("booking_confirmation", booking_number="AB123456",
                                         forwarder_name="Fast Forwarding Ltd",
                                         factory_name="Wholesale Tire Distributors"))
    row = odoo.rows("b2b.ocr.booking")[booking_id]
    assert row["forwarder_id"] == 7 and row["mapping_freight_product_id"] == 9
    assert row["mapping_cost_per_container"] == 1500.0
    assert not row.get("factory_name")


def test_email_creates_booking_only_when_format_confirms_from_mapped_forwarder():
    odoo = FakeOdoo()
    email = doc("email_message", "Email: x", booking_number="AB123456", confirms_booking=True)
    assert make_booker(odoo).take(email)[0] == "waiting"
    state, _n, booking_id = make_booker(odoo, [mapping(sender_domain="fwd.com")]).take(email)
    assert state == "done" and odoo.rows("b2b.ocr.booking")[booking_id]["has_uncertain_match"] is True


def test_freight_po_is_suggested_never_linked():
    odoo = FakeOdoo()
    booker = make_booker(odoo, [mapping(sender_domain="fwd.com", partner_id=7, freight_product_id=9)])
    po = odoo.add("purchase.order", name="P00042", partner_id=7, master_bl="HLCU123456789", house_bl=False,
                  state="purchase", company_id=1, invoice_ids=[], picking_type_id=False, partner_ref=False,
                  date_order="2026-10-02 00:00:00")
    odoo.add("purchase.order.line", order_id=po, product_id=9, product_qty=1)
    odoo.add("product.product", is_storable=False)
    odoo.rows("product.product")[9] = {"is_storable": False}
    _s, _n, booking_id = booker.take(doc("booking_confirmation", booking_number="AB123456",
                                         master_bl="HLCU123456789", containers=["MSCU1234567"]))
    row = odoo.rows("b2b.ocr.booking")[booking_id]
    assert row["history_purchase_order_id"] == po
    assert not row.get("purchase_order_id")


# ── scraped bills ────────────────────────────────────────────────────

def make_matcher(odoo, mappings=()):
    odoo.add("res.company", name="Wholesale Tire Distributors", partner_id=1, currency_id=2)
    odoo.rows("res.company")[1] = {"name": "Wholesale Tire Distributors", "partner_id": 1, "currency_id": 2}
    odoo.rows("res.currency")[2] = {"name": "USD", "rounding": 0.01}
    return BillMatcher(odoo, 1, list(mappings))


def test_invoice_matches_draft_bill_and_is_not_duplicated():
    odoo = FakeOdoo()
    matcher = make_matcher(odoo, [mapping(role="factory", sender_domain="factory.cn", partner_id=7)])
    bill = odoo.add("account.move", partner_id=7, company_id=1, move_type="in_invoice", state="draft",
                    ref="INV-001", display_name="BILL/1", amount_total=100.0)
    invoice = doc("supplier_invoice", "inv.pdf", sender="ar@factory.cn", invoice_number="INV-001",
                  vendor_name="Good Factory", amount_total=100.0, currency="USD")
    state, _n, scraped_id = matcher.take(invoice)
    assert state == "done"
    assert matcher.take(invoice)[2] == scraped_id
    row = odoo.rows("b2b.ocr.scraped.bill")[scraped_id]
    assert row["state"] == "bill_found" and row["bill_id"] == bill
    assert len(odoo.rows("b2b.ocr.scraped.bill")) == 1


def test_unknown_vendor_waits_and_no_number_never_reaches_odoo():
    odoo = FakeOdoo()
    matcher = make_matcher(odoo)
    _s, _n, scraped_id = matcher.take(doc("supplier_invoice", sender="x@nowhere.com", invoice_number="A1"))
    row = odoo.rows("b2b.ocr.scraped.bill")[scraped_id]
    assert row["state"] == "waiting" and row["waiting_reason"] == "vendor_not_found"
    assert matcher.take(doc("supplier_invoice", vendor_name="X"))[0] == "waiting"
    assert len(odoo.rows("b2b.ocr.scraped.bill")) == 1


def test_no_bill_no_po_is_ready_for_po():
    odoo = FakeOdoo()
    matcher = make_matcher(odoo, [mapping(role="factory", sender_domain="factory.cn", partner_id=7)])
    _s, _n, scraped_id = matcher.take(doc("supplier_invoice", sender="ar@factory.cn", invoice_number="INV-9"))
    assert odoo.rows("b2b.ocr.scraped.bill")[scraped_id]["state"] == "ready_for_po"


def test_invoice_lines_are_written_once():
    odoo = FakeOdoo()
    matcher = make_matcher(odoo, [mapping(role="factory", sender_domain="factory.cn", partner_id=7)])
    lines = [{"description": "TIRE 205/55R16", "quantity": 400, "unit_price": 25.5, "amount": 10200},
             {"description": "Freight", "quantity": None, "unit_price": None, "amount": 300}]
    invoice = doc("supplier_invoice", sender="ar@factory.cn", invoice_number="INV-9", lines=lines)
    _s, _n, scraped_id = matcher.take(invoice)
    matcher.take(doc("supplier_invoice", "copy.pdf", sender="ar@factory.cn", invoice_number="INV-9", lines=lines))

    written = sorted(odoo.rows("b2b.ocr.scraped.bill.line").values(), key=lambda line: line["amount"])
    assert [(w["bill_scrape_id"], w["description"], w["quantity"], w["price_unit"], w["amount"]) for w in written] == [
        (scraped_id, "Freight", 0, 0, 300), (scraped_id, "TIRE 205/55R16", 400, 25.5, 10200)]


def test_vendor_is_found_by_the_mappings_printed_name():
    odoo = FakeOdoo()
    matcher = make_matcher(odoo, [mapping(role="forwarder", document_name="DE WELL", partner_id=7)])
    _s, _n, scraped_id = matcher.take(doc("supplier_invoice", sender="acc@qin.dewell.com", invoice_number="GRKA-1",
                                          vendor_name="DE WELL CONTAINER SHIPPING,INC"))
    row = odoo.rows("b2b.ocr.scraped.bill")[scraped_id]
    assert row["partner_id"] == 7 and row["state"] == "ready_for_po"


def test_bill_still_without_vendor_is_resolved_again_by_a_later_document():
    odoo = FakeOdoo()
    matcher = make_matcher(odoo)
    invoice = doc("supplier_invoice", sender="acc@qin.dewell.com", invoice_number="GRKA-1",
                  vendor_name="DE WELL CONTAINER SHIPPING,INC")
    _s, _n, scraped_id = matcher.take(invoice)
    assert odoo.rows("b2b.ocr.scraped.bill")[scraped_id]["waiting_reason"] == "vendor_not_found"

    matcher.mappings.append(mapping(role="forwarder", document_name="DE WELL", partner_id=7))
    matcher.take(invoice)
    row = odoo.rows("b2b.ocr.scraped.bill")[scraped_id]
    assert row["partner_id"] == 7 and row["state"] == "ready_for_po"


def found_po_with_bill(bill_state):
    odoo = FakeOdoo()
    matcher = make_matcher(odoo, [mapping(role="factory", sender_domain="factory.cn", partner_id=7)])
    bill = odoo.add("account.move", partner_id=7, company_id=1, move_type="in_invoice", state=bill_state,
                    ref="OTHER-REF", display_name="BILL/2026/10/0900", amount_total=100.0)
    po = odoo.add("purchase.order", partner_id=7, company_id=1, state="purchase", name="P73249",
                  partner_ref="INV77", amount_total=100.0, invoice_ids=[bill])
    _s, _n, scraped_id = matcher.take(doc("supplier_invoice", sender="ar@factory.cn", invoice_number="INV77"))
    return odoo.rows("b2b.ocr.scraped.bill")[scraped_id], po, bill


def test_po_found_with_a_draft_bill_tags_the_bill():
    row, po, bill = found_po_with_bill("draft")
    assert (row["state"], row["purchase_id"], row["bill_id"]) == ("bill_found", po, bill)


def test_po_found_with_a_posted_bill_is_already_billed():
    row, po, bill = found_po_with_bill("posted")
    assert (row["state"], row["purchase_id"], row["bill_id"]) == ("bill_confirmed", po, bill)
    assert row["match_notes"] == "Already billed: BILL/2026/10/0900 is the bill of purchase order P73249."


def test_invoice_on_a_posted_bill_is_already_billed():
    odoo = FakeOdoo()
    matcher = make_matcher(odoo, [mapping(role="factory", sender_domain="factory.cn", partner_id=7)])
    posted = odoo.add("account.move", partner_id=7, company_id=1, move_type="in_invoice", state="posted",
                      ref="F26AR10THA0692", display_name="BILL/2026/10/0763", amount_total=100.0)
    odoo.add("purchase.order", partner_id=7, company_id=1, state="purchase", name="P73249",
             partner_ref="F26AR10THA0692", amount_total=100.0)
    _s, _n, scraped_id = matcher.take(doc("supplier_invoice", sender="ar@factory.cn", invoice_number="F26AR10THA0692"))
    row = odoo.rows("b2b.ocr.scraped.bill")[scraped_id]
    assert row["state"] == "bill_confirmed" and row["bill_id"] == posted
    assert row["match_notes"] == "Already billed: BILL/2026/10/0763 carries this invoice number."
