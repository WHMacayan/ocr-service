"""Scraped Bills (mailbox purpose Bill Matching): a supplier's commercial invoice becomes a scraped bill in Odoo,
matched to the vendor's bill or purchase order; invoices uploaded in Odoo by hand are read too.

    reading.py   what the AI reads in a supplier's file
    matcher.py   the BillMatcher: the scraped bill, and its match
    uploads.py   the invoices uploaded in Odoo (Upload Invoices)
    handler.py   its part of a pipeline pass;  pages.py  its pages
"""

from app.scraped_bills.reading import READING
from app.shared.jobs import Job, MenuItem


def _handler(run):
    from app.scraped_bills.handler import BillHandler
    return BillHandler(run)


JOB = Job(
    purpose="bill_matching", label="Bill Matching", reading=READING, handler=_handler,
    records={"b2b.ocr.scraped.bill": "Scraped bill"},
    menu=(MenuItem("Scraped bills", "web.scraped_bills", "/scraped-bills"),),
    pages="app.scraped_bills.pages", datasets="app.scraped_bills.data",
)
