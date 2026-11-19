"""Scraped Bills' data set: the supplier invoices the service sent to Odoo. An import updates a bill by its Odoo ID (a bill
comes from an invoice, so none is added by a file)."""

from app.scraped_bills.matcher import SCRAPED_BILL
from app.shared.data.odoo import OdooDataSet
from app.shared.web import odoo_forms

BILLS = OdooDataSet(
    "scraped-bills", "Scraped bills", "scraped bill", SCRAPED_BILL, lambda ctx, filters: odoo_forms.record_ids(ctx.db, "bill_matching"),
    "web.scraped_bills", importable=True, creatable=False,
    default=("name", "partner_id", "vendor_name", "invoice_date", "currency_name", "amount_total", "state", "po_numbers",
             "master_bl", "house_bl", "bill_id", "purchase_id"))

DATASETS = (BILLS,)
