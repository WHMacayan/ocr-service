"""A table in a file: Excel or CSV, written for a person to open and read back for an import.

Text that came from outside (an email's subject, a sender) can start with = + - or @, which Excel would run as a formula
when the file is opened; such a text is written with an apostrophe in front (safe), and read back without it (see
convert.to_text)."""

import csv
import io
from datetime import date, datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

XLSX = "xlsx"
CSV = "csv"
CONTENT_TYPES = {XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", CSV: "text/csv; charset=utf-8"}
MAX_COLUMNS = 200
MAX_ROWS = 20000
_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


def safe(value):
    """A cell value that no spreadsheet will run as a formula."""
    if isinstance(value, str) and value[:1] in _DANGEROUS:
        return "'" + value
    return value


def write_table(headers, rows, fmt=XLSX, sheet="Data", notes=None):
    """The file's bytes: a header row, then rows (lists of cell values). notes: [(heading, text)] on a second sheet (Excel)."""
    if fmt == CSV:
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow([safe(h) for h in headers])
        for row in rows:
            writer.writerow([safe(v) for v in row])
        return ("﻿" + out.getvalue()).encode("utf-8")  # a BOM: Excel opens it as UTF-8, not as ANSI
    wb = Workbook()
    ws = wb.active
    ws.title = sheet[:31]
    ws.append([safe(h) for h in headers])
    for row in rows:
        ws.append([safe(v) if not isinstance(v, (int, float, bool, date, datetime)) or v is None else v for v in row])
    for cell in ws[1]:
        cell.font = Font(name="Arial", bold=True)
        cell.alignment = Alignment(vertical="top")
    ws.freeze_panes = "A2"
    for index, header in enumerate(headers, 1):
        longest = max([len(str(header))] + [len(str(r[index - 1])) for r in rows[:200] if index - 1 < len(r) and r[index - 1] is not None])
        ws.column_dimensions[get_column_letter(index)].width = min(max(longest + 2, 10), 60)
    if notes:
        guide = wb.create_sheet("How to fill in")
        guide.column_dimensions["A"].width = 34
        guide.column_dimensions["B"].width = 100
        for heading, text in notes:
            guide.append([heading, text])
            guide.cell(row=guide.max_row, column=1).font = Font(name="Arial", bold=True)
            guide.cell(row=guide.max_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def read_table(raw, filename):
    """(headers, rows) of the first sheet of an Excel file, or of a CSV: every cell as the file holds it. A row with
    nothing in it is dropped; trailing empty columns are cut. Raises ValueError for a file that is not a table."""
    if filename.lower().endswith(".csv"):
        text = None
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("The file is not text: save it as CSV UTF-8 or as an Excel workbook.")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        table = [row for row in csv.reader(io.StringIO(text), dialect)]
    elif filename.lower().endswith((".xlsx", ".xlsm")):
        try:
            wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        except Exception as exc:
            raise ValueError(f"The file could not be opened as an Excel workbook: {exc}") from None
        table = [list(row) for row in wb.worksheets[0].iter_rows(values_only=True, max_col=MAX_COLUMNS)]
        wb.close()
    else:
        raise ValueError("Use an Excel workbook (.xlsx) or a CSV file (.csv).")
    table = [row for row in table if any(c not in (None, "") and str(c).strip() for c in row)]
    if not table:
        raise ValueError("The file has no rows.")
    width = max(len(row) for row in table)
    while width and all((len(row) <= width - 1 or row[width - 1] in (None, "")) for row in table):
        width -= 1
    table = [list(row[:width]) + [None] * (width - len(row)) for row in table]
    headers = [str(h).strip() if h is not None else "" for h in table[0]]
    if len(table) - 1 > MAX_ROWS:
        raise ValueError(f"The file has more than {MAX_ROWS} rows: split it.")
    return headers, table[1:]
