"""Customs' pages: the customs audit batches and entry summaries the service uploaded, as Odoo holds them now."""

from flask import g, url_for

from app.shared.web import bp, odoo_forms
from app.shared.web.base import _odoo_or_error
from app.shared.web.odoo_forms import child
from app.shared.web.odoo_records import _odoo_list, _odoo_record

CUSTOMS_ENTRY = {
    "model": "cbp7501.entry", "purpose": "customs_entry", "title": "name", "subtitle": ("file_name", "entry_type"),
    "status": "status", "steps": ("draft", "review_required", "pass"),
    "alerts": (),
    "sections": [
        ("Comparison", ["landed_cost_actual_amount", "landed_cost_estimated_amount", "landed_cost_po_amount",
                        "landed_cost_difference", "landed_cost_tolerance", "landed_cost_audited_on"]),
        ("Customs Bill", ["landed_cost_match_status", "landed_cost_po_status", "estimated_vendor_bill_id",
                          "purchase_order_id", "batch_id"]),
    ],
    "tabs": [
        ("Duties & Fees", ["total_entered_value", "total_duty", "mpf", "hmf", "ad", "cvd", "total_other",
                           "total_amount"]),
        ("Findings", child("cbp7501.finding", "entry_id", ["rule_id", "message", "expected", "actual", "severity"])),
        ("Document", ["file_name", "page_count", "pdf_delete_date", "po_source_document", "po_master_bill_of_lading",
                      "currency_id"]),
    ],
}

CUSTOMS_BATCH = {
    "model": "cbp7501.audit.batch", "title": "name",
    "status": "status", "steps": ("draft", "review_required", "pass"),
    "alerts": (),
    "sections": [("Batch", ["entry_count", "pass_count", "review_count", "fail_count", "findings_count",
                            "total_amount", "currency_id"])],
    "tabs": [
        ("Customs Entries", child("cbp7501.entry", "batch_id",
                                  ["entry_number", "file_name", "estimated_vendor_bill_id", "estimated_vendor_bill_state",
                                   "total_duty", "total_other", "total_amount", "landed_cost_difference",
                                   "landed_cost_match_status", "status"], link="web.customs_entry")),
        ("Notes", ["notes"]),
    ],
}

BATCH_LIST = ["create_date", "name", "entry_count", "pass_count", "review_count", "fail_count", "total_amount",
              "status"]


def _batch_ids():
    """The audit batches holding the entries the service uploaded."""
    entry_ids = odoo_forms.record_ids(g.db, "customs_entry")
    odoo, _error = _odoo_or_error()
    if not (odoo and entry_ids):
        return set()
    try:
        return {r["batch_id"][0] for r in odoo.call(
            CUSTOMS_ENTRY["model"], "search_read", [("id", "in", sorted(entry_ids))], fields=["batch_id"]) if r["batch_id"]}
    except Exception:
        return set()  # the list reports Odoo's error


@bp.route("/customs")
def customs_batches():
    return _odoo_list("Customs", "The customs audit batches holding the entry summaries the service uploaded, "
                      "as Odoo holds them now. Open a batch for its entries, and an entry for its findings.",
                      CUSTOMS_BATCH["model"], _batch_ids(), BATCH_LIST, "web.customs_batch", "batch_id",
                      "Batches appear here once the service has uploaded an entry summary from a Customs Entry "
                      "Summaries mailbox.", group="status", data=("customs-batches", "customs-entries"))


@bp.route("/customs/batches/<int:batch_id>")
def customs_batch(batch_id):
    return _odoo_record(CUSTOMS_BATCH, batch_id, [("Customs", url_for("web.customs_batches"))])


@bp.route("/customs/entries/<int:entry_id>")
def customs_entry(entry_id):
    crumbs = [("Customs", url_for("web.customs_batches"))]
    odoo, _error = _odoo_or_error()
    if odoo:
        try:
            found = odoo.call(CUSTOMS_ENTRY["model"], "search_read", [("id", "=", entry_id)],
                              fields=["batch_id"])
            if found and found[0]["batch_id"]:
                crumbs.append((found[0]["batch_id"][1], url_for("web.customs_batch", batch_id=found[0]["batch_id"][0])))
        except Exception:
            pass  # the page reports Odoo's error
    return _odoo_record(CUSTOMS_ENTRY, entry_id, crumbs, purpose="customs_entry")
