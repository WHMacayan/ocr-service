import json

from app.freight_booking import compare as booking_compare
from app.shared.web import odoo_fields

from fakes import mailbox, signed_in


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
    monkeypatch.setattr("app.shared.odoo.client.Odoo.from_store", lambda db: Odoo())
    odoo_fields._fields_cache.clear()
    signed_in(client)

    default = client.get("/bookings").get_data(as_text=True)                 # opens grouped on the Odoo state, folded
    assert '<span class="clip">Conflict Detected</span><span class="n">1</span>' in default and "BK123" not in default
    assert 'aria-current="true">Odoo state</a>' in default
    listing = client.get("/bookings?groupby=none").get_data(as_text=True)
    assert "BK123" in listing and "1 differs" in listing and 'data-group=' not in listing
    grouped = client.get("/bookings?groupby=check").get_data(as_text=True)
    assert '<span class="clip">Differs</span><span class="n">1</span>' in grouped and 'aria-current="true">Check</a>' in grouped
    assert "BK123" not in grouped                                            # folded until opened
    assert "BK123" in client.get("/bookings?groupby=check&group=Differs").get_data(as_text=True)
    by_state = client.get("/bookings?groupby=state&group=Conflict+Detected").get_data(as_text=True)
    assert '<span class="clip">Conflict Detected</span><span class="n">1</span>' in by_state and "BK123" in by_state

    page = client.get("/bookings/41").get_data(as_text=True)
    assert '<a class="o-crumb" href="/bookings">Bookings</a>' in page and "<h1>BK123</h1>" in page   # the breadcrumb
    assert "1 field in Odoo differs" in page
    assert 'class="differs"' in page and "EVER GIVEN" in page and "MSC ANNA" in page
    assert "Sentury Morocco" in page and "from bc.pdf</a>" in page
    assert "MSCU1234567" in page and 'href="/messages/1">bc.pdf' in page
    assert "Conflict Detected" in page


def test_a_booking_page_lists_its_whole_conversation(client, db, monkeypatch):
    """The Emails tab: every email of the booking's conversation, oldest first, each with what it changed on the booking
    and what became of its attachments; one still waiting on a person has Process again beside it."""
    box = mailbox(db)
    with db:
        db.executemany("INSERT INTO message (id, mailbox_id, dedupe_key, subject, sender, received, state, note, "
                       "conversation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'conv')",
                       [(1, box["id"], "a", "BKG BK123", "ops@fwd.com", "2026-11-05 08:00:00", "done", None),
                        (2, box["id"], "b", "RE: BKG BK123", "me@fwd.com", "2026-11-06 08:00:00", "review", "No booking has this B/L yet.")])
        db.execute("INSERT INTO file (id, message_id, filename, sha256, path, state, doc_class) "
                   "VALUES (5, 1, 'bc.pdf', 's', '/x', 'done', 'booking_confirmation')")
        db.execute("INSERT INTO file (id, message_id, filename, sha256, path, state, doc_class, note) "
                   "VALUES (6, 2, 'late.pdf', 't', '/x', 'waiting', 'mbl_hbl', 'No booking has this B/L yet.')")
        db.execute("INSERT INTO odoo_change (message_id, file_id, action, model, record_id, values_json) "
                   "VALUES (1, 5, 'created', 'b2b.ocr.booking', 41, '{\"booking_number\": \"BK123\"}')")
    monkeypatch.setattr("app.shared.odoo.client.Odoo.from_store", lambda db: Odoo())
    odoo_fields._fields_cache.clear()
    signed_in(client)
    page = client.get("/bookings/41").get_data(as_text=True)
    tab = page[page.index('id="panel-emails"'):]
    assert tab.index("ops@fwd.com") < tab.index("me@fwd.com")                      # oldest first, the reply included
    assert "Created this record" in tab and "bc.pdf" in tab                        # what the first email and its file did
    assert "Waiting for its record" in tab and "late.pdf" in tab                   # the reply's file has no booking yet
    assert 'action="/messages/2/retry"' in tab and 'action="/messages/1/retry"' not in tab   # Process again where needed
