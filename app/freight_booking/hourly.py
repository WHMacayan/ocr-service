"""Freight Booking's task after each worker pass, once an hour: every booking still without a PO is looked up in the
team's POs again (the team uploads them after the papers arrive), and the empty fields are filled from the Booking
Report saved on the Import / Export page."""

import logging
from datetime import datetime, timedelta, timezone

from app.shared.core import store
from app.shared.odoo.client import Odoo

_logger = logging.getLogger(__name__)

# The team's Booking Report, saved in the data folder: the bookings' rate, factory invoice and Deliver To come
# from it (booking_fill.py). Filled at most once an hour; only empty fields are filled.
BOOKING_REPORT = "booking_report.xlsx"
FILL_EVERY = timedelta(hours=1)


def fill_bookings(db, now=None):
    path = store.data_dir() / BOOKING_REPORT
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    last = store.get_setting(db, "fill_last_at")
    if last and now - datetime.fromisoformat(last) < FILL_EVERY:
        return
    from openpyxl import load_workbook

    from app.freight_booking import booking_fill
    from app.freight_booking.rules import link_team_pos
    store.set_setting(db, "fill_last_at", now.isoformat(timespec="seconds"))
    rows = booking_fill.read_details(load_workbook(path, read_only=True, data_only=True)) if path.exists() else None
    mappings = db.execute("SELECT * FROM vendor_mapping WHERE active = 1").fetchall()
    for connection in store.connections(db, on=True):
        odoo = Odoo.from_connection(db, connection)
        try:
            # The team uploads its POs after the papers: a booking without one is looked up again every hour.
            linked = link_team_pos(odoo, mappings)
        except Exception:
            _logger.exception("Looking up the team's freight POs in %s failed", connection["name"])
            linked = 0
        if linked:
            store.log_activity(db, "processing", f"Linked {linked} bookings to the team's freight PO, already "
                               f"existing ({connection['name']})")
        if rows is None:
            continue
        try:
            done, _summary = booking_fill.fill(odoo, mappings, rows, apply=True)
        except Exception:
            _logger.exception("Filling the bookings of %s from the Booking Report failed", connection["name"])
            continue
        if done:
            store.log_activity(db, "processing", f"Filled {len(done)} bookings from the Booking Report and the "
                               f"team's freight POs ({connection['name']})")
