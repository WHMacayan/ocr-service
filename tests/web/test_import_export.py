"""The Import / Export page: the Booking Report upload, the setup workbook, the full backup."""

import re
import io
import json

from openpyxl import Workbook, load_workbook

from app.shared.core import store, transfer

from fakes import signed_in


def mapping(db, **values):
    row = dict({"role": "forwarder", "document_name": "QUARTZ LOGISTICS", "partner_id": 113794,
                "partner_name": "Quartz Logistics (NY) Inc.", "active": 1}, **values)
    db.execute(f"INSERT INTO vendor_mapping ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    db.commit()


def test_the_page_shows_its_four_parts(db, client):
    signed_in(client)
    page = client.get("/import-export").get_data(as_text=True)
    for part in ("Booking Report", "Bookings status", "Mappings and setup", "Full backup"):
        assert part in page


def test_the_setup_workbook_goes_out_and_back_unchanged_and_never_holds_secrets(db):
    mapping(db)
    store.set_setting(db, "openai_api_key", "sk-secret")
    data = transfer.export_setup(db)
    wb = load_workbook(io.BytesIO(data))
    assert {"Vendor mappings", "Subject formats", "Settings"} <= set(wb.sheetnames)
    keys = [r[0] for r in wb["Settings"].iter_rows(min_row=2, values_only=True)]
    assert "openai_api_key" not in keys and "admin_password_hash" not in keys
    summary, _notes = transfer.import_setup(db, data)
    assert not any(k.endswith((": changed", ": added")) for k in summary), summary


def test_a_setup_workbook_adds_and_changes_mappings_only_when_applied(db):
    mapping(db)
    wb = Workbook()
    ws = wb.active
    ws.title = "Vendor mappings"
    ws.append(["role", "document_name", "sender_domain", "partner_id", "partner_name", "cost_per_container", "active"])
    ws.append(["forwarder", "QUARTZ LOGISTICS", None, 113794, "Quartz Logistics (NY) Inc.", 6500, 1])
    ws.append(["forwarder", "TIANSHI", None, 113794, "Quartz Logistics (NY) Inc.", None, 1])
    out = io.BytesIO()
    wb.save(out)
    summary, _ = transfer.import_setup(db, out.getvalue())
    assert summary["Vendor mappings: changed"] == 1 and summary["Vendor mappings: added"] == 1
    assert db.execute("SELECT COUNT(*) FROM vendor_mapping").fetchone()[0] == 1          # a preview writes nothing
    transfer.import_setup(db, out.getvalue(), apply=True)
    rows = {r["document_name"]: r for r in db.execute("SELECT * FROM vendor_mapping")}
    assert rows["QUARTZ LOGISTICS"]["cost_per_container"] == 6500 and "TIANSHI" in rows


def test_a_backup_restores_what_was_lost(db):
    mapping(db)
    data = transfer.backup_bytes(db)
    assert json.loads(data)["tables"]["vendor_mapping"][0]["document_name"] == "QUARTZ LOGISTICS"
    db.execute("DELETE FROM vendor_mapping")
    db.commit()
    summary, _ = transfer.restore(db, data)
    assert summary["vendor_mapping: added"] == 1
    transfer.restore(db, data, apply=True)
    assert db.execute("SELECT document_name FROM vendor_mapping").fetchone()[0] == "QUARTZ LOGISTICS"
    assert transfer.restore(db, b"not a backup")[1] == ["This is not a backup file from the OCR service."]


def booking_report():
    wb = Workbook()
    ws = wb.active
    ws.title = "26OCT"
    ws.append(["PO#", "Factory", "BOOKING DATE", "FORWARDER", "FREIGHT", "Departure Port", "PORT OF DISCHARGE",
               "Booked Place", "40HQ", "ETD", "ETA", "Booking confirmation", "Factory invoice", "FD"])
    ws.append(["CC11752NEWYORK1-OCT-01", "vicky.lai@senturytire.com", None, "APEX", 6900, "LAEM CHABANG", "NEW YORK",
               "NEW YORK", 3, None, None, "BKKM94310800", "F26AR10THA0705", "EAST HAVEN, CT 06512"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_a_booking_report_is_previewed_then_used_on_the_next_pass(db, client):
    token = signed_in(client)
    store.set_setting(db, "fill_last_at", "2026-11-17T05:00:00")
    client.post("/import-export/report", data={"csrf": token, "action": "preview",
                                               "file": (io.BytesIO(booking_report()), "Booking Report.xlsx")},
                content_type="multipart/form-data")
    page = client.get("/import-export").get_data(as_text=True)
    assert "Use this report" in page and "26OCT (1)" in page
    assert not (store.data_dir() / "booking_report.xlsx").exists()                     # not used before Apply
    digest = re.search(r'name="digest" value="([0-9a-f]{64})"', page).group(1)
    client.post("/import-export/report", data={"csrf": token, "action": "apply", "digest": digest})
    assert (store.data_dir() / "booking_report.xlsx").exists()
    assert store.get_setting(db, "fill_last_at") is None                                # filled on the next pass


def test_a_setup_workbook_replaced_after_its_preview_is_not_applied(client, db):
    token = signed_in(client)

    def book(value):
        wb = Workbook()
        ws = wb.active
        ws.title = "Settings"
        ws.append(["key", "value"])
        ws.append(["max_pages", value])
        buffer = io.BytesIO()
        wb.save(buffer)
        return buffer.getvalue()

    def preview(value):
        client.post("/import-export/setup", data={"csrf": token, "action": "preview", "file": (io.BytesIO(book(value)), "s.xlsx")},
                    content_type="multipart/form-data")
        return client.get("/import-export").get_data(as_text=True)

    first = preview("11")
    digest = re.search(r'name="digest" value="([0-9a-f]{64})"', first).group(1)
    preview("12")  # another browser uploads over it

    refused = client.post("/import-export/setup", data={"csrf": token, "action": "apply", "digest": digest}, follow_redirects=True)

    assert "replaced after you previewed it" in refused.get_data(as_text=True)
    assert store.get_setting(db, "max_pages") == "20"


# ── v003 R02: what is checked, previewed and applied is one set of bytes ──

import hashlib  # noqa: E402

from app.shared.jobs import TransferPart  # noqa: E402
from app.shared.web import data_transfer  # noqa: E402


def synthetic(monkeypatch, on_preview=None):
    """A transfer part of its own; ``applied`` collects the bytes its apply was given."""
    applied = []

    def preview(path):
        if on_preview:
            on_preview()  # another upload arrives while the preview is being made
        return {"changes": [], "notes": []}

    part = TransferPart("synthetic", "synthetic.pending.bin", "", preview,
                        lambda db, path: applied.append(path.read_bytes()))
    monkeypatch.setattr(data_transfer, "parts", lambda: {"synthetic": part})
    return part, applied


def pending_file():
    return store.data_dir() / "synthetic.pending.bin"


def test_a_file_that_replaces_the_pending_one_during_the_preview_is_not_the_one_applied(client, db, monkeypatch):
    token = signed_in(client)
    pending = pending_file()
    pending.write_bytes(b"A")
    fingerprint = hashlib.sha256(b"A").hexdigest()
    part, applied = synthetic(monkeypatch, on_preview=lambda: pending.write_bytes(b"B"))

    client.post("/import-export/synthetic", data={"csrf": token, "action": "apply", "digest": fingerprint})

    assert applied == [b"A"]                      # the bytes that were previewed and validated
    assert pending.read_bytes() == b"B"           # the new upload is still waiting for its own preview


def test_a_file_replaced_after_the_digest_check_cannot_change_what_is_applied(client, db, monkeypatch):
    token = signed_in(client)
    pending = pending_file()
    pending.write_bytes(b"A")
    part, applied = synthetic(monkeypatch)
    real = data_transfer.digest

    def digest_then_replace(path):
        value = real(path)
        pending.write_bytes(b"B")  # replaced right after the check
        return value
    monkeypatch.setattr(data_transfer, "digest", digest_then_replace)

    client.post("/import-export/synthetic", data={"csrf": token, "action": "apply", "digest": hashlib.sha256(b"A").hexdigest()})

    assert applied == [b"A"] and pending.read_bytes() == b"B"


def test_a_file_replaced_before_apply_is_refused_and_left_for_its_own_preview(client, db, monkeypatch):
    token = signed_in(client)
    pending = pending_file()
    pending.write_bytes(b"B")
    part, applied = synthetic(monkeypatch)

    client.post("/import-export/synthetic", data={"csrf": token, "action": "apply", "digest": hashlib.sha256(b"A").hexdigest()})

    assert applied == [] and pending.read_bytes() == b"B"


def test_the_page_digest_is_of_the_bytes_the_preview_was_made_from(client, db, monkeypatch):
    signed_in(client)
    pending = pending_file()
    pending.write_bytes(b"A")
    seen = []

    def preview(path):
        seen.append(path.read_bytes())
        pending.write_bytes(b"B")  # replaced while previewing
        return {"changes": [], "notes": []}
    part = TransferPart("synthetic", "synthetic.pending.bin", "", preview, lambda db, path: None)
    monkeypatch.setattr(data_transfer, "parts", lambda: {"synthetic": part})

    previews = {}
    monkeypatch.setattr(data_transfer, "render_template", lambda *a, **k: previews.update(k["previews"]) or "ok")
    client.get("/import-export")

    assert seen == [b"A"] and previews["synthetic"]["digest"] == hashlib.sha256(b"A").hexdigest()
    assert not list(store.data_dir().glob(".*.bin"))  # no private copies left behind
