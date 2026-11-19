"""Scraped Bills' pages: the scraped bills the service sent to Odoo, as Odoo holds them now."""

from flask import g, url_for

from app.shared.web import bp, odoo_forms
from app.shared.web.odoo_records import _odoo_list, _odoo_record

SCRAPED_BILL = {
    "model": "b2b.ocr.scraped.bill", "purpose": "bill_matching", "title": "name",
    "status": "state", "steps": ("to_read", "waiting", "ready_for_po", "po_found", "bill_found", "bill_confirmed"),
    "alerts": (("posted_bill_warning", "warn"),),
    "sections": [
        ("Vendor", ["partner_id", "vendor_name", "vendor_vat", "sender_email", "waiting_reason"]),
        ("Invoice", ["invoice_date", "currency_name", "amount_total", "po_numbers", "master_bl", "house_bl",
                     "container_numbers", "source"]),
        ("Match", ["bill_id", "purchase_id", "match_score", "match_notes"]),
    ],
    "tabs": [],
}

BILL_LIST = ["name", "partner_id", "invoice_date", "amount_total", "currency_name", "state", "bill_id", "purchase_id"]


@bp.route("/scraped-bills")
def scraped_bills():
    return _odoo_list("Scraped bills", "Every supplier invoice the service sent to Odoo, as Odoo holds it now.",
                      SCRAPED_BILL["model"], odoo_forms.record_ids(g.db, "bill_matching"),
                      BILL_LIST, "web.scraped_bill", "bill_id", "Scraped bills appear here once the service has "
                      "sent an invoice from a Bill Matching mailbox to Odoo.", purpose="bill_matching", group="state", data=("scraped-bills",))


@bp.route("/scraped-bills/<int:bill_id>")
def scraped_bill(bill_id):
    return _odoo_record(SCRAPED_BILL, bill_id, [("Scraped bills", url_for("web.scraped_bills"))],
                        purpose="bill_matching")
