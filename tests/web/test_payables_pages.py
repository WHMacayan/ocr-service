"""The Freight Payables page: its stored mail, a file as it came, and its settings."""

from fakes import mailbox, signed_in


def stored(db, tmp_path):
    """A payables mailbox with one email and its file, as the payables reader keeps them."""
    box = mailbox(db, name="AP mail", purpose="freight_payables")
    path = tmp_path / "f1"
    path.write_bytes(b"%PDF-1.4 invoice")
    with db:
        db.execute("INSERT INTO payables_message (id, mailbox_id, dedupe_key, conversation_id, subject, sender, "
                   "sender_domain, received, unique_body_html) VALUES (1, ?, 'k1', 'c1', 'Freight invoice 277046688', "
                   "'ar@carrier.com', 'carrier.com', '2026-11-03 08:00:00', '<p>Please see the invoice.</p>')", (box["id"],))
        db.execute("INSERT INTO payables_message (id, mailbox_id, dedupe_key, conversation_id, subject, sender, "
                   "sender_domain, received) VALUES (2, ?, 'k2', 'c1', 'RE: Freight invoice 277046688', 'me@atv.com', "
                   "'atv.com', '2026-11-04 08:00:00')", (box["id"],))
        db.execute("INSERT INTO payables_file (id, sha256, path, kind, size) VALUES (7, 'abc', ?, 'pdf', 16)", (str(path),))
        db.execute("INSERT INTO payables_message_file (message_id, file_id, filename) VALUES (1, 7, '277046688.pdf')")
    return box


def test_the_menu_leads_to_the_stored_mail_grouped_as_asked(client, db, tmp_path):
    signed_in(client)
    stored(db, tmp_path)
    page = client.get("/payables/emails?groupby=none").get_data(as_text=True)
    assert 'href="/payables/emails"' not in page.split("<main")[0]  # parked: not in the menu, the page still opens
    assert "Freight invoice 277046688" in page and "ar@carrier.com" in page
    assert 'aria-current="true">' in page and 'Emails <span class="n">2</span>' in page
    assert "RE: Freight" not in client.get("/payables/emails?q=277046688.pdf&groupby=none").get_data(as_text=True)  # by file name
    grouped = client.get("/payables/emails?groupby=sender").get_data(as_text=True)
    assert '<span class="clip">carrier.com</span><span class="n">1</span>' in grouped and "Freight invoice" not in grouped


def test_an_email_shows_its_text_files_and_conversation(client, db, tmp_path):
    signed_in(client)
    stored(db, tmp_path)
    page = client.get("/payables/emails/1").get_data(as_text=True)
    assert "Please see the invoice." in page and "277046688.pdf" in page and "RE: Freight invoice" in page
    download = client.get("/payables/emails/1/files/7")
    assert download.data == b"%PDF-1.4 invoice" and "277046688.pdf" in download.headers["Content-Disposition"]
    assert client.get("/payables/emails/2/files/7").status_code == 404           # only on the email it came on


