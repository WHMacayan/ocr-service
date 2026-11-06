import json

from app import bookings

from fakes import mailbox
from test_store_web import client, signed_in  # noqa: F401  (fixtures)


class Odoo:
    """Odoo as the booking page reads it: one booking, a person changed its vessel."""

    def call(self, model, method, *args, **kwargs):
        if method == "fields_get":
            return {"booking_number": {"string": "Booking Number", "type": "char"},
                    "vessel": {"string": "Vessel", "type": "char"}, "factory_id": {"string": "Factory", "type": "many2one"},
                    "etd": {"string": "ETD", "type": "date"}, "state": {"string": "State", "type": "selection",
                                                                     "selection": [["conflict", "Conflict Detected"]]},
                    "container_no": {"string": "Container No", "type": "char"}}
        if method == "read":
            return [{"id": 41, "booking_number": "BK123", "vessel": "MSC ANNA", "factory_id": [7, "Sentury Morocco"],
                     "etd": "2026-10-18", "state": "conflict", "vessel_source": "bc.pdf"}]
        if method == "search_read" and model == "b2b.ocr.booking.container":
            return [{"id": 9, "container_no": "MSCU1234567"}]
        if method == "search_read" and model == "b2b.ocr.booking":
            return [{"id": 41, "booking_number": "BK123", "vessel": "MSC ANNA", "factory_id": [7, "Sentury Morocco"],
                     "etd": "2026-10-18", "state": "conflict"}]
        return []


def test_booking_record_sets_what_the_service_wrote_against_odoo(client, db, monkeypatch):
    box = mailbox(db)
    with db:
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key, subject, state) VALUES (1, ?, 'a', 'BKG BK123', 'done')",
                   (box["id"],))
        db.execute("INSERT INTO file (id, message_id, filename, sha256, path) VALUES (5, 1, 'bc.pdf', 's', '/x')")
        for action, model, record, values in (
                ("created", "b2b.ocr.booking", 41, {"booking_number": "BK123"}),
                ("updated", "b2b.ocr.booking", 41, {"vessel": "EVER GIVEN", "factory_id": 7, "etd": "2026-10-18"}),
                ("created", "b2b.ocr.booking.container", 9, {"booking_id": 41, "container_no": "MSCU1234567"})):
            db.execute("INSERT INTO odoo_change (message_id, file_id, action, model, record_id, values_json) "
                       "VALUES (1, 5, ?, ?, ?, ?)", (action, model, record, json.dumps(values)))
    monkeypatch.setattr("app.web.Odoo.from_store", lambda db: Odoo())
    bookings._fields_cache.clear()
    signed_in(client)

    listing = client.get("/bookings").get_data(as_text=True)
    assert "BK123" in listing and "1 differs" in listing

    page = client.get("/bookings/41").get_data(as_text=True)
    assert "1 field in Odoo differs" in page
    assert 'class="differs"' in page and "EVER GIVEN" in page and "MSC ANNA" in page
    assert "Sentury Morocco" in page and "from bc.pdf</a>" in page
    assert "MSCU1234567" in page and 'href="/messages/1">bc.pdf' in page
    assert "Conflict Detected" in page
