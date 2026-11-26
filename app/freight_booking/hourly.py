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


# A connection that could not be reached is tried again after this long, not an hour.
RETRY_AFTER_FAILURE = timedelta(minutes=5)


def _due(db, connection, now):
    last = store.get_setting(db, f"fill_last_at.{connection['id']}")
    return not last or now - datetime.fromisoformat(last) >= FILL_EVERY


def _done(db, connection, when):
    store.set_setting(db, f"fill_last_at.{connection['id']}", when.isoformat(timespec="seconds"))


def fill_bookings(db, now=None):
    """Each connection on has its own hour: one that cannot be reached neither stops the others nor makes them wait
    (review F16)."""
    path = store.data_dir() / BOOKING_REPORT
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    due = [c for c in store.connections(db, on=True) if _due(db, c, now)]
    if not due:
        return
    from openpyxl import load_workbook

    from app.freight_booking import booking_fill
    from app.freight_booking.rules import link_team_pos
    rows = booking_fill.read_details(load_workbook(path, read_only=True, data_only=True)) if path.exists() else None
    mappings = db.execute("SELECT * FROM vendor_mapping WHERE active = 1").fetchall()
    completed = False
    for connection in due:
        try:
            odoo = Odoo.from_connection(db, connection)
        except Exception:
            _logger.exception("Could not reach %s for the hourly fill; it is tried again soon", connection["name"])
            _done(db, connection, now - FILL_EVERY + RETRY_AFTER_FAILURE)
            continue
        worked = True
        try:
            # The team uploads its POs after the papers: a booking without one is looked up again every hour.
            linked = link_team_pos(odoo, mappings)
        except Exception:
            _logger.exception("Looking up the team's freight POs in %s failed", connection["name"])
            linked, worked = 0, False
        if linked:
            store.log_activity(db, "processing", f"Linked {linked} bookings to the team's freight PO, already "
                               f"existing ({connection['name']})")
        if rows is not None:
            try:
                done, _summary = booking_fill.fill(odoo, mappings, rows, apply=True)
            except Exception:
                _logger.exception("Filling the bookings of %s from the Booking Report failed", connection["name"])
                worked = False
            else:
                if done:
                    store.log_activity(db, "processing", f"Filled {len(done)} bookings from the Booking Report and the "
                                       f"team's freight POs ({connection['name']})")
        # The hour starts when the work succeeded; a failure is tried again in minutes (review v003 R06).
        _done(db, connection, now if worked else now - FILL_EVERY + RETRY_AFTER_FAILURE)
        completed = completed or worked
    if completed:
        store.set_setting(db, "fill_last_at", now.isoformat(timespec="seconds"))
