"""Every Odoo write is logged, and kept against the email that caused it."""


from fakes import mailbox, signed_in


def test_odoo_writes_are_logged_with_a_link(db):
    from app.odoo.client import Odoo

    class Server:
        def execute_kw(self, *args):
            return 41 if args[4] == "create" else True

    odoo = Odoo.__new__(Odoo)
    odoo.url, odoo.db, odoo.uid, odoo.api_key, odoo.activity_db, odoo.source = "http://odoo", "1025", 7, "k", db, None
    odoo._object = Server()
    odoo.create("b2b.ocr.booking", {"booking_number": "BK123", "vessel": "MAERSK X"})
    odoo.write("b2b.ocr.booking", [41], {"eta": "2026-12-01"})
    odoo.search_read("b2b.ocr.booking", [], ["id"])

    rows = [dict(r) for r in db.execute("SELECT summary, detail, link FROM activity WHERE kind = 'odoo' ORDER BY id")]
    assert [r["summary"] for r in rows] == ["Created booking #41 (BK123)", "Updated booking #41"]
    assert rows[0]["link"] == "http://odoo/web#model=b2b.ocr.booking&id=41&view_type=form"
    assert "eta: 2026-12-01" in rows[1]["detail"]


def test_odoo_writes_while_processing_an_email_are_recorded_against_it(client, db):
    from app.odoo.client import Odoo

    class Server:
        def execute_kw(self, *args):
            if args[4] == "read":
                return [{"id": 41, "display_name": "BK123"}]
            return 41 if args[4] == "create" else True

    box = mailbox(db)
    with db:
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key, subject, state) VALUES "
                   "(1, ?, 'a', 'BKG BK123', 'done'), (2, ?, 'b', 'Newsletter', 'done')", (box["id"],) * 2)
        db.execute("UPDATE message SET processed_at = CURRENT_TIMESTAMP WHERE id = 2")
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key, subject, state) VALUES (3, ?, 'c', 'Old', 'done')", (box["id"],))
        db.execute("INSERT INTO file (id, message_id, filename, sha256, path) VALUES (5, 1, 'bc.pdf', 's', '/x')")
    odoo = Odoo.__new__(Odoo)
    odoo.url, odoo.db, odoo.uid, odoo.api_key, odoo.activity_db, odoo.source = "http://odoo", "d", 7, "k", db, None
    odoo._object = Server()
    with odoo.working_on(1, 5):
        odoo.create("b2b.ocr.booking", {"booking_number": "BK123"})
    with odoo.working_on(1):
        odoo.write("b2b.ocr.booking", [41], {"eta": "2026-12-01"})
        odoo.create("b2b.ocr.booking.container", {"booking_id": 41, "container_no": "MSCU1234567"})
    odoo.write("b2b.ocr.booking", [41], {"vessel": "X"})  # outside any email: activity log only
    db.commit()

    rows = [tuple(r) for r in db.execute("SELECT message_id, file_id, action, model, label FROM odoo_change ORDER BY id")]
    assert rows == [(1, 5, "created", "b2b.ocr.booking", "BK123"), (1, None, "updated", "b2b.ocr.booking", "BK123"),
                    (1, None, "created", "b2b.ocr.booking.container", "MSCU1234567")]

    signed_in(client)
    listing = client.get("/messages").get_data(as_text=True)
    assert "<b>Booking</b> BK123 <span>created</span>" in listing and "+1 line change" in listing
    assert "Nothing written" in listing  # the newsletter, processed with tracking
    assert "Not recorded" in listing  # processed before tracking
    detail = client.get("/messages/1").get_data(as_text=True)
    assert "http://odoo/web#model=b2b.ocr.booking&amp;id=41&amp;view_type=form" in detail and "bc.pdf" in detail
    assert "Invoice" not in detail
    assert "BKG BK123" in client.get("/messages?q=MSCU1234567").get_data(as_text=True)
