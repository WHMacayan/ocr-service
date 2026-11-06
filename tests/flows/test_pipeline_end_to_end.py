"""The pipeline from the service's own database to Odoo, with the AI stood in for."""


from app.flows import pipeline
from app.core import store
from fakes import connection, email, FakeOdoo, odoo, row


def test_freight_email_becomes_a_booking(db, tmp_path, ai):
    store.set_setting(db, "openai_api_key", "sk-test")
    message_id = email(db, tmp_path)
    fake = odoo()
    pipeline.process(db, fake, connection(db))
    bookings = list(fake.rows("b2b.ocr.booking").items())
    assert len(bookings) == 1 and bookings[0][1]["vessel"] == "EVER GIVEN"
    message = row(db, "message", message_id)
    assert message["state"] == "done" and message["email_done"] == 1 and message["body_text"] == "See attached."
    file = db.execute("SELECT * FROM file WHERE message_id = ?", (message_id,)).fetchone()
    assert file["state"] == "done" and file["odoo_id"] == bookings[0][0]


def test_retry_reuses_the_ai_reading_and_adds_nothing(db, tmp_path, ai):
    store.set_setting(db, "openai_api_key", "sk-test")
    message_id = email(db, tmp_path)
    fake = odoo()
    pipeline.process(db, fake, connection(db))
    with db:  # the email page's Process again button
        db.execute("UPDATE message_target SET state = 'new', attempts = 0, note = NULL WHERE message_id = ?",
                   (message_id,))
        db.execute("UPDATE file_target SET state = 'new' WHERE file_id IN (SELECT id FROM file WHERE message_id = ?)",
                   (message_id,))
    pipeline.process(db, fake, connection(db))
    assert ai.calls == 1
    assert len(fake.rows("b2b.ocr.booking")) == 1 and not fake.rows("b2b.ocr.booking.conflict")


def test_odoo_failure_keeps_the_email_and_stops_after_max_retries(db, tmp_path, ai):
    store.set_setting(db, "openai_api_key", "sk-test")
    store.set_setting(db, "max_retries", "2")
    message_id = email(db, tmp_path)

    class Down(FakeOdoo):
        def create(self, model, values):
            raise ConnectionError("Odoo is down")
    pipeline.process(db, Down(), connection(db))
    message = row(db, "message", message_id)
    assert message["state"] == "new" and message["attempts"] == 1 and "Odoo is down" in message["note"]
    file = db.execute("SELECT * FROM file WHERE message_id = ?", (message_id,)).fetchone()
    assert file["extracted"]  # the AI reading survives the failure
    pipeline.process(db, Down(), connection(db))
    assert row(db, "message", message_id)["state"] == "failed"
    assert ai.calls == 1


def test_document_without_booking_waits_for_review(db, tmp_path, ai):
    store.set_setting(db, "openai_api_key", "sk-test")
    ai.answer = ("commercial_invoice", {"doc_class": "commercial_invoice", "booking_number": "ZZ999999"})
    message_id = email(db, tmp_path, subject="Invoice")
    pipeline.process(db, odoo(), connection(db))
    message = row(db, "message", message_id)
    assert message["state"] == "review" and "No booking" in message["note"]


def test_waiting_emails_do_not_block_new_ones(db, tmp_path, ai):
    store.set_setting(db, "openai_api_key", "sk-test")
    store.set_setting(db, "documents_per_run", "1")
    email(db, tmp_path, received="2026-09-01 08:00:00", state="review")
    fresh = email(db, tmp_path)
    pipeline.process(db, odoo(), connection(db))
    assert row(db, "message", fresh)["state"] == "done"


def test_supplier_invoice_becomes_a_scraped_bill(db, tmp_path, ai):
    store.set_setting(db, "openai_api_key", "sk-test")
    ai.answer = ("supplier_invoice", {"is_invoice": True, "invoice_number": "INV-001", "vendor_name": "Good Factory"})
    message_id = email(db, tmp_path, purpose="bill_matching", sender="ar@factory.cn")
    fake = odoo()
    pipeline.process(db, fake, connection(db))
    bills = list(fake.rows("b2b.ocr.scraped.bill").values())
    assert len(bills) == 1 and bills[0]["external_key"] == "GOOD FACTORY|INV001"
    assert bills[0]["waiting_reason"] == "vendor_not_found"
    assert row(db, "message", message_id)["state"] == "done"


def test_nothing_is_read_without_an_openai_key(db, tmp_path, ai):
    message_id = email(db, tmp_path)
    pipeline.process(db, odoo(), connection(db))
    assert ai.calls == 0 and row(db, "message", message_id)["state"] == "new"
