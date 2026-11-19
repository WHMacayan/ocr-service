"""Customs (mailbox purpose Customs Entry Summaries): each CBP 7501 entry summary the customs broker sends is uploaded
to Odoo's customs audit; other forms (a 3461 release) are kept and skipped.

    uploader.py  reading the form (Tesseract for scans, no AI) and the upload
    handler.py   its part of a pipeline pass;  pages.py  its pages
"""

from app.shared.jobs import Job, MenuItem


def _handler(run):
    from app.customs.handler import CustomsHandler
    return CustomsHandler(run)


JOB = Job(
    purpose="customs_entry", label="Customs Entry Summaries", handler=_handler,
    menu=(MenuItem("Customs", "web.customs_batches", "/customs"),),
    pages="app.customs.pages", datasets="app.customs.data",
)
