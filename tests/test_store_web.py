import re

import pytest

from app import create_app, store

from fakes import mailbox


def test_defaults_seeded_and_secrets_encrypted(db, tmp_path):
    assert store.get_setting(db, "max_retries", int) == 5
    assert store.get_setting(db, "poll_seconds", int) == 60
    assert store.get_setting(db, "openai_api_key") is None

    store.set_setting(db, "openai_api_key", "sk-test")
    raw = db.execute("SELECT value FROM setting WHERE key = 'openai_api_key'").fetchone()[0]
    assert raw != "sk-test"
    assert store.get_setting(db, "openai_api_key") == "sk-test"
    assert (tmp_path / "secret.key").exists()


def test_a_changed_setting_survives_a_restart(db, tmp_path):
    store.set_setting(db, "max_retries", 9)
    again = store.connect()
    assert store.get_setting(again, "max_retries", int) == 9
    again.close()


@pytest.fixture
def client(db):
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def csrf(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


def signed_in(client):
    token = csrf(client, "/setup")
    client.post("/setup", data={"csrf": token, "password": "long-password", "confirm": "long-password"})
    return csrf(client, "/settings")  # signing in issues a new form token


def test_first_start_asks_for_a_password_then_requires_login(client):
    assert client.get("/messages").headers["Location"].endswith("/setup")
    signed_in(client)
    assert client.get("/messages").status_code == 200

    client.post("/logout", data={"csrf": csrf(client, "/messages")})
    assert client.get("/messages").headers["Location"].endswith("/login")
    token = csrf(client, "/login")
    assert "Wrong password" in client.post("/login", data={"csrf": token, "password": "nope"},
                                           follow_redirects=True).get_data(as_text=True)
    client.post("/login", data={"csrf": token, "password": "long-password"})
    assert client.get("/messages").status_code == 200


def test_post_without_csrf_token_is_refused(client, db):
    signed_in(client)
    page = client.post("/settings", data={"max_retries": "3"}, follow_redirects=True).get_data(as_text=True)
    assert "nothing was saved" in page and "Settings" in page
    assert store.get_setting(db, "max_retries", int) == 5


def test_an_expired_action_form_goes_back_to_the_page_it_came_from(client):
    signed_in(client)
    resp = client.post("/logout", data={"csrf": "old"}, headers={"Referer": "http://localhost/messages"})
    assert resp.headers["Location"] == "/messages"
    resp = client.post("/logout", data={"csrf": "old"}, headers={"Referer": "https://elsewhere.example/x"})
    assert resp.headers["Location"] == "/"


def test_times_are_marked_for_the_viewers_time_zone(client):
    from app.web import when
    assert when("2026-11-06 01:54:48") == ('<time datetime="2026-11-06T01:54:48Z" title="2026-11-06 01:54:48 UTC">'
                                          '2026-11-06 01:54:48 UTC</time>')
    assert when("2026-11-06T09:54:48+08:00").startswith('<time datetime="2026-11-06T01:54:48Z"')
    assert when(None) == "" and when("not a time") == "not a time"
    signed_in(client)
    page = client.get("/activity").get_data(as_text=True)
    assert '<time datetime="' in page and "(UTC)" not in page


def test_background_requests_keep_the_login_form_working(client):
    signed_in(client)
    client.post("/logout", data={"csrf": csrf(client, "/messages")})
    token = csrf(client, "/login")
    client.get("/favicon.ico")  # browsers ask for this after the page loads
    client.post("/login", data={"csrf": token, "password": "long-password"})
    assert client.get("/messages").status_code == 200


def test_settings_page_saves_and_keeps_an_empty_secret(client, db):
    token = signed_in(client)
    form = {"csrf": token, "openai_api_key": "sk-1", "max_retries": "3", "openai_model": "gpt-4o-mini"}
    client.post("/settings", data=form)
    client.post("/settings", data=dict(form, openai_api_key=""))

    assert store.get_setting(db, "openai_api_key") == "sk-1"
    assert store.get_setting(db, "max_retries", int) == 3


def test_saved_secret_shows_its_last_characters_never_the_value(client, db):
    token = signed_in(client)
    client.post("/settings", data={"csrf": token, "openai_api_key": "sk-secret-value-wxyz"})

    page = client.get("/settings").get_data(as_text=True)
    assert "Saved, ends in <code>wxyz</code>" in page
    assert "sk-secret-value" not in page


def test_mailbox_saved_with_encrypted_client_secret(client, db):
    token = signed_in(client)
    client.post("/mailboxes/new", data={"csrf": token, "name": "Bookings", "active": "on",
                                        "purpose": "freight_booking", "company_id": "1",
                                        "client_secret": "azure-secret", "start_from": "2026-11-02"})

    box = store.mailbox(db, 1)
    assert box["client_secret"] == "azure-secret"
    assert box["start_from"] == "2026-11-02 00:00:00"
    raw = db.execute("SELECT client_secret FROM mailbox").fetchone()[0]
    assert raw != "azure-secret"


def test_subject_format_regex_is_checked(client, db):
    token = signed_in(client)
    page = client.post("/subject-formats/new", follow_redirects=True, data={
        "csrf": token, "name": "Maersk", "subject_pattern": r"BKG (?P<booking>\d+)"}).get_data(as_text=True)
    assert "Unknown group(s) booking" in page
    client.post("/subject-formats/new", data={
        "csrf": token, "name": "Maersk", "subject_pattern": r"BKG (?P<booking_number>\d+)"})
    assert db.execute("SELECT name FROM subject_format").fetchone()[0] == "Maersk"


def test_odoo_lists_load_sorted_by_name_and_long_ones_are_searchable(client, monkeypatch):
    class FakeOdoo:
        def search_read(self, model, domain, fields, order):
            assert order == "name", "Odoo cannot sort on display_name"
            count = 40 if model == "res.partner" else 2
            return [{"id": n, "display_name": f"{model} {n}"} for n in range(1, count + 1)]

    monkeypatch.setattr("app.web.Odoo.from_store", lambda db: FakeOdoo())
    signed_in(client)
    page = client.get("/vendor-mappings/new").get_data(as_text=True)

    assert 'data-combo="partner_id"' in page
    assert '<select id="freight_product_id"' in page
    assert "could not be loaded" not in page


def test_retry_puts_the_email_back_and_keeps_the_extraction(client, db):
    token = signed_in(client)
    box = mailbox(db)
    with db:
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key, state, attempts, note) "
                   "VALUES (1, ?, 'k', 'failed', 5, 'AI down')", (box["id"],))
        db.execute("INSERT INTO file (message_id, sha256, path, state, extracted) "
                   "VALUES (1, 'abc', '/x', 'failed', '{}')")
    client.post("/messages/1/retry", data={"csrf": token})

    message = db.execute("SELECT state, attempts, note FROM message").fetchone()
    assert tuple(message) == ("new", 0, None)
    assert tuple(db.execute("SELECT state, extracted FROM file").fetchone()) == ("new", "{}")


