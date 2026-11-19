"""Scraped Bills' part of a pipeline pass: each commercial invoice goes to the company's BillMatcher (matcher.py);
after the pass, the bills still waiting are matched again and the invoices uploaded in Odoo are read."""

from app.scraped_bills.matcher import BillMatcher
from app.scraped_bills.reading import not_invoice_note
from app.scraped_bills.uploads import read_uploads

PURPOSE = "bill_matching"


class BillHandler:
    def __init__(self, run):
        self.run = run
        self.odoo = run.odoo
        self.settings = run.settings
        self._matchers = {}

    def matcher(self, company_id):
        if company_id not in self._matchers:
            self._matchers[company_id] = BillMatcher(self.run.odoo, company_id, self.run.mappings)
        return self._matchers[company_id]

    def take(self, message, doc):
        """``(state, note, scraped bill id)`` for a file the AI read."""
        if doc["doc_class"] != "supplier_invoice":
            return "skipped", not_invoice_note(doc["values"]), None
        return self.matcher(message["company_id"]).take(doc)

    def after_pass(self, limit):
        for company_id in {row["company_id"] for row in self.run.db.execute(
                "SELECT company_id FROM mailbox WHERE purpose = ?", (PURPOSE,))}:
            self.matcher(company_id).rematch_waiting(limit)
        read_uploads(self, limit)
