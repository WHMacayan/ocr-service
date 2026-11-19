"""Import / Export on every list: the menu on each page, the field picker and its downloads, and the import flow
(upload, map the columns, test, apply)."""

import io

import pytest

from app.shared.data.tables import read_table, write_table

from data_fakes import DataOdoo, patch_odoo, seed_bookings
from fakes import connection, mailbox, signed_in


def last_activity(db):
    return db.execute("SELECT kind, summary, detail FROM activity ORDER BY id DESC LIMIT 1").fetchone()


def upload(client, token, key, headers, rows, filename="file.xlsx"):
    data = write_table(headers, rows, "csv" if filename.endswith(".csv") else "xlsx")
    return client.post(f"/data/{key}/import", data={"csrf": token, "action": "upload", "file": (io.BytesIO(data), filename)},
                       content_type="multipart/form-data", follow_redirects=True)


def seed_mappings(db):
    with db:
        db.executemany("INSERT INTO vendor_mapping (role, document_name, partner_id, partner_name, active, cost_per_container) "
                       "VALUES (?, ?, ?, ?, 1, ?)", [("forwarder", "QUARTZ LOGISTICS", 5, "Quartz Logistics (NY) Inc.", 4900.0),
                                                      ("forwarder", "K-APEX", 6, "Apex Maritime Co.,Inc.", None)])


# ── the menu, on every list ──────────────────────────────────────────

@pytest.mark.parametrize("path, offers_import, key", [
    ("/bookings", True, "bookings"), ("/scraped-bills", True, "scraped-bills"), ("/mailboxes/", True, "mailboxes"),
    ("/vendor-mappings/", True, "vendor-mappings"), ("/subject-formats/", True, "subject-formats"),
    ("/messages", False, "emails"), ("/activity?groupby=none", False, "activity"), ("/payables/emails", False, "payables-emails"),
    ("/customs", False, "customs-batches")])
def test_every_list_has_an_import_export_menu_and_only_the_editable_ones_offer_an_import(client, path, offers_import, key):
    signed_in(client)
    page = client.get(path).get_data(as_text=True)
    assert f'href="/data/{key}/export' in page
    assert (f'href="/data/{key}/import"' in page) is offers_import


def test_the_menu_carries_what_the_list_was_showing_to_the_export(client):
    signed_in(client)
    assert 'href="/data/emails/export?state=failed&amp;q=apex"' in client.get("/messages?state=failed&q=apex").get_data(as_text=True)
    assert 'href="/data/activity/export?kind=odoo"' in client.get("/activity?kind=odoo").get_data(as_text=True)
    customs = client.get("/customs").get_data(as_text=True)
    assert '/data/customs-batches/export' in customs and '/data/customs-entries/export' in customs


def test_the_import_export_page_lists_every_list(client):
    signed_in(client)
    page = client.get("/import-export").get_data(as_text=True)
    for key in ("mailboxes", "vendor-mappings", "subject-formats", "bookings", "scraped-bills", "customs-entries", "emails",
                "activity", "payables-emails"):
        assert f'href="/data/{key}/export"' in page
    assert 'href="/data/bookings/import"' in page and 'href="/data/emails/import"' not in page


# ── export ───────────────────────────────────────────────────────────

def test_the_picker_offers_every_field_with_the_lists_columns_ticked_and_remembers_the_last_pick(client, db):
    token = signed_in(client)
    page = client.get("/data/vendor-mappings/export").get_data(as_text=True)
    assert 'name="f_role"' in page and 'name="f_cost_per_container"' in page and "Odoo partner (Odoo ID)" in page
    assert 'name="f_partner_name" id="f_partner_name" checked' in page and 'name="f_cost_per_container" id="f_cost_per_container" >' in page
    assert 'name="f_client_secret"' not in client.get("/data/mailboxes/export").get_data(as_text=True)   # a secret is never offered
    assert 'name="f_role" id="f_role" checked' in client.get("/data/vendor-mappings/export?preset=all").get_data(as_text=True)
    assert "checked" not in client.get("/data/vendor-mappings/export?preset=none").get_data(as_text=True).split('id="fields"')[1].split("</table>")[0]
    client.post("/data/vendor-mappings/export", data={"csrf": token, "f_role": "on", "f_cost_per_container": "on", "format": "csv"})
    again = client.get("/data/vendor-mappings/export").get_data(as_text=True)
    assert 'name="f_cost_per_container" id="f_cost_per_container" checked' in again and 'name="f_partner_name" id="f_partner_name" >' in again


