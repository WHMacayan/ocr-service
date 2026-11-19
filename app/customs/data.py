"""Customs' data sets: the audit batches and the entry summaries, as Odoo holds them. Export only: an entry is read from the
customs broker's form and audited by Odoo, so a file never edits it."""

from app.shared.data.odoo import OdooDataSet
from app.shared.web import odoo_forms

BATCH = "cbp7501.audit.batch"
ENTRY = "cbp7501.entry"


def _entry_ids(ctx, filters):
    return odoo_forms.record_ids(ctx.db, "customs_entry")


def _batch_ids(ctx, filters):
    entries = _entry_ids(ctx, filters)
    if not entries:
        return set()
    return {r["batch_id"][0] for r in ctx.odoo().call(ENTRY, "search_read", [("id", "in", sorted(entries))], fields=["batch_id"])
            if r["batch_id"]}


BATCHES = OdooDataSet("customs-batches", "Customs batches", "batch", BATCH, _batch_ids, "web.customs_batches",
                      default=("name", "create_date", "entry_count", "pass_count", "review_count", "fail_count",
                               "findings_count", "total_amount", "status"))
ENTRIES = OdooDataSet("customs-entries", "Customs entries", "entry", ENTRY, _entry_ids, "web.customs_batches",
                      default=("name", "entry_number", "file_name", "batch_id", "status", "total_duty", "mpf", "hmf",
                               "total_other", "total_amount", "landed_cost_difference", "landed_cost_match_status"))

DATASETS = (BATCHES, ENTRIES)
