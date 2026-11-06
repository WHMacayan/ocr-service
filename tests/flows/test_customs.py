"""Customs entry summaries: which PDFs are uploaded to the customs audit module."""

import pytest

from app.flows import customs, pipeline
from app.core import store
from fakes import connection, email, odoo, row

FORM_7501 = ("DEPARTMENT OF HOMELAND SECURITY OMB CONTROL NUMBER 1651-0022\nU.S. Customs and Border Protection\n"
             "ENTRY SUMMARY\n1. Filer Code/Entry Number 2. Entry Type\n040-0496733-9 01 ABI/P 11/04/26\n"
             "CBP Form 7501 (5/22)")
FORM_3461 = "ENTRY/IMMEDIATE DELIVERY\n1. ARRIVAL DATE\n11. ENTRY NUMBER 040-0497200-1\nCBP Form 3461 (10/09)"
INVOICE = "Triumph Link (USA), Inc.\nInvoice\nBILL TO PARTY INVOICE NO. TLA-AR214477"


def test_entry_forms_are_recognised_and_other_pdfs_are_not():
    assert customs.form_of(FORM_7501) == "7501"
    assert customs.form_of(FORM_3461) == "3461"
    assert customs.form_of(INVOICE) is None
    assert customs.form_of("COMMERCIAL INVOICE quoting entry summary 7501 fees") is None


def test_entry_number_is_read_from_the_form():
    assert customs.entry_number(FORM_7501) == "040-0496733-9"
    assert customs.entry_number(FORM_3461) == "040-0497200-1"


@pytest.fixture
def texts(monkeypatch):
    """Stand-in for reading a PDF's text: every file reads as ``texts.current``."""
    class Texts:
        current = ""
    monkeypatch.setattr(customs, "file_text", lambda raw, max_pages: Texts.current)
    return Texts


def customs_email(db, tmp_path, received="2026-10-28 21:40:55"):
    store.set_setting(db, "openai_api_key", "sk-test")
    return email(db, tmp_path, purpose="customs_entry", sender="customs@tlius.com", received=received)


def test_entry_summary_becomes_an_entry_in_the_days_batch(db, tmp_path, ai, texts):  # noqa: F811
    message_id = customs_email(db, tmp_path)
    texts.current = FORM_7501
    fake = odoo()
    pipeline.process(db, fake, connection(db))

    [(entry_id, entry)] = fake.rows("cbp7501.entry").items()
    [(batch_id, batch)] = fake.rows("cbp7501.audit.batch").items()
    assert batch["name"] == "Customs Audit 2026-10-28 (email)"
    assert entry["batch_id"] == batch_id and entry["file_name"] == "doc.pdf"
    assert entry["pdf_file"] == "JVBERi0xLjQgdGVzdA=="  # base64 of the saved PDF
    assert ai.calls == 0
    assert row(db, "message", message_id)["state"] == "done"
    file = db.execute("SELECT * FROM file WHERE message_id = ?", (message_id,)).fetchone()
    assert file["state"] == "done" and file["odoo_id"] == entry_id and "040-0496733-9" in file["extracted"]


def test_same_entry_number_again_and_other_pdfs_are_skipped(db, tmp_path, ai, texts):  # noqa: F811
    fake = odoo()
    texts.current = FORM_7501
    customs_email(db, tmp_path, received="2026-10-27 23:20:24")
    pipeline.process(db, fake, connection(db))
    again = customs_email(db, tmp_path)
    pipeline.process(db, fake, connection(db))
    texts.current = INVOICE
    invoice = customs_email(db, tmp_path, received="2026-10-29 08:00:00")
    pipeline.process(db, fake, connection(db))

    assert len(fake.rows("cbp7501.entry")) == 1 and len(fake.rows("cbp7501.audit.batch")) == 1
    note = lambda message_id: db.execute("SELECT state, note FROM file WHERE message_id = ?",  # noqa: E731
                                         (message_id,)).fetchone()
    assert tuple(note(again)) == ("skipped", "Entry 040-0496733-9 is already in the customs audit.")
    assert tuple(note(invoice)) == ("skipped", "Not a CBP 7501 or 3461.")


def test_sender_domain_filter_keeps_only_those_domains():
    from app.mail.ingest import filter_reason
    from fakes import message
    box = {"sender_domains": "tlius.com, example.org"}
    assert filter_reason(box, message(1, **{"from": {"emailAddress": {"address": "owen@TLIUS.com"}}})) is None
    assert filter_reason(box, message(2)) == "sender domain"