def test_the_file_has_the_fields_picked_in_the_lists_order_as_excel_or_csv(client, db):
    token = signed_in(client)
    seed_mappings(db)
    resp = client.post("/data/vendor-mappings/export", data={"csrf": token, "f_cost_per_container": "on", "f_id": "on",
                                                             "f_partner_name": "on", "format": "xlsx"})
    assert resp.status_code == 200 and "attachment" in resp.headers["Content-Disposition"] and ".xlsx" in resp.headers["Content-Disposition"]
    assert read_table(resp.data, "x.xlsx") == (["ID", "Odoo partner", "Cost per container"],
                                               [[1, "Quartz Logistics (NY) Inc.", 4900], [2, "Apex Maritime Co.,Inc.", None]])
    csv = client.post("/data/vendor-mappings/export", data={"csrf": token, "f_role": "on", "format": "csv"})
    assert csv.mimetype == "text/csv" and read_table(csv.data, "x.csv") == (["Role"], [["Forwarder"], ["Forwarder"]])
    assert last_activity(db)["summary"] == "Exported 2 vendor mappings" and "Fields: Role" in last_activity(db)["detail"]


def test_nothing_picked_is_not_an_export(client, db):
    token = signed_in(client)
    resp = client.post("/data/vendor-mappings/export", data={"csrf": token, "format": "xlsx"}, follow_redirects=True)
    assert "Pick at least one field" in resp.get_data(as_text=True)


def test_emails_export_what_the_list_showed_and_a_subject_that_looks_like_a_formula_is_made_safe(client, db):
    token = signed_in(client)
    box = mailbox(db)
    with db:
        db.executemany("INSERT INTO message (mailbox_id, dedupe_key, subject, sender, state, received) VALUES (?, ?, ?, ?, ?, ?)",
                       [(box["id"], "a", '=HYPERLINK("http://evil.test","x")', "x@evil.test", "failed", "2026-11-05 10:00:00"),
                        (box["id"], "b", "Fine", "ops@fwd.com", "done", "2026-11-05 11:00:00")])
    resp = client.post("/data/emails/export?state=failed", data={"csrf": token, "f_subject": "on", "f_state": "on", "format": "xlsx"})
    headers, rows = read_table(resp.data, "x.xlsx")
    assert headers == ["Subject", "State"] and rows == [["'=HYPERLINK(\"http://evil.test\",\"x\")", "Failed"]]


def test_an_unknown_list_is_not_found(client):
    signed_in(client)
    assert client.get("/data/nothing/export").status_code == 404
    assert client.get("/data/emails/import").status_code == 404          # a record of what happened is never imported


def test_a_picker_for_odoo_says_so_when_odoo_cannot_be_read(client, monkeypatch):
    signed_in(client)

    def refuse(db):
        raise RuntimeError("No Odoo connection is switched on.")
    monkeypatch.setattr("app.shared.odoo.client.Odoo.from_store", refuse)
    page = client.get("/data/bookings/export").get_data(as_text=True)
    assert "Odoo could not be read, so its fields are not known" in page and "No Odoo connection" in page


def test_the_bookings_are_exported_with_the_fields_odoo_offers(client, db, monkeypatch):
    token = signed_in(client)
    patch_odoo(monkeypatch, DataOdoo())
    seed_bookings(db)
    assert 'name="f_forwarder_id"' in client.get("/data/bookings/export").get_data(as_text=True)
    resp = client.post("/data/bookings/export", data={"csrf": token, "f_id": "on", "f_booking_number": "on", "f_forwarder_id": "on",
                                                      "f_container_ids": "on", "format": "xlsx"})
    assert read_table(resp.data, "x.xlsx") == (
        ["ID", "Booking Number", "Forwarder (Payable Vendor)", "Containers"],
        [[42, "BK200", "Apex Maritime Co.,Inc.", None], [41, "BK123", "Quartz Logistics (NY) Inc.", "MSCU1234566, TGHU7654320"]])


