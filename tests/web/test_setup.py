"""Mailboxes, vendor mappings and subject formats: their forms, lists and mailbox actions."""


from app.core import store
from fakes import mailbox, signed_in


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

    monkeypatch.setattr("app.odoo.client.Odoo.from_store", lambda db: FakeOdoo())
    signed_in(client)
    page = client.get("/vendor-mappings/new").get_data(as_text=True)

    assert 'data-combo="partner_id"' in page
    assert '<select id="freight_product_id"' in page
    assert "could not be loaded" not in page


def test_connect_outlook_sends_to_microsoft_with_a_state(client, db):
    token = signed_in(client)
    box = mailbox(db)
    resp = client.post(f"/mailboxes/{box['id']}/connect", data={"csrf": token})
    assert resp.headers["Location"].startswith("https://login.microsoftonline.com/t/oauth2/v2.0/authorize?")
    assert "state=" in resp.headers["Location"]
    assert client.get("/oauth/callback?state=wrong&code=x").status_code == 400


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


def test_setup_lists_group_by_a_choice(client, db):
    signed_in(client)
    with db:
        db.executemany("INSERT INTO vendor_mapping (role, partner_name) VALUES (?, ?)",
                       [("forwarder", "Maersk"), ("factory", "Sentury"), ("forwarder", "MSC")])
    page = client.get("/vendor-mappings/?groupby=role").get_data(as_text=True)
    assert page.index('<span class="clip">Factory</span><span class="n">1</span>') \
        < page.index('<span class="clip">Forwarder</span><span class="n">2</span>')
    assert 'href="/vendor-mappings/1?groupby=role"' in page
    assert ">Odoo partner</a>" in page and ">Freight mode</a>" in page   # what the menu offers
    assert 'data-group=' not in client.get("/vendor-mappings/").get_data(as_text=True)
