import json

import openpyxl

from tools import compare_payables as cp

HEADERS = ["Vendor ID", "Vendor Name", "INVOICE DATE", "INVOICE NO.", "REFERRENCE NO:", "ETD", "Charges",
           "Chare Amt", "Invoice Total"]


def scan(tmp_path, rows, name="scan.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"date_from": "2026-11-03", "date_to": "2026-11-05", "rows": rows}), encoding="utf-8")
    return path


ROW_A = {"vendor_id": "KLN001", "vendor_name": "KLN", "invoice_no": "A1", "open_balance": 100, "sheet": "Ocean"}
ROW_B = {"vendor_id": "TQL001", "vendor_name": "TQL", "invoice_no": "B2", "open_balance": 50, "sheet": "Inland"}


def test_same_scan_has_no_difference(tmp_path):
    rows = cp.load_audit(scan(tmp_path, [ROW_A, ROW_B]))
    assert cp.diff(rows, rows, cp.AUDIT_FIELDS) == {"only_left": [], "only_right": [], "changed": []}
    assert cp.main(["audit", str(scan(tmp_path, [ROW_A, ROW_B])), str(scan(tmp_path, [ROW_A, ROW_B], "b.json"))]) == 0


def test_missing_and_changed_rows_reported(tmp_path):
    left = cp.load_audit(scan(tmp_path, [ROW_A, ROW_B]))
    right = cp.load_audit(scan(tmp_path, [dict(ROW_A, open_balance=120)], "right.json"))
    result = cp.diff(left, right, ("open_balance",))
    key_a, key_b = cp.audit_key(ROW_A), cp.audit_key(ROW_B)
    assert result["only_left"] == [key_b]
    assert result["only_right"] == []
    assert result["changed"] == [(key_a, "open_balance", 100, 120)]


def test_invoice_numbers_normalised():
    assert cp.audit_key({"vendor_id": "kln ", "invoice_no": " ab  12"}) == ("KLN", "AB 12")
    assert cp.audit_key({"vendor_id": None, "vendor_name": "Advik inc", "invoice_no": "1"}) == ("ADVIK INC", "1")


def test_repeated_invoice_rows_are_all_kept(tmp_path):
    rows = cp.load_audit(scan(tmp_path, [ROW_A, dict(ROW_A, sheet="Inland")]))
    assert len(rows) == 2


def test_invoice_workbook_charges_grouped(tmp_path):
    path = tmp_path / "Consolidated_Invoice.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(HEADERS)
    for charge, amount in (("PRE PULL", 75), ("STORAGE", 25), ("CHASSIS", 650)):
        sheet.append([None, "ADVIK INC", None, "31425-79225", "#MSDU7066308", None, charge, amount, 750])
    book.save(path)
    invoices = cp.load_invoices(path)
    assert list(invoices) == [("ADVIK INC", "31425-79225")]
    assert invoices[("ADVIK INC", "31425-79225")]["Invoice Total"] == 750
    assert invoices[("ADVIK INC", "31425-79225")]["charges"] == 750
    assert invoices[("ADVIK INC", "31425-79225")]["lines"] == 3


def test_report_survives_a_windows_console(tmp_path, monkeypatch):
    import io
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr("sys.stdout", console)
    vietnamese = dict(ROW_A, vendor_id=None, vendor_name="CÔNG TY TNHH TIẾP VẬN THIÊN SƯ")
    assert cp.main(["audit", str(scan(tmp_path, [vietnamese])), str(scan(tmp_path, [], "b.json"))]) == 1


def test_inventory_counts_by_conversation(db, tmp_path):
    db.execute("INSERT INTO mailbox (name, purpose) VALUES ('Payables', 'freight_payables')")
    stored = [("k1", "t1", "2026-11-03 08:00:00"), ("k2", "t1", "2026-11-04 09:00:00"),
              ("k3", "t2", "2026-11-05 23:00:00"),
              ("k4", "t3", "2026-11-06 01:00:00"),   # after date_to: not counted
              ("k5", "t9", "2026-11-02 23:59:00")]   # before the scanner's first day: not counted
    with db:
        db.executemany("INSERT INTO payables_message (mailbox_id, dedupe_key, conversation_id, received) "
                       "VALUES (1, ?, ?, ?)", stored)
    emails = tmp_path / "emails.json"
    emails.write_text(json.dumps([
        {"conversation_id": "t1", "received": "2026-11-03T08:00:00Z"},
        {"conversation_id": "t1", "received": "2026-11-04T09:00:00Z"},
        {"conversation_id": "t2", "received": "2026-11-05T23:00:00Z"}]), encoding="utf-8")
    db_path = db.directory / "ocr.sqlite3"

    assert cp.inventory(db_path, emails, "2026-11-05") == {}

    emails.write_text(json.dumps([{"conversation_id": "t1", "received": "2026-11-03T08:00:00Z"},
                                  {"conversation_id": "t4", "received": "2026-11-04T08:00:00Z"}]), encoding="utf-8")
    assert cp.inventory(db_path, emails, "2026-11-05") == {"t1": (1, 2), "t2": (0, 1), "t4": (1, 0)}


def test_main_reports_differences(tmp_path, capsys):
    assert cp.main(["audit", str(scan(tmp_path, [ROW_A])), str(scan(tmp_path, [ROW_B], "b.json"))]) == 1
    assert "only in" in capsys.readouterr().out