# ── import ───────────────────────────────────────────────────────────

def test_a_file_is_uploaded_mapped_tested_and_only_then_applied(client, db):
    token = signed_in(client)
    seed_mappings(db)
    page = upload(client, token, "vendor-mappings", ["id", "Role", "Name on documents", "Sender domain", "Cost per container"],
                  [[1, None, None, None, 6500], [None, "Factory", "SENTURY", None, 100]]).get_data(as_text=True)
    assert "2. Map your columns" in page and "2 rows" in page and 'name="col_4"' in page
    assert '<option value="cost_per_container" selected>' in page and '<option value="document_name" selected>' in page   # matched by label
    fields = {"csrf": token, "col_0": "id", "col_1": "role", "col_2": "document_name", "col_3": "sender_domain", "col_4": "cost_per_container"}
    tested = client.post("/data/vendor-mappings/import", data={**fields, "action": "test"}).get_data(as_text=True)
    assert "1 to add · 1 to update · 0 with no change" in tested and "Nothing has been written" in tested
    assert "Cost per container: 4900.0 → 6500.0" in tested
    assert db.execute("SELECT COUNT(*) FROM vendor_mapping").fetchone()[0] == 2        # a test writes nothing
    done = client.post("/data/vendor-mappings/import", data={**fields, "action": "apply"}).get_data(as_text=True)
    assert "1 added · 1 updated" in done
    assert db.execute("SELECT cost_per_container FROM vendor_mapping WHERE id = 1").fetchone()[0] == 6500.0
    assert db.execute("SELECT role, document_name FROM vendor_mapping WHERE id = 3").fetchone()[:] == ("factory", "SENTURY")
    entry = last_activity(db)
    assert entry["kind"] == "settings" and entry["summary"] == "Imported vendor mappings: 1 added, 1 updated"
    assert "Row 2: updated" in entry["detail"] and "Cost per container: 4900.0 → 6500.0" in entry["detail"]
    assert "2. Map your columns" not in client.get("/data/vendor-mappings/import").get_data(as_text=True)   # the file is used up


def test_a_row_with_a_problem_stops_the_import_unless_it_is_skipped(client, db):
    token = signed_in(client)
    seed_mappings(db)
    upload(client, token, "vendor-mappings", ["Role", "Name on documents", "Sender domain", "Cost per container"],
           [["Forwarder", "FRESH", None, 100], ["Carrier", "BAD", None, 200]])
    fields = {"csrf": token, "col_0": "role", "col_1": "document_name", "col_2": "sender_domain", "col_3": "cost_per_container"}
    stopped = client.post("/data/vendor-mappings/import", data={**fields, "action": "apply"}, follow_redirects=True).get_data(as_text=True)
    assert "1 row(s) have a problem" in stopped and "Role: choose one of: Forwarder, Factory" in stopped
    assert db.execute("SELECT COUNT(*) FROM vendor_mapping").fetchone()[0] == 2
    done = client.post("/data/vendor-mappings/import", data={**fields, "skip": "on", "action": "apply"}).get_data(as_text=True)
    assert "1 added · 0 updated" in done and "1 skipped" in done
    assert db.execute("SELECT COUNT(*) FROM vendor_mapping").fetchone()[0] == 3


