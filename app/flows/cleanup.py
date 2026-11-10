"""One-off passes over what is already in Odoo, after the booking and bill rules changed:

- conflicts: close the open booking conflicts the rules now settle (the same value written another
  way, a later document moving the schedule, an older one, the feeder beside the ocean vessel, the
  carrier beside the forwarder, an impossible value); the rest stay for a person;
- bills: read the invoices behind the Ready-to-Create-PO bills again, so their lines get their part
  number and product;
- freight: give the freight cost of the forwarders' invoices that reached Bill Matching to their bookings.
  (Freight invoices still waiting for a booking are tried again by the worker itself on its next runs.)

Nothing is written unless ``apply``; each pass returns what it found, counted by reason.
"""

import json
import logging
import re
from collections import Counter, defaultdict

from ..core import store
from ..documents import extract
from ..documents.text import file_text, normalize_identifier
from .bills import SCRAPED_BILL, BillMatcher
from .freight import (BOOKING, CARRIER, CONFLICT, UPDATED_BY_CONFIRMATION, _READ, plausible_date, plausible_vessel,
                      resolve_mapping, same_value)
from .pipeline import SETTING_KEYS, BookingHistory, _Run, _source

_logger = logging.getLogger(__name__)

_FEEDER_LINE = re.compile(r"PRE[\s-]*CARRI|FEEDER|1ST\s+VESSEL|FIRST\s+VESSEL", re.IGNORECASE)


# ── conflicts ────────────────────────────────────────────────────────

def _feeders(db, history, connection_id, booking_id, max_pages):
    """The feeder vessels and voyages the booking's papers print: from their readings, and for papers
    read before the readings had them, from the lines of their text that name a pre-carrier."""
    found = set(history.pre_carriers(booking_id))
    lines = []
    for row in db.execute(f"SELECT f.path {history._FILES} AND f.doc_class = 'booking_confirmation'",
                          (connection_id, booking_id)):
        try:
            with open(row["path"], "rb") as handle:
                text = file_text(handle.read(), max_pages)
        except Exception:  # a paper that can't be read again names no feeder
            continue
        lines += [normalize_identifier(line) for line in text.splitlines() if _FEEDER_LINE.search(line)]
    return found, lines


def verdict(name, old, new, booking, received_new, history, feeders, mappings):
    """(reason, take, value) for an open conflict the rules settle, else (None, False, value): take says the
    booking gets value (the new one, its date turned the right way round when it was read swapped)."""
    if name in ("etd", "eta"):
        fitting = plausible_date(name, new, received_new) if received_new else new
        if not fitting:
            return "an impossible date", False, new
        if fitting != new:
            new = fitting
            if new == old:
                return "a date read with its day and month swapped", False, new
    if name == "vessel" and not plausible_vessel(new):
        return "not a vessel name", False, new
    if same_value(name, old, new, mappings) or normalize_identifier(old) == normalize_identifier(new):
        return "the same value written another way", False, new
    if name in ("vessel", "voyage"):
        known, lines = feeders
        a, b = normalize_identifier(old), normalize_identifier(new)
        if b in known or any(b and b in line for line in lines):
            return "the feeder vessel beside the ocean vessel", False, new
        if a in known or any(a and a in line for line in lines):
            return "the ocean vessel replacing the feeder", True, new
    if name == "forwarder_name":
        if CARRIER.search(new) and not CARRIER.search(old):
            return "the carrier naming itself beside the forwarder", False, new
        if CARRIER.search(old) and not CARRIER.search(new) and resolve_mapping(mappings, "forwarder", name=new):
            return "the forwarder replacing the carrier", True, new
    if name in UPDATED_BY_CONFIRMATION:
        then = history.received(booking["id"], booking.get(f"{name}_source"))
        if then and received_new and then != received_new:
            return ("a later document moving the schedule", True, new) if received_new > then else (
                "an older document than the value kept", False, new)
    return None, False, new


def close_false_conflicts(db, odoo, connection_id, apply=False):
    history = BookingHistory(db, connection_id)
    mappings = db.execute("SELECT * FROM vendor_mapping WHERE active = 1").fetchall()
    max_pages = int(store.get_setting(db, "max_pages") or 3)
    by_booking = defaultdict(list)
    for conflict in odoo.search_read(CONFLICT, [("state", "=", "open")],
                                     ["booking_id", "field_name", "field_label", "existing_value", "new_value", "source"]):
        by_booking[conflict["booking_id"][0]].append(conflict)
    outcome = Counter()
    for booking_id, conflicts in by_booking.items():
        booking = odoo.call(BOOKING, "read", [booking_id], list(_READ))[0]
        feeders = (set(), [])
        if any(c["field_name"] in ("vessel", "voyage") for c in conflicts):
            feeders = _feeders(db, history, connection_id, booking_id, max_pages)
        # In the order their documents arrived: a later one judges against what an earlier one left.
        conflicts.sort(key=lambda c: history.received(booking_id, c["source"]) or "")
        updates, closed, notes = {}, [], []
        for c in conflicts:
            name = c["field_name"]
            current = dict(booking, **updates)
            old = str(current.get(name) or c["existing_value"] or "")
            reason, take, new = verdict(name, old, c["new_value"], current, history.received(booking_id, c["source"]),
                                   history, feeders, mappings)
            outcome[reason or "still a conflict for a person"] += 1
            if not reason:
                continue
            closed.append(c["id"])
            if take:
                updates[name] = new
                if f"{name}_source" in booking:
                    updates[f"{name}_source"] = c["source"]
                notes.append(f"{c['field_label'] or name}: {old} → {new} ({reason}, {c['source']}).")
            else:
                notes.append(f"{c['field_label'] or name}: kept {old}; {c['new_value']} from {c['source']} was {reason}.")
        if apply and closed:
            if updates.get("forwarder_name"):
                row = resolve_mapping(mappings, "forwarder", name=updates["forwarder_name"])
                if row:
                    updates["forwarder_id"] = row["partner_id"]
            if updates:
                odoo.write(BOOKING, [booking_id], updates)
            odoo.write(CONFLICT, closed, {"state": "resolved"})
            odoo.call(BOOKING, "message_post", [booking_id],
                      body="Conflicts closed by the OCR service's updated rules:<br/>" + "<br/>".join(notes))
    return outcome