def test_connect_outlook_sends_to_microsoft_with_a_state(client, db):
    token = signed_in(client)
    box = mailbox(db)
    resp = client.post(f"/mailboxes/{box['id']}/connect", data={"csrf": token})
    assert resp.headers["Location"].startswith("https://login.microsoftonline.com/t/oauth2/v2.0/authorize?")
    assert "state=" in resp.headers["Location"]
    assert client.get("/oauth/callback?state=wrong&code=x").status_code == 400


def test_pages_send_security_headers_and_run_no_inline_script(client):
    signed_in(client)
    resp = client.get("/settings")
    policy = resp.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy and "frame-ancestors 'none'" in policy
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Cache-Control"] == "no-store"
    assert "<script>" not in resp.get_data(as_text=True)


def test_login_locks_out_after_five_wrong_passwords(client):
    signed_in(client)
    client.post("/logout", data={"csrf": csrf(client, "/messages")})
    token = csrf(client, "/login")
    for _ in range(5):
        assert client.post("/login", data={"csrf": token, "password": "nope"}).status_code == 200
    locked = client.post("/login", data={"csrf": token, "password": "long-password"})
    assert locked.status_code == 429
    assert client.get("/messages").headers["Location"].endswith("/login")


def test_changing_the_password_signs_out_other_browsers(client, db):
    from app import create_app
    other = create_app().test_client()
    signed_in(client)
    token = csrf(other, "/login")
    other.post("/login", data={"csrf": token, "password": "long-password"})
    assert other.get("/messages").status_code == 200

    token = csrf(client, "/password")
    client.post("/password", data={"csrf": token, "current": "long-password",
                                   "password": "another-password", "confirm": "another-password"})

    assert client.get("/messages").status_code == 200
    assert other.get("/messages").headers["Location"].endswith("/login")


