"""One-off passes over what is already in Odoo, after the booking and bill rules changed:

- conflicts: close the open booking conflicts the rules now settle (the same value written another
  way, a later document moving the schedule, an older one, the feeder beside the ocean vessel, the
  carrier beside the forwarder, an impossible value); the rest stay for a person;

Nothing is written unless ``apply``; the pass returns what it found, counted by reason.
"""

import logging
import re
from collections import Counter, defaultdict

from ..core import store
from ..documents.text import file_text, normalize_identifier
from .freight import (BOOKING, CARRIER, CONFLICT, UPDATED_BY_CONFIRMATION, _READ, plausible_date, plausible_vessel,
                      resolve_mapping, same_value)
from .pipeline import BookingHistory

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
