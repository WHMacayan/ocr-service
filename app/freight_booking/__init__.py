"""Freight Booking: shipping documents (booking confirmations, bills of lading, factory invoices, arrival notices)
become freight bookings in Odoo, ready for the freight PO; the team's Booking Report fills what no paper states.

    reading.py       what the AI reads in a shipping document; an email's own subject and body
    rules.py         the Booker: finding the booking of a document and consolidating it (dates, parties, B/Ls,
                     containers, the team's POs)
    handler.py       its part of a pipeline pass
    booking_fill.py  the Booking Report fill; hourly.py runs it, with the team-PO check, once an hour
    booking_report.py, cleanup.py, export.py, transfer.py, commands.py, pages.py, compare.py, history.py
"""

from app.freight_booking.reading import READING
from app.shared.jobs import Job, MenuItem, TransferPart


def _handler(run):
    from app.freight_booking.handler import FreightHandler
    return FreightHandler(run)


def _every_pass(db):
    from app.freight_booking.hourly import fill_bookings
    fill_bookings(db)


def _commands(cli):
    from app.freight_booking.commands import register
    register(cli)


def _report_preview(path):
    from app.freight_booking.transfer import report_preview
    return report_preview(path)


def _use_report(db, pending):
    from app.freight_booking.transfer import use_report
    use_report(db, pending)


def _report_context(db):
    from app.freight_booking.transfer import report_context
    return report_context(db)


JOB = Job(
    purpose="freight_booking", label="Freight Booking", reading=READING, handler=_handler, every_pass=_every_pass,
    records={"b2b.ocr.booking": "Booking"},
    menu=(MenuItem("Bookings", "web.booking_list", "/bookings"),),
    commands=_commands, pages="app.freight_booking.pages", datasets="app.freight_booking.data",
    transfer=(TransferPart("report", "booking_report.pending.xlsx", "freight_booking/_import_export.html",
                           _report_preview, _use_report, _report_context),),
    skips_senders=True, unprefixed_keys=True,
)