def test_every_field_explains_itself_in_a_tooltip(client):
    from app.web import FORMS, SETTING_FIELDS
    assert all(f.tip for form in FORMS.values() for f in form.fields)
    assert all(f.tip for f in SETTING_FIELDS)
    signed_in(client)
    page = client.get("/mailboxes/new").get_data(as_text=True)
    assert 'role="tooltip" id="tip-group_address"' in page


def test_activity_records_settings_security_and_hides_secrets(client, db):
    token = signed_in(client)
    client.post("/settings", data={"csrf": token, "openai_model": "gpt-4o", "openai_api_key": "super-secret-key"})
    client.post("/connections/new", data={"csrf": token, "name": "Staging", "login": "admin",
                                          "api_key": "odoo-secret-key"})
    client.post("/subject-formats/new", data={"csrf": token, "name": "Maersk",
                                              "subject_pattern": r"BKG (?P<booking_number>\d+)"})
    rows = [dict(r) for r in db.execute("SELECT kind, summary, detail, actor FROM activity ORDER BY id")]

    assert rows[0]["kind"] == "security" and rows[0]["summary"] == "Admin password set on first start"
    settings = next(r for r in rows if r["summary"].startswith("Changed settings"))
    assert "Model: gpt-4o-mini → gpt-4o" in settings["detail"] and "OpenAI API key: changed" in settings["detail"]
    created = next(r for r in rows if r["summary"] == "Created Odoo connection Staging")
    assert "Login: (empty) → admin" in created["detail"] and "API key: changed" in created["detail"]
    assert "super-secret-key" not in str(rows) and "odoo-secret-key" not in str(rows)
    assert "super-secret-key" not in str(rows)
    assert any(r["summary"] == "Created subject format Maersk" for r in rows)
    assert rows[-1]["actor"].startswith("admin from")

    page = client.get("/activity?kind=settings").get_data(as_text=True)
    assert "Created subject format Maersk" in page and "Admin password set" not in page


def test_odoo_writes_are_logged_with_a_link(db):
    from app.odoo import Odoo

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


def test_an_open_email_shows_beside_its_filtered_list(client, db):
    token = signed_in(client)
    box = mailbox(db)
    with db:
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key, subject, state, received) VALUES "
                   "(1, ?, 'a', 'BKG 1', 'failed', '2026-11-05 10:00:00'), (2, ?, 'b', 'BKG 2', 'done', '2026-11-05 11:00:00')",
                   (box["id"],) * 2)
    page = client.get("/messages/1?state=failed").get_data(as_text=True)
    assert 'href="/messages/1?state=failed" aria-current="true"' in page  # the list keeps the filter
    assert "BKG 2" not in page.split('id="detail"')[0]                     # filtered out of the list
    assert 'data-list-key="emails failed "' in page and 'id="email-detail"' in page
    resp = client.post("/messages/1/retry?state=failed", data={"csrf": token})
    assert resp.headers["Location"] == "/messages/1?state=failed"


def test_the_email_list_hands_out_the_next_page(client, db, monkeypatch):
    signed_in(client)
    monkeypatch.setattr("app.web.EMAILS_PAGE", 2)
    box = mailbox(db)
    with db:
        for n in range(5):
            db.execute("INSERT INTO message (mailbox_id, dedupe_key, subject, received) VALUES (?, ?, ?, ?)",
                       (box["id"], f"k{n}", f"Mail {n}", f"2026-11-0{n + 1} 08:00:00"))
    first = client.get("/messages").get_data(as_text=True)
    assert "Mail 4" in first and "Mail 3" in first and "Mail 2" not in first
    assert 'data-more="/messages?offset=2"' in first
    second = client.get("/messages?offset=2").get_data(as_text=True)
    assert "Mail 2" in second and "Mail 1" in second and "Mail 4" not in second.split('id="detail"')[0]


