"""The same value written another way (a date read day-first, a vessel with its operator), and impossible values (a date far from the email, a charge read as a vessel)."""

import re
from datetime import date, datetime, timedelta

from app.shared.odoo.mappings import resolve_mapping
from app.freight_booking.rules.parties import party_key
from app.freight_booking.rules.ports import port_key
from app.freight_booking.rules.words import _OPERATORS, _PLACES, _words


def swapped_date(a, b, days=10):
    """Whether date ``b`` is a date near ``a`` with its day and month read the other way round: 2026-08-10 for
    8 October beside 7 October (Tianshi's day-first cargo receipts moved an ETD two months back, Nov 2026)."""
    try:
        x, y = date.fromisoformat(str(a)[:10]), date.fromisoformat(str(b)[:10])
        if x == y or y.day > 12:
            return False
        return abs((y.replace(month=y.day, day=y.month) - x).days) <= days
    except (TypeError, ValueError):
        return False


def vessel_key(text):
    """A vessel by its own name: MAERSK MEMPHIS and HAPAG-LLOYD MEMPHIS are MEMPHIS."""
    return "".join(w for w in _words(text) if w not in _OPERATORS)


def _contains(a, b):
    return bool(a and b) and min(len(a), len(b)) >= 4 and (a in b or b in a)


def same_value(name, old, new, mappings=()):
    """Whether two values of a booking field name the same thing, written another way."""
    if name in ("port_of_loading", "port_of_discharge"):
        return _contains(port_key(old), port_key(new))
    if name in ("forwarder_name", "factory_name"):
        role = name.split("_")[0]
        a, b = resolve_mapping(mappings, role, name=old), resolve_mapping(mappings, role, name=new)
        if a and b and a["partner_id"] == b["partner_id"]:
            return True  # two printed names of one Odoo vendor (an origin agent and the forwarder it works for)
        return _contains(party_key(old), party_key(new))
    if name == "vessel":
        return vessel_key(old) == vessel_key(new) != ""
    return False


# ── impossible values ────────────────────────────────────────────────

# A date further than this from the email that brought it is a misreading.
# A schedule document comes before the vessel sails or soon after it arrives, never months after the departure.
_DATE_WINDOW = {"etd": (timedelta(days=100), timedelta(days=200)), "eta": (timedelta(days=90), timedelta(days=270))}


def plausible_date(name, iso, received):
    """The date as it can be for a document received then, else None. A date with its day and month
    read the wrong way round (2026-06-11 for 11 June when 6 November fits) is turned back."""
    if not iso:
        return None
    try:
        day = date.fromisoformat(iso)
        sent = datetime.fromisoformat(str(received).replace("T", " ").rstrip("Z")).date()
    except (TypeError, ValueError):
        return iso
    before, after = _DATE_WINDOW[name]
    fits = lambda d: sent - before <= d <= sent + after
    if fits(day):
        return iso
    try:
        swapped = day.replace(month=day.day, day=day.month)
    except ValueError:
        return None
    return swapped.isoformat() if fits(swapped) else None


_NOT_A_VESSEL = re.compile(r"DEMURRAGE|DETENTION|CHARGE|INVOICE|TERMINAL|\bPORT\b|\bTBA\b|\bTBN\b|\bN/?A\b", re.I)


def plausible_vessel(text):
    """Not a vessel: a port with its UN/LOCODE (SGSIN SINGAPORE: the code's city part starts the name
    after it, unlike a vessel such as MAPLE LEAF), a charge, a terminal."""
    words = _words(text)
    if not words or _NOT_A_VESSEL.search(text or ""):
        return False
    locode = len(words) > 1 and re.fullmatch(r"[A-Z]{2}[A-Z]{3}", words[0]) and words[0][:2] in _PLACES
    return not (locode and words[1][0] == words[0][2])
