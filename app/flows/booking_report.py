"""The team's Booking Report (one Excel tab per month, one row per booking): the rate per container agreed
with the forwarder, written to the booking's own Cost per Container in Odoo.

The rate is negotiated per booking and printed on no document the service reads, so this report is its
source (Sir Nic, 12 Nov 2026). A row finds its booking by booking number ("Booking confirmation"
column), also when Odoo holds it inside the MBL after the carrier code. A rate a person already entered
on the booking is never replaced: a different one is reported instead. Freight collect, two rates in one
cell, blanks and implausible rates are reported, never written. Nothing is written unless ``apply``.
"""

import re
from collections import Counter

from ..documents.text import normalize_identifier
from .freight import _SCAC, BOOKING

# Below this a rate per container is a typing slip (e.g. 20 for 2,000), not a price.
LOWEST_RATE = 500
COLLECT = re.compile(r"\b(FREIGHT|FRT)?\s*COLLECT\b", re.IGNORECASE)
_NUMBER = re.compile(r"^\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*$")

# Outcomes, in the order the summary lists them.
SET = "rate written to the booking (dry run: would be)"
SAME = "same rate already on the booking"
OTHER_RATE = "booking has another rate: kept, check it"
CONFLICT = "report gives this booking several rates: not written"
FREIGHT_COLLECT = "freight collect: not written"
TWO_RATES = "two rates in one cell: not written"
NO_RATE = "no rate yet"
SUSPICIOUS = f"rate below {LOWEST_RATE}: looks like a typo, not written"
UNREADABLE = "rate not readable: not written"
NOT_FOUND = "booking not in Odoo"
SEVERAL = "several Odoo bookings match: not written"
NO_NUMBER = "no booking number"


def key(value):
    """A booking or bill number as compared: separators and the carrier code before it dropped."""
    return _SCAC.sub("", normalize_identifier(value))


def rate_of(value):
    """(kind, rate): ("rate", 5500.0) · ("collect", None) · ("two", None) · ("none", None) · ("unreadable", None)."""
    if value in (None, ""):
        return "none", None
    if isinstance(value, (int, float)):
        return "rate", float(value)
    text = str(value).strip()
    if COLLECT.search(text):
        return "collect", None
    if "/" in text and len(re.findall(r"\d[\d,]*", text)) >= 2:
        return "two", None
    match = _NUMBER.match(text)
    if match:
        return "rate", float(match.group(1).replace(",", ""))
    return "unreadable", None


def read_rows(workbook):
    """Every booking row of every tab laid out as the Booking Report: [{tab, row, po, forwarder,
    numbers, freight}]. A tab without FORWARDER, FREIGHT and Booking confirmation headers is skipped."""
    rows = []
    for ws in workbook.worksheets:
        if not hasattr(ws, "iter_rows"):
            continue
        it = ws.iter_rows(values_only=True, max_col=40)
        columns = None
        for row_number, values in enumerate(it, 1):
            names = [str(v).strip().upper() if v is not None else "" for v in values]
            if {"FORWARDER", "FREIGHT", "BOOKING CONFIRMATION"} <= set(names):
                columns = {name: i for i, name in reversed(list(enumerate(names))) if name}
                break
            if row_number > 10:
                break
        if not columns:
            continue
        po_column = next((columns[n] for n in ("P.O", "PO#", "PO NUMBER", "PO") if n in columns), None)
        for row_number, values in enumerate(it, row_number + 1):
            if not values or not any(v not in (None, "") for v in values):
                continue
            cell = lambda i: values[i] if i is not None and i < len(values) else None  # noqa: E731
            booking = cell(columns["BOOKING CONFIRMATION"])
            numbers = [n for n in (key(part) for part in re.split(r"[/,;\n]", str(booking or ""))) if len(n) >= 6]
            rows.append({"tab": ws.title, "row": row_number, "po": str(cell(po_column) or "").strip(),
                         "forwarder": str(cell(columns["FORWARDER"]) or "").strip(), "numbers": numbers,
                         "freight": cell(columns["FREIGHT"])})
    return rows


def _odoo_bookings(odoo):
    """{compared key: [booking]} for every booking, by booking number and by MBL."""
    found = odoo.search_read(BOOKING, [], ["booking_number", "master_bl", "cost_per_container"],
                             context={"active_test": False})
    index = {}
    for booking in found:
        for value in (booking.get("booking_number"), booking.get("master_bl")):
            k = key(value)
            if len(k) >= 6:
                index.setdefault(k, {})[booking["id"]] = booking
    return {k: list(v.values()) for k, v in index.items()}


def import_rates(odoo, rows, apply=False):
    """(outcomes per row, summary Counter). Writes Cost per Container only when ``apply``."""
    index = _odoo_bookings(odoo)
    # A booking the report prices twice, differently, is left for a person.
    rates_by_booking = {}
    matched = []
    for row in rows:
        kind, rate = rate_of(row["freight"])
        bookings = {}
        for number in row["numbers"]:
            for booking in index.get(number, []):
                bookings[booking["id"]] = booking
        matched.append((row, kind, rate, list(bookings.values())))
        if kind == "rate" and len(bookings) == 1:
            rates_by_booking.setdefault(next(iter(bookings)), set()).add(rate)
    out, summary = [], Counter()
    for row, kind, rate, bookings in matched:
        booking = bookings[0] if len(bookings) == 1 else None
        if not row["numbers"]:
            outcome = NO_NUMBER
        elif kind == "collect":
            outcome = FREIGHT_COLLECT
        elif kind == "two":
            outcome = TWO_RATES
        elif kind == "none":
            outcome = NO_RATE
        elif kind == "unreadable":
            outcome = UNREADABLE
        elif rate < LOWEST_RATE:
            outcome = SUSPICIOUS
        elif not bookings:
            outcome = NOT_FOUND
        elif len(bookings) > 1:
            outcome = SEVERAL
        elif len(rates_by_booking.get(booking["id"], ())) > 1:
            outcome = CONFLICT
        elif booking.get("cost_per_container"):
            outcome = SAME if abs(booking["cost_per_container"] - rate) < 0.005 else OTHER_RATE
        else:
            outcome = SET
            if apply:
                odoo.write(BOOKING, [booking["id"]], {"cost_per_container": rate})
                booking["cost_per_container"] = rate  # a second row for the same booking reads as the same rate
        summary[outcome] += 1
        out.append(dict(row, rate=rate, outcome=outcome, booking_id=booking["id"] if booking else None,
                        booking_rate=(booking or {}).get("cost_per_container") or None))
    return out, summary


def write_report(path, outcomes):
    """The row-by-row result as an Excel file, for the team to check."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Booking Report import"
    ws.append(["Tab", "Row", "P.O", "Forwarder", "Booking number(s)", "FREIGHT in report", "Odoo booking id",
               "Rate on booking", "Outcome"])
    for cell in ws[1]:
        cell.font = Font(name="Arial", bold=True)
    for o in outcomes:
        ws.append([o["tab"], o["row"], o["po"], o["forwarder"], ", ".join(o["numbers"]),
                   o["freight"] if not isinstance(o["freight"], str) else o["freight"].strip(),
                   o["booking_id"], o["booking_rate"], o["outcome"]])
    for column, width in zip("ABCDEFGHI", (9, 6, 30, 18, 26, 16, 14, 14, 48)):
        ws.column_dimensions[column].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