def test_setup_pages_list_their_records_beside_the_form(client, db):
    signed_in(client)
    box = mailbox(db, name="Customs Fetching")
    mailbox(db, name="Bills")
    page = client.get(f"/mailboxes/{box['id']}").get_data(as_text=True)
    assert 'data-list-key="mailboxes"' in page and "Bills" in page
    assert f'href="/mailboxes/{box["id"]}" aria-current="true"' in page
    assert 'id="record-form"' in page
    runs = client.get(f"/mailboxes/{box['id']}/runs").get_data(as_text=True)
    assert 'data-list-key="mailboxes"' in runs and "Run history" in runs


def test_clear_starts_a_mailbox_over_and_keeps_its_settings(client, db, tmp_path):
    token = signed_in(client)
    box = mailbox(db, folder="Inbox", fetch_resume_from="2026-10-19 12:00:41", status="success",
                  last_sync="2026-11-03 05:54:47")
    other = mailbox(db, name="Bills")
    saved = tmp_path / "files" / "ab" / "abc"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"%PDF-1.4")
    with db:
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key) VALUES (1, ?, 'a'), (2, ?, 'b')",
                   (box["id"], other["id"]))
        db.execute("INSERT INTO file (message_id, sha256, path) VALUES (1, 'abc', ?)", (str(saved),))
        db.execute("INSERT INTO run (mailbox_id, status) VALUES (?, 'success')", (box["id"],))

    page = client.post(f"/mailboxes/{box['id']}/clear", data={"csrf": token}, follow_redirects=True)

    assert "Cleared: 1 emails and 1 files removed" in page.get_data(as_text=True)
    assert [r[0] for r in db.execute("SELECT mailbox_id FROM message")] == [other["id"]]
    assert not db.execute("SELECT COUNT(*) FROM file").fetchone()[0] and not saved.exists()
    assert not db.execute("SELECT COUNT(*) FROM run").fetchone()[0]
    after = store.mailbox(db, box["id"])
    assert after["fetch_resume_from"] is None and after["status"] == "new" and after["last_sync"] is None
    assert after["folder"] == "Inbox" and after["refresh_token"] == "r"
    assert db.execute("SELECT summary FROM activity WHERE kind = 'processing'").fetchone()[0] == \
        "Cleared mailbox Bookings to start over"


def test_duplicate_copies_the_settings_but_not_the_sign_in_or_reading_state(client, db):
    token = signed_in(client)
    box = mailbox(db, purpose="customs_entry", sender_domains="tlius.com", batch_size=100,
                  fetch_resume_from="2026-10-19 12:00:41", status="success", last_sync="2026-11-03 05:54:47")
    store.update_mailbox(db, box["id"], {"access_token": "a", "token_expires_at": "2026-11-04 06:00:00"})

    page = client.post(f"/mailboxes/{box['id']}/duplicate", data={"csrf": token})

    copy = store.mailbox(db, box["id"] + 1)
    assert page.headers["Location"].endswith(f"/mailboxes/{copy['id']}")
    assert copy["name"] == "Bookings (copy)" and copy["active"] == 0
    assert (copy["purpose"], copy["sender_domains"], copy["batch_size"]) == ("customs_entry", "tlius.com", 100)
    assert (copy["tenant_id"], copy["client_id"], copy["client_secret"]) == ("t", "c", "s")
    assert copy["refresh_token"] is None and copy["access_token"] is None and copy["token_expires_at"] is None
    assert copy["fetch_resume_from"] is None and copy["status"] == "new" and copy["last_sync"] is None


def test_clear_keeps_a_file_another_mailbox_still_uses(client, db, tmp_path):
    token = signed_in(client)
    box = mailbox(db)
    other = mailbox(db, name="Bills", purpose="bill_matching")
    saved = tmp_path / "files" / "ab" / "abc"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"%PDF-1.4")
    with db:
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key) VALUES (1, ?, 'a'), (2, ?, 'b')",
                   (box["id"], other["id"]))
        db.execute("INSERT INTO file (message_id, sha256, path) VALUES (1, 'abc', ?), (2, 'bill_matching:abc', ?)",
                   (str(saved), str(saved)))

    client.post(f"/mailboxes/{box['id']}/clear", data={"csrf": token})

    assert saved.exists()
    assert [r[0] for r in db.execute("SELECT sha256 FROM file")] == ["bill_matching:abc"]


def test_odoo_writes_while_processing_an_email_are_recorded_against_it(client, db):
    from app.odoo import Odoo

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
