"""Reading, booking consolidation and bill matching against an in-memory Odoo."""

from datetime import date

from app.flows.bills import _READ, BillMatcher
from app.documents.extract import checked, email_values
from app.documents.text import newest_message_text, normalize_identifier, parse_month_date
from fakes import doc, FakeOdoo, make_booker, mapping


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


def test_no_bill_falls_back_to_the_purchase_order_and_then_its_bill():
    odoo = FakeOdoo()
    matcher = make_matcher(odoo, [mapping(role="factory", sender_domain="factory.cn", partner_id=7)])
    order = odoo.add("purchase.order", partner_id=7, company_id=1, state="purchase", name="PO0042",
                     partner_ref="", amount_total=100.0, invoice_ids=[])
    invoice = doc("supplier_invoice", sender="ar@factory.cn", invoice_number="INV-9", po_numbers=["PO0042"],
                  amount_total=100.0, currency="USD")
    _s, _n, scraped_id = matcher.take(invoice)
    row = odoo.rows("b2b.ocr.scraped.bill")[scraped_id]
    assert row["state"] == "po_found" and row["purchase_id"] == order  # no bill anywhere: the order is found

    bill = odoo.add("account.move", partner_id=7, company_id=1, move_type="in_invoice", state="draft",
                    ref="", display_name="BILL/9", amount_total=100.0)
    odoo.rows("purchase.order")[order]["invoice_ids"] = [bill]
    matcher.match(odoo.call("b2b.ocr.scraped.bill", "read", [scraped_id], _READ)[0])  # checked again
    row = odoo.rows("b2b.ocr.scraped.bill")[scraped_id]
    assert row["state"] == "bill_found" and row["bill_id"] == bill and row["purchase_id"] == order


def test_a_bill_found_also_names_its_purchase_order():
    odoo = FakeOdoo()
    matcher = make_matcher(odoo, [mapping(role="factory", sender_domain="factory.cn", partner_id=7)])
    order = odoo.add("purchase.order", partner_id=7, company_id=1, state="purchase", name="PO0042",
                     partner_ref="", amount_total=100.0, invoice_ids=[])
    bill = odoo.add("account.move", partner_id=7, company_id=1, move_type="in_invoice", state="draft",
                    ref="INV-5", display_name="BILL/5", amount_total=100.0)
    po_line = odoo.add("purchase.order.line", order_id=order)
    odoo.add("account.move.line", move_id=bill, purchase_line_id=po_line, purchase_order_id=order)
    scraped = matcher.take(doc("supplier_invoice", sender="ar@factory.cn", invoice_number="INV-5"))[2]
    row = odoo.rows("b2b.ocr.scraped.bill")[scraped]
    assert row["state"] == "bill_found" and row["bill_id"] == bill and row["purchase_id"] == order


def test_an_order_already_matched_to_another_scraped_bill_is_flagged_duplicate():
    odoo = FakeOdoo()
    matcher = make_matcher(odoo, [mapping(role="factory", sender_domain="factory.cn", partner_id=7)])
    order = odoo.add("purchase.order", partner_id=7, company_id=1, state="purchase", name="PO0042",
                     partner_ref="", amount_total=100.0, invoice_ids=[])
    first = matcher.take(doc("supplier_invoice", sender="ar@factory.cn", invoice_number="INV-1", po_numbers=["PO0042"]))[2]
    second = matcher.take(doc("supplier_invoice", sender="ar@factory.cn", invoice_number="INV-2", po_numbers=["PO0042"]))[2]
    assert odoo.rows("b2b.ocr.scraped.bill")[first]["state"] == "po_found"
    row = odoo.rows("b2b.ocr.scraped.bill")[second]
    assert row["state"] == "needs_review" and row["purchase_id"] == order
    assert row["match_notes"].startswith("Duplicate purchase order: PO0042")


def test_several_orders_that_fit_are_flagged_duplicate():
    odoo = FakeOdoo()
    matcher = make_matcher(odoo, [mapping(role="factory", sender_domain="factory.cn", partner_id=7)])
    for name in ("PO0042", "PO0043"):
        odoo.add("purchase.order", partner_id=7, company_id=1, state="purchase", name=name, partner_ref="",
                 amount_total=1.0, invoice_ids=[])
    scraped = matcher.take(doc("supplier_invoice", sender="ar@factory.cn", invoice_number="INV-9",
                               po_numbers=["PO0042", "PO0043"]))[2]
    row = odoo.rows("b2b.ocr.scraped.bill")[scraped]
    assert row["state"] == "needs_review" and row["match_notes"].startswith("Duplicate purchase orders PO0042, PO0043")


def test_invoice_lines_are_not_written():
    odoo = FakeOdoo()
    matcher = make_matcher(odoo, [mapping(role="factory", sender_domain="factory.cn", partner_id=7)])
    matcher.take(doc("supplier_invoice", sender="ar@factory.cn", invoice_number="INV-9",
                     lines=[{"description": "TIRE", "quantity": 1, "unit_price": 2, "amount": 2}]))
    assert not odoo.rows("b2b.ocr.scraped.bill.line")


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
