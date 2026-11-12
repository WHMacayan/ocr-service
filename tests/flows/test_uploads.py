"""Invoices uploaded by hand in Odoo: bills in To Read are read, matched, or parked for a person."""

import base64

from app.documents import extract
from app.flows import uploads

from fakes import FakeOdoo, mapping
from flows.test_pipeline_logic import make_matcher  # noqa: F401


class Run:
    def __init__(self, odoo, matcher):
        self.odoo, self.settings, self._matcher = odoo, {}, matcher

    def matcher(self, _company_id):
        return self._matcher


def upload(odoo, name="scan.pdf"):
    bill = odoo.add("b2b.ocr.scraped.bill", name=name, source=name, company_id=1, state="to_read",
                    external_key="upload-ab12")
    odoo.add("ir.attachment", res_model="b2b.ocr.scraped.bill", res_id=bill, name=name,
             datas=base64.b64encode(b"%PDF").decode())
    return bill


def reading(monkeypatch, doc_class, **values):
    monkeypatch.setattr(extract, "read_file", lambda raw, purpose, settings: (doc_class, values))


def setup():
    odoo = FakeOdoo()
    matcher = make_matcher(odoo, [mapping(role="factory", sender_domain=None, document_name="GOOD FACTORY", partner_id=7)])
    return odoo, Run(odoo, matcher)


def test_an_uploaded_invoice_takes_its_identity_and_is_matched(monkeypatch):
    odoo, run = setup()
    order = odoo.add("purchase.order", partner_id=7, company_id=1, state="purchase", name="PO0042",
                     partner_ref="", amount_total=100.0, invoice_ids=[])
    placeholder = upload(odoo)
    reading(monkeypatch, "supplier_invoice", vendor_name="Good Factory", invoice_number="INV-7", po_numbers=["PO0042"],
            amount_total=100.0, currency="USD")
    assert uploads.read_uploads(run, 10) == 1
    rows = odoo.rows("b2b.ocr.scraped.bill")
    assert list(rows) == [placeholder]  # the placeholder itself was filled in
    row = rows[placeholder]
    assert row["name"] == "INV-7" and row["external_key"] == "GOOD FACTORY|INV7" and row["partner_id"] == 7
    assert row["state"] == "po_found" and row["purchase_id"] == order


def test_an_invoice_already_there_is_merged_and_the_placeholder_goes(monkeypatch):
    odoo, run = setup()
    existing = odoo.add("b2b.ocr.scraped.bill", name="INV-7", external_key="GOOD FACTORY|INV7", company_id=1,
                        partner_id=7, state="ready_for_po", source="mail.pdf")
    placeholder = upload(odoo)
    reading(monkeypatch, "supplier_invoice", vendor_name="Good Factory", invoice_number="INV-7")
    uploads.read_uploads(run, 10)
    assert list(odoo.rows("b2b.ocr.scraped.bill")) == [existing]
    assert "scan.pdf" in odoo.rows("b2b.ocr.scraped.bill")[existing]["source"]
    assert [a["res_id"] for a in odoo.rows("ir.attachment").values()] == [existing]  # the file moved over
    assert placeholder not in odoo.rows("b2b.ocr.scraped.bill")


def test_what_is_not_an_invoice_or_has_no_number_is_parked_not_read_again(monkeypatch):
    odoo, run = setup()
    first, second = upload(odoo, "a.pdf"), upload(odoo, "b.pdf")
    reading(monkeypatch, "not_invoice")
    uploads.read_uploads(run, 10)
    assert odoo.rows("b2b.ocr.scraped.bill")[first]["state"] == "needs_review"
    assert odoo.rows("b2b.ocr.scraped.bill")[first]["match_notes"] == "Not an invoice."

    odoo.rows("b2b.ocr.scraped.bill")[second]["state"] = "to_read"
    reading(monkeypatch, "supplier_invoice", vendor_name="Good Factory")
    uploads.read_uploads(run, 10)
    row = odoo.rows("b2b.ocr.scraped.bill")[second]
    assert row["state"] == "needs_review" and row["match_notes"] == "No invoice number printed."
    assert uploads.read_uploads(run, 10) == 0


def test_a_failure_that_may_pass_leaves_it_to_read(monkeypatch):
    odoo, run = setup()
    bill = upload(odoo)

    def broken(raw, purpose, settings):
        raise RuntimeError("OCR programs are not installed")
    monkeypatch.setattr(extract, "read_file", broken)
    uploads.read_uploads(run, 10)
    assert odoo.rows("b2b.ocr.scraped.bill")[bill]["state"] == "to_read"
