"""Freight Booking's part of the Import / Export page: the team's Booking Report (uploaded, previewed, then used by the
hourly fill) and the bookings status workbook."""

import io
import shutil
from collections import Counter
from datetime import datetime

from flask import flash, g, redirect, send_file, url_for
from openpyxl import load_workbook

from app.freight_booking import booking_fill, export
from app.freight_booking.hourly import BOOKING_REPORT
from app.shared.core import store
from app.shared.odoo.client import Odoo
from app.shared.web import bp

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _path(name):
    return store.data_dir() / name


def report_summary(path):
    """What a Booking Report holds, read without Odoo: rows per tab, and how many give each value."""
    rows = booking_fill.read_details(load_workbook(io.BytesIO(path.read_bytes()), read_only=True, data_only=True))
    tabs = Counter(r["tab"] for r in rows)
    return {"rows": len(rows), "tabs": sorted(tabs.items()),
            "invoices": sum(bool(r["invoices"]) for r in rows), "fd": sum(bool(r["fd"]) for r in rows),
            "rates": sum(booking_fill.rate_of(r["freight"])[0] == "rate" for r in rows),
            "factories": sum(bool(r["factory_email"]) for r in rows)}


def report_preview(path):
    return {"summary": report_summary(path)}


def use_report(db, pending):
    current = _path(BOOKING_REPORT)
    if current.exists():
        shutil.copyfile(current, _path("booking_report.previous.xlsx"))
    shutil.move(str(pending), current)
    with db:  # the worker fills on its next pass, not in an hour
        db.execute("DELETE FROM setting WHERE key = 'fill_last_at' OR key LIKE 'fill_last_at.%'")
    store.log_activity(db, "settings", "Booking Report uploaded: bookings are filled from it on the next pass",
                       actor="admin")
    flash("Booking Report saved. The bookings are filled from it on the worker's next pass (within minutes).",
          "success")


def report_context(db):
    current = _path(BOOKING_REPORT)
    report = None
    if current.exists():
        report = {"saved": datetime.fromtimestamp(current.stat().st_mtime), "size": current.stat().st_size}
    return {"report": report, "last_fill": store.get_setting(db, "fill_last_at"),
            "connections": [c["name"] for c in store.connections(db, on=True)]}


@bp.route("/import-export/bookings.xlsx")
def export_bookings():
    connections = []
    for connection in store.connections(g.db, on=True):
        try:
            connections.append((connection["name"], Odoo.from_connection(g.db, connection)))
        except Exception as exc:
            flash(f"{connection['name']} could not be reached: {exc}", "error")
    if not connections:
        flash("No Odoo connection is on: there are no bookings to export.", "error")
        return redirect(url_for("web.import_export"))
    return send_file(io.BytesIO(export.bookings_workbook(connections)), mimetype=XLSX, as_attachment=True,
                     download_name=f"Freight bookings status {datetime.now():%Y-%m-%d}.xlsx")