def test_a_file_can_be_discarded_and_a_wrong_kind_of_file_is_refused(client, db):
    token = signed_in(client)
    upload(client, token, "vendor-mappings", ["Role"], [["Forwarder"]])
    assert "Map your columns" in client.get("/data/vendor-mappings/import").get_data(as_text=True)
    client.post("/data/vendor-mappings/import", data={"csrf": token, "action": "discard"})
    assert "1. Choose a file" in client.get("/data/vendor-mappings/import").get_data(as_text=True)
    refused = client.post("/data/vendor-mappings/import", data={"csrf": token, "action": "upload", "file": (io.BytesIO(b"x"), "x.pdf")},
                          content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
    assert "Use an Excel workbook (.xlsx) or a CSV file (.csv)" in refused
    broken = client.post("/data/vendor-mappings/import", data={"csrf": token, "action": "upload", "file": (io.BytesIO(b"not excel"), "x.xlsx")},
                         content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
    assert "could not be opened as an Excel workbook" in broken


def test_a_csv_file_imports_like_a_workbook(client, db):
    token = signed_in(client)
    upload(client, token, "subject-formats", ["Name", "Subject pattern", "This subject confirms the booking"],
           [["Maersk", r"BKG (?P<booking_number>\d+)", "Yes"]], filename="formats.csv")
    client.post("/data/subject-formats/import", data={"csrf": token, "col_0": "name", "col_1": "subject_pattern", "col_2": "confirms_booking",
                                                      "action": "apply"})
    assert tuple(db.execute("SELECT name, confirms_booking FROM subject_format").fetchone()) == ("Maersk", 1)


def test_the_template_has_the_columns_an_import_takes_and_says_how_to_fill_them(client):
    signed_in(client)
    resp = client.get("/data/vendor-mappings/import/template")
    headers, rows = read_table(resp.data, "x.xlsx")
    assert headers[0] == "ID" and "Role" in headers and "Cost per container" in headers and rows == []
    import openpyxl
    guide = openpyxl.load_workbook(io.BytesIO(resp.data))["How to fill in"]
    texts = {r[0].value: r[1].value for r in guide.iter_rows()}
    assert "Leave the ID empty to add a new vendor mapping" in texts["About this file"]
    assert texts["Role"].startswith("one of: Forwarder, Factory") and "Required for a new record" in texts["Role"]


def test_nothing_is_imported_without_the_page_token(client, db):
    signed_in(client)
    resp = client.post("/data/vendor-mappings/import", data={"action": "apply"}, follow_redirects=True)
    assert "nothing was saved" in resp.get_data(as_text=True)


# ── import into Odoo ─────────────────────────────────────────────────

def test_bookings_are_imported_into_the_connection_chosen_and_production_is_called_by_its_name(client, db, monkeypatch):
    token = signed_in(client)
    odoo = patch_odoo(monkeypatch, DataOdoo())
    connection(db)                                         # an Odoo connection that is on: "Production"
    page = upload(client, token, "bookings", ["ID", "Booking Number", "Vessel", "Forwarder (Payable Vendor)"],
                  [[41, None, "EVER GIVEN", None], [None, "BK999", "NEW SHIP", "Apex Maritime Co.,Inc."]]).get_data(as_text=True)
    assert "Importing writes to <b>Odoo: Staging (1115)</b> — <b>PRODUCTION</b>" in page
    fields = {"csrf": token, "col_0": "id", "col_1": "booking_number", "col_2": "vessel", "col_3": "forwarder_id"}
    tested = client.post("/data/bookings/import", data={**fields, "action": "test"}).get_data(as_text=True)
    assert "1 to add · 1 to update" in tested and "Vessel: MSC ANNA → EVER GIVEN" in tested and odoo.writes == []
    done = client.post("/data/bookings/import", data={**fields, "action": "apply"}).get_data(as_text=True)
    assert "1 added · 1 updated" in done
    assert [w[0] for w in odoo.writes] == ["write", "create"]
    entry = last_activity(db)
    assert entry["kind"] == "odoo" and entry["summary"] == "Imported bookings: 1 added, 1 updated"
    assert "Row 2: updated #41 (Vessel: MSC ANNA → EVER GIVEN)" in entry["detail"]


def test_an_import_into_odoo_says_so_when_odoo_cannot_be_read(client, db, monkeypatch):
    token = signed_in(client)
    patch_odoo(monkeypatch, DataOdoo())
    upload(client, token, "bookings", ["ID", "Vessel"], [[41, "X"]])

    def refuse(db, connection=None):
        raise RuntimeError("Odoo refused the login")
    monkeypatch.setattr("app.shared.odoo.client.Odoo.from_store", refuse)
    monkeypatch.setattr("app.shared.odoo.client.Odoo.from_connection", refuse)
    page = client.get("/data/bookings/import").get_data(as_text=True)
    assert "Odoo could not be read, so the fields are not known" in page and "Odoo refused the login" in page
