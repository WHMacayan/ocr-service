"""Only a commercial invoice becomes a scraped bill: freight and service invoices and arrival
notices are read but not taken (SBKKS0026907: an arrival notice made a second bill on its PO)."""

import pytest

from app.shared.documents import extract
from app.scraped_bills import reading

SETTINGS = {"max_pages": "5", "max_chars": "1000", "openai_api_key": "k", "openai_model": "m",
            "invoice_prompt": "p", "freight_prompt": "p"}


@pytest.mark.parametrize("document_type, doc_class", [
    ("commercial_invoice", "supplier_invoice"),
    ("freight_invoice", "not_invoice"),
    ("service_invoice", "not_invoice"),
    ("shipping_notice", "not_invoice"),
    ("other", "not_invoice"),
])
def test_only_commercial_invoices_are_invoices(monkeypatch, document_type, doc_class):
    monkeypatch.setattr(extract, "file_text", lambda raw, pages: "Invoice INV-1")
    monkeypatch.setattr(extract.ai, "extract", lambda *a: {"document_type": document_type, "invoice_number": "INV-1"})
    assert extract.read_file(b"%PDF", "bill_matching", SETTINGS)[0] == doc_class


def test_the_schema_asks_for_a_document_type():
    assert reading.INVOICE_SCHEMA["properties"]["document_type"]["enum"] == list(reading.INVOICE_TYPES)
    assert "is_invoice" not in reading.INVOICE_SCHEMA["properties"]


@pytest.mark.parametrize("values, note", [
    ({"document_type": "shipping_notice", "document_name": "arrival notice"}, "Not a commercial invoice: arrival notice"),
    ({"document_type": "other", "document_name": "customs entry summary (CBP 7501)."},
     "Not a commercial invoice: customs entry summary (CBP 7501)"),
    ({"document_type": "freight_invoice"}, "Not a commercial invoice: freight invoice"),
    ({"document_type": "other"}, "Not a commercial invoice: other document"),
    ({}, "Not a commercial invoice: unidentified document"),
    ({"document_type": "freight_invoice", "document_name": "Invoice"}, "Not a commercial invoice: freight invoice"),
    ({"document_type": "service_invoice", "document_name": "unknown"}, "Not a commercial invoice: service invoice"),
])
def test_the_note_says_what_the_file_actually_is(values, note):
    assert reading.not_invoice_note(values) == note
