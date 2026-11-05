"""Read-only mirrors of the Odoo records the service feeds besides bookings: scraped bills and
customs entries, laid out like their Odoo forms, parent to child. Odoo's side is read live.

A page is plain data: the form's title field, status bar, alerts, field sections, then tabs that
hold either more fields or a child table.
"""

from .bookings import fields_info, shown


def child(model, inverse, columns, link=None):
    return {"model": model, "inverse": inverse, "columns": columns, "link": link}


SCRAPED_BILL = {
    "model": "b2b.ocr.scraped.bill", "purpose": "bill_matching", "title": "name",
    "status": "state", "steps": ("waiting", "ready_for_po", "po_found", "bill_found", "bill_confirmed"),
    "alerts": (("posted_bill_warning", "warn"), ("conflict_notes", "bad")),
    "sections": [
        ("Vendor", ["partner_id", "vendor_name", "vendor_vat", "sender_email", "waiting_reason"]),
        ("Invoice", ["invoice_date", "currency_name", "amount_total", "po_numbers", "master_bl", "house_bl",
                     "container_numbers", "source"]),
        ("Match", ["bill_id", "purchase_id", "match_score", "match_notes"]),
    ],
    "tabs": [("Lines", child("b2b.ocr.scraped.bill.line", "bill_scrape_id",
                             ["description", "quantity", "price_unit", "amount"]))],
}

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

BILL_LIST = ["name", "partner_id", "invoice_date", "amount_total", "currency_name", "state", "bill_id", "purchase_id",
             "line_ids"]
BATCH_LIST = ["create_date", "name", "entry_count", "pass_count", "review_count", "fail_count", "total_amount",
              "status"]


def record_ids(db, purpose):
    """Odoo ids of the records the service wrote from this purpose's mailboxes."""
    return {r[0] for r in db.execute(
        "SELECT DISTINCT f.odoo_id FROM file f JOIN message m ON m.id = f.message_id JOIN mailbox b "
        "ON b.id = m.mailbox_id WHERE f.odoo_id IS NOT NULL AND b.purpose = ?", (purpose,))}


def emails(db, purpose, record_id):
    """The emails whose files went into this record, newest first."""
    return db.execute(
        "SELECT DISTINCT m.id, m.subject, m.sender, m.received, m.state FROM message m JOIN file f "
        "ON f.message_id = m.id JOIN mailbox b ON b.id = m.mailbox_id WHERE f.odoo_id = ? AND b.purpose = ? "
        "ORDER BY m.received DESC", (record_id, purpose)).fetchall()


def email_counts(db, purpose):
    return dict(db.execute(
        "SELECT f.odoo_id, COUNT(DISTINCT f.message_id) FROM file f JOIN message m ON m.id = f.message_id "
        "JOIN mailbox b ON b.id = m.mailbox_id WHERE f.odoo_id IS NOT NULL AND b.purpose = ? GROUP BY f.odoo_id",
        (purpose,)).fetchall())


def list_rows(odoo, model, ids, columns):
    """[(column, label)], [{id, raw row, shown values}] for a list of records, newest first."""
    info = fields_info(odoo, model)
    columns = [c for c in columns if c in info]
    found = odoo.call(model, "search_read", [("id", "in", sorted(ids))], fields=columns,
                      order="id desc", context={"active_test": False}) if ids else []
    rows = [{"id": r["id"], "raw": r, **{c: _shown(r.get(c), info[c]) for c in columns}} for r in found]
    return [(c, info[c]["string"]) for c in columns], rows


def read(odoo, page, record_id):
    """Everything the record's page shows, or None when Odoo no longer has it."""
    info = fields_info(odoo, page["model"])
    wanted = {page["title"], page["status"], *page.get("subtitle", ()), *(name for name, _kind in page["alerts"]),
              *(f for _title, fields in page["sections"] for f in fields),
              *(f for _title, tab in page["tabs"] if isinstance(tab, list) for f in tab)}
    found = odoo.call(page["model"], "search_read", [("id", "=", record_id)],
                      fields=[f for f in wanted if f in info], context={"active_test": False})
    if not found:
        return None
    row = found[0]
    value = lambda f: _shown(row.get(f), info.get(f))  # noqa: E731
    status = row.get(page["status"])
    steps = list(page["steps"]) + ([status] if status and status not in page["steps"] else [])
    choices = dict(info.get(page["status"], {}).get("selection") or [])
    return {
        "title": value(page["title"]) or f"#{record_id}",
        "subtitle": " · ".join(filter(None, (value(f) for f in page.get("subtitle", ())))),
        "status": choices.get(status, status),
        "steps": [(choices.get(s, s), s == status) for s in steps],
        "alerts": [(kind, value(name)) for name, kind in page["alerts"] if row.get(name)],
        "sections": [(title, [(info[f]["string"], value(f)) for f in fields if f in info])
                     for title, fields in page["sections"]],
        "tabs": [_tab(odoo, title, tab, record_id, info, value) for title, tab in page["tabs"]],
    }


def _tab(odoo, title, tab, record_id, info, value):
    if isinstance(tab, list):
        return {"title": title, "fields": [(info[f]["string"], value(f)) for f in tab if f in info]}
    child_info = fields_info(odoo, tab["model"])
    columns = [c for c in tab["columns"] if c in child_info]
    found = odoo.call(tab["model"], "search_read", [(tab["inverse"], "=", record_id)], fields=columns, order="id")
    return {"title": title, "link": tab["link"], "columns": [child_info[c]["string"] for c in columns],
            "rows": [{"id": r["id"], "cells": [_shown(r.get(c), child_info[c]) for c in columns]} for r in found]}


def _shown(value, info):
    if (info or {}).get("type") == "one2many":
        return str(len(value or []))
    return shown(value, info)
