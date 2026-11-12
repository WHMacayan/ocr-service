"""Invoices uploaded by hand in Odoo: Upload Invoices creates one scraped bill per file in state
``to_read`` (the file is its attachment); this reads each one the way an emailed invoice is read and
fills the bill in, matching included. The bill is the record to update: it takes the invoice's number
and identity, or merges into the bill already holding that invoice.

A file that is not a commercial invoice, comes from a forwarder, has no text, or prints no invoice number is parked as Needs Review with
the reason in Match Notes, so it is never read twice. A failure that may pass (OCR programs missing, the
AI unreachable) leaves it To Read for the next run.
"""

import base64
import logging

from ..documents import extract
from .bills import SCRAPED_BILL

TO_READ = "to_read"
_logger = logging.getLogger(__name__)


def read_uploads(run, limit):
    """Read up to ``limit`` bills waiting in Odoo; returns how many were taken."""
    odoo = run.odoo
    try:
        waiting = odoo.search_read(SCRAPED_BILL, [("state", "=", TO_READ)], ["name", "source", "company_id"],
                                   limit=limit, order="id")
    except Exception:  # Odoo unreachable, or a module without To Read: the next run tries again
        _logger.warning("Uploaded invoices could not be listed", exc_info=True)
        return 0
    for bill in waiting:
        try:
            _read(run, bill)
        except Exception:
            _logger.warning("Uploaded invoice %s could not be read now; it stays To Read", bill["id"], exc_info=True)
    return len(waiting)


def _park(odoo, bill_id, reason):
    odoo.write(SCRAPED_BILL, [bill_id], {"state": "needs_review", "match_notes": reason})


def _read(run, bill):
    odoo = run.odoo
    attachments = odoo.search_read("ir.attachment", [("res_model", "=", SCRAPED_BILL), ("res_id", "=", bill["id"])],
                                   ["name", "datas"], limit=1, order="id")
    if not attachments or not attachments[0]["datas"]:
        return _park(odoo, bill["id"], "The uploaded file is missing.")
    raw = base64.b64decode(attachments[0]["datas"])
    doc_class, values = extract.read_file(raw, "bill_matching", run.settings)
    if doc_class is None:
        return _park(odoo, bill["id"], "No readable text in this file.")
    if doc_class != "supplier_invoice":
        return _park(odoo, bill["id"], extract.not_invoice_note(values))
    doc = {"sender": "", "subject": "", "received": None, "doc_class": doc_class, "values": values,
           "source": bill["source"] or attachments[0]["name"] or bill["name"], "file_id": None}
    state, note, _id = run.matcher(bill["company_id"][0]).take(doc, placeholder=bill["id"])
    if state != "done":  # no invoice number printed
        _park(odoo, bill["id"], note)