# ── bills ────────────────────────────────────────────────────────────

def _settings(db):
    return {key: store.get_setting(db, key) for key in SETTING_KEYS}


def reread_ready_bills(db, odoo, connection_id, apply=False, limit=None):
    """The invoices behind the Ready-to-Create-PO bills, read again: their lines get part numbers and products."""
    settings = _settings(db)
    outcome = Counter()
    bills = odoo.search_read(SCRAPED_BILL, [("state", "=", "ready_for_po")], ["id", "company_id"], limit=limit)
    matchers = {}
    for bill in bills:
        company_id = bill["company_id"][0]
        matcher = matchers.setdefault(company_id, BillMatcher(odoo, company_id, []))
        files = db.execute(
            "SELECT f.id, f.path, f.message_id FROM file f JOIN file_target t ON t.file_id = f.id AND t.connection_id = ? "
            "JOIN message m ON m.id = f.message_id JOIN mailbox b ON b.id = m.mailbox_id AND b.purpose = 'bill_matching' "
            "WHERE t.odoo_id = ? AND f.doc_class = 'supplier_invoice' ORDER BY f.id", (connection_id, bill["id"])).fetchall()
        if not files:
            outcome["no invoice file kept"] += 1
            continue
        if not apply:
            outcome["would be read again"] += 1
            continue
        for file in files:
            with open(file["path"], "rb") as handle:
                doc_class, values = extract.read_file(handle.read(), "bill_matching", settings)
            if doc_class != "supplier_invoice" or not values.get("lines"):
                continue
            db.execute("UPDATE file SET extracted = ? WHERE id = ?", (json.dumps(values), file["id"]))
            db.commit()  # the AI is paid for
            with _source(odoo, file["message_id"], file["id"]):
                matcher.add_lines(bill["id"], values["lines"], replace=True)
            lines = odoo.search_read("b2b.ocr.scraped.bill.line", [("bill_scrape_id", "=", bill["id"])], ["product_id"])
            linked = sum(1 for line in lines if line.get("product_id"))
            outcome["bills read again"] += 1
            outcome["lines"] += len(lines)
            outcome["lines linked to a product"] += linked
            break
        else:
            outcome["no lines found when read again"] += 1
    return outcome


# ── freight ──────────────────────────────────────────────────────────

def freight_from_bills(db, odoo, connection_id, apply=False):
    """The forwarders' invoices already in Bill Matching: their freight cost goes to their bookings."""
    run = _Run(db, odoo, connection_id, _settings(db))
    outcome = Counter()
    for row in db.execute(
            "SELECT f.id, f.filename, f.extracted, m.id AS message_id, m.sender, m.subject, m.received, b.company_id "
            "FROM file f JOIN message m ON m.id = f.message_id JOIN mailbox b ON b.id = m.mailbox_id "
            "AND b.purpose = 'bill_matching' JOIN file_target t ON t.file_id = f.id AND t.connection_id = ? "
            "WHERE f.doc_class = 'supplier_invoice' AND t.state = 'done' "
            "AND f.id NOT IN (SELECT file_id FROM file_freight_reading)", (connection_id,)).fetchall():
        values = json.loads(row["extracted"] or "{}")
        domain = (row["sender"] or "").rpartition("@")[2]
        if not (values.get("master_bl") or values.get("house_bl") or values.get("containers")) or resolve_mapping(
                run.mappings, "factory", sender_domain=domain, name=values.get("vendor_name")):
            continue
        outcome["forwarder or carrier invoices"] += 1
        if not apply:
            continue
        doc = {"sender": row["sender"] or "", "subject": row["subject"] or "", "received": row["received"],
               "doc_class": "supplier_invoice", "values": values, "source": row["filename"], "file_id": row["id"]}
        with _source(odoo, row["message_id"], row["id"]):
            run.freight_cost_from({"company_id": row["company_id"]}, doc)
        reading = db.execute("SELECT doc_class FROM file_freight_reading WHERE file_id = ?", (row["id"],)).fetchone()
        outcome[f"read as {reading['doc_class'] if reading else 'nothing'}"] += 1
    return outcome
