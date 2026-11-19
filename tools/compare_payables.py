"""Compare Freight Payables output with the scanner's Run folders.

    python tools/compare_payables.py audit    "<Run A>/scan ....json"            "<Run B>/scan ....json"
    python tools/compare_payables.py invoices "<Run A>/.../Consolidated_Invoice.xlsx" "<Run B>/.../Consolidated_Invoice.xlsx"
    python tools/compare_payables.py inventory /data/ocr.sqlite3 "<Run>/emails <from> to <to>.json" <to>

Exits 0 only when there is no difference, so a side-by-side run can be checked by its exit code.
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

AUDIT_FIELDS = ("open_balance", "reference_no", "invoice_date", "vendor_name", "sheet")
INVOICE_FIELDS = ("Invoice Total", "charges", "lines", "REFERRENCE NO:", "INVOICE DATE", "sheet")


def norm(value):
    """Upper-case, trimmed, single spaces; None is ''."""
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip().upper()


def audit_key(row):
    return norm(row.get("vendor_id") or row.get("vendor_name")), norm(row.get("invoice_no"))


def invoice_key(row):
    return norm(row.get("Vendor ID") or row.get("Vendor Name")), norm(row.get("INVOICE NO."))


def _numbered(items, key):
    """Rows by key. A key seen again gets its occurrence number, so no row is silently dropped."""
    out, seen = {}, Counter()
    for row in items:
        k = key(row)
        seen[k] += 1
        out[k if seen[k] == 1 else (*k, seen[k])] = row
    return out


def load_audit(scan_json):
    data = json.loads(Path(scan_json).read_text(encoding="utf-8"))
    return _numbered(data.get("rows") or [], audit_key)


def load_invoices(xlsx):
    """One entry per invoice (charge lines summed) from every sheet of a Consolidated_Invoice workbook."""
    import openpyxl
    book = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    invoices = {}
    try:
        for sheet in book.worksheets:
            rows = sheet.iter_rows(values_only=True)
            headers = [str(h).strip() if h is not None else "" for h in next(rows, ())]
            for values in rows:
                row = dict(zip(headers, values))
                if not row.get("INVOICE NO."):
                    continue
                entry = invoices.setdefault(invoice_key(row), dict(row, sheet=sheet.title, charges=0, lines=0))
                entry["charges"] += row.get("Chare Amt") or 0
                entry["lines"] += 1
    finally:
        book.close()
    return invoices


def diff(left, right, fields):
    return {
        "only_left": [key for key in left if key not in right],
        "only_right": [key for key in right if key not in left],
        "changed": [(key, field, left[key].get(field), right[key].get(field))
                    for key in left if key in right for field in fields
                    if left[key].get(field) != right[key].get(field)],
    }


def inventory(db_path, emails_json, date_to):
    """Emails per conversation: the scanner's emails JSON against payables_message, over the same UTC days
    (the scanner's first day to ``date_to``). Returns {conversation: (scanner, store)} where they differ."""
    import sqlite3
    emails = json.loads(Path(emails_json).read_text(encoding="utf-8"))
    scanner = Counter(e["conversation_id"] for e in emails)
    first = min((e["received"][:10] for e in emails), default=date_to)
    db = sqlite3.connect(db_path)
    try:
        stored = Counter(row[0] for row in db.execute(
            "SELECT conversation_id FROM payables_message WHERE received >= ? AND received <= ?",
            (f"{first} 00:00:00", f"{date_to} 23:59:59")))
    finally:
        db.close()
    return {conversation: (scanner[conversation], stored[conversation])
            for conversation in sorted(set(scanner) | set(stored)) if scanner[conversation] != stored[conversation]}


def report(result, left_name, right_name):
    for key in result["only_left"]:
        print(f"only in {left_name}: {key}")
    for key in result["only_right"]:
        print(f"only in {right_name}: {key}")
    for key, field, a, b in result["changed"]:
        print(f"changed {key} {field}: {a!r} -> {b!r}")
    total = sum(len(v) for v in result.values())
    print(f"{total} difference(s)")
    return 0 if total == 0 else 1


def main(argv=None):
    # Vendor names in Vietnamese or Chinese must not crash a Windows console.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "invoices"):
        command = sub.add_parser(name)
        command.add_argument("left")
        command.add_argument("right")
    command = sub.add_parser("inventory", help="emails per conversation: ocr.sqlite3 against a Run's emails JSON")
    command.add_argument("db")
    command.add_argument("emails_json")
    command.add_argument("date_to")
    args = parser.parse_args(argv)
    if args.command == "inventory":
        differences = inventory(args.db, args.emails_json, args.date_to)
        for conversation, (scanner, stored) in differences.items():
            print(f"{conversation}: scanner {scanner}, store {stored}")
        print(f"{len(differences)} conversation(s) differ")
        return 0 if not differences else 1
    if args.command == "audit":
        result = diff(load_audit(args.left), load_audit(args.right), AUDIT_FIELDS)
    else:
        result = diff(load_invoices(args.left), load_invoices(args.right), INVOICE_FIELDS)
    return report(result, args.left, args.right)


if __name__ == "__main__":
    sys.exit(main())