def test_settings_keep_an_export_folder_inside_the_shared_one(client, db, tmp_path, monkeypatch):
    monkeypatch.setenv("PAYABLES_EXPORT_HOST", r"C:\Users\me\OneDrive - ATV INC")
    monkeypatch.setenv("PAYABLES_EXPORT_MOUNT", str(tmp_path / "shared"))
    (tmp_path / "shared" / "OCR AP").mkdir(parents=True)
    token = signed_in(client)
    box = stored(db, tmp_path)
    page = client.get("/payables/settings").get_data(as_text=True)
    assert "AP mail" in page and "<span>OneDrive - ATV INC \\</span>" in page            # short, after the folder's name
    assert 'value="OCR AP\\Exports"' in page and '<option value="OCR AP">' in page      # pre-filled; folders suggested
    assert "docker" not in page.lower() and "PAYABLES_EXPORT_HOST" not in page

    refused = client.post("/payables/settings", follow_redirects=True, data={
        "csrf": token, "mailbox_id": box["id"], "invoices_enabled": "on", "export_folder": r"C:\Users\me\Desktop"})
    assert "Not saved: Choose a folder inside OneDrive - ATV INC." in refused.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) FROM payables_setting").fetchone()[0] == 0

    client.post("/payables/settings", data={"csrf": token, "mailbox_id": box["id"], "invoices_enabled": "on",
                                            "export_folder": r"C:\Users\me\OneDrive - ATV INC\OCR AP\Exports"})
    row = db.execute("SELECT invoices_enabled, audit_enabled, export_folder FROM payables_setting").fetchone()
    assert tuple(row) == (1, 0, r"OCR AP\Exports")                                     # kept short
    assert (tmp_path / "shared" / "OCR AP" / "Exports").is_dir()
    saved = client.get("/payables/settings").get_data(as_text=True)
    assert r"Saved in C:\Users\me\OneDrive - ATV INC\OCR AP\Exports" in saved
    logged = db.execute("SELECT detail FROM activity WHERE summary = 'Updated Freight Payables settings of AP mail'").fetchone()
    assert "Audit: on → off" in logged[0]
    assert r"Export folder: (none) → C:\Users\me\OneDrive - ATV INC\OCR AP\Exports" in logged[0]


def test_the_automatic_export_is_off_for_a_new_mailbox_and_set_like_odoos_scheduled_actions(client, db, tmp_path):
    token = signed_in(client)
    box = stored(db, tmp_path)
    page = client.get("/payables/settings").get_data(as_text=True)
    assert 'name="export_auto" >' in page or 'name="export_auto">' in page            # off: not ticked
    for unit in ("Minutes", "Hours", "Days", "Weeks", "Months", "Once only"):
        assert f">{unit}</option>" in page
    assert "nothing runs yet" in page and "No export yet." in page

    client.post("/payables/settings", data={"csrf": token, "mailbox_id": box["id"], "export_auto": "on",
                                            "export_every": "1", "export_unit": "days", "export_next": "2026-11-10 07:00"})
    row = db.execute("SELECT export_auto, export_every, export_unit, export_next FROM payables_setting").fetchone()
    assert tuple(row) == (1, 1, "days", "2026-11-10 07:00:00")
    page = client.get("/payables/settings").get_data(as_text=True)
    assert 'data-utc="2026-11-10T07:00:00Z"' in page and 'name="export_auto" checked' in page
    logged = db.execute("SELECT detail FROM activity WHERE summary LIKE 'Updated Freight Payables settings%'").fetchone()[0]
    assert "Automatic export: off → on" in logged and "Repeat: Every 1 Days" not in logged    # unchanged repeat not listed
    assert "Next export (UTC): (none) → 2026-11-10 07:00:00" in logged

    client.post("/payables/settings", data={"csrf": token, "mailbox_id": box["id"], "export_auto": "on",
                                            "export_every": "2", "export_unit": "weeks"})       # no time: worked out
    row = db.execute("SELECT export_every, export_unit, export_next FROM payables_setting").fetchone()
    assert row[:2] == (2, "weeks") and row[2] > "2026-11-20"

    bad = client.post("/payables/settings", follow_redirects=True, data={
        "csrf": token, "mailbox_id": box["id"], "export_every": "0", "export_unit": "days"}).get_data(as_text=True)
    assert "Not saved: Repeat every must be a whole number from 1 to 999." in bad
    assert db.execute("SELECT export_every FROM payables_setting").fetchone()[0] == 2


def test_settings_say_plainly_when_exports_are_not_set_up(client, db, tmp_path, monkeypatch):
    monkeypatch.delenv("PAYABLES_EXPORT_HOST", raising=False)
    signed_in(client)
    stored(db, tmp_path)
    page = client.get("/payables/settings").get_data(as_text=True)
    assert "Ask the administrator" in page and "disabled" in page and "docker" not in page.lower()
