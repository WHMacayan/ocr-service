"""Booking, B/L, container and invoice numbers: how the team writes them, and when two readings are one number."""

import re

from app.shared.documents.text import normalize_identifier


# Carrier codes that start bills and bookings: ONEYSELGE0071900 is booking SELGE0071900.
_SCAC = re.compile(r"^(ONEY|MEDU|MSCU|HLCU|HDMU|MAEU|COSU|CMDU|EGLV|OOLU|YMLU|YMJA|ZIMU|WHLC|EMIV|SMLM|KMTC|SUDU|APLU)")


# Neighbouring ports stay apart on purpose (Los Angeles / Long Beach, Ho Chi Minh City / Vung Tau): the terminal and
# the trucking differ, so a document naming the other one is a disagreement for a person.
# The factories' invoice numbers (Sentury F26AR10THA0693 / F26ARS09THA180, General Rubber GRKA-P-2026436 written
# without dashes): read where a B/L goes, never a B/L.
_INVOICE_NUMBER = re.compile(r"^(F\d{2}ARS?\d{2}[A-Z]{2,3}\d{3,4}|GRKA[A-Z]{0,2}\d{7})")


# A container number (ONEU0312535) is never a bill of lading number.
_CONTAINER_NUMBER = re.compile(r"[A-Z]{3}[UJZ]\d{7}")


def is_container_number(text):
    return bool(_CONTAINER_NUMBER.fullmatch(normalize_identifier(text) or ""))


# How the team writes B/L numbers on its POs (scorecard against 209 team POs, 17 Nov 2026):
# KLN prints its house bills with its code in front (KFUNA2611310408); the team writes A2611310408.
_HOUSE_PREFIX = re.compile(r"^KFUN(?=A\d{8,}$)")


# The forwarders' own house bills, in their file names: De Well's drafts "HBL#SBKKS0027412", Tianshi's cargo receipts
# THSE26100423 (Thailand) and VNSE26110149 (Vietnam). The team's POs carry them as the HBL (SBKKS 2,625, THSE 1,811,
# VNSE 202 of 15,763 freight POs).
_HOUSE_IN_NAME = re.compile(r"HBL#\s*([A-Z0-9]{8,20})|(?<![A-Z0-9])((?:THSE|VNSE)\d{8})(?!\d)", re.I)


# Tianshi's cargo receipt names its booking: DRAFT-13204963-THSE26100423_TS FCR (DFCR).PDF.
_BOOKING_IN_NAME = re.compile(r"^DRAFT-([A-Z0-9]{8,14})-(?:THSE|VNSE)\d{8}", re.I)


def clean_bl(number):
    """A B/L number as the team writes it: KLN's code dropped from its house bill, and a Hapag bill's last O or S read
    as the 0 or 5 it is (none of 7,288 Hapag B/Ls on the team's POs ends in O or S; five bookings had one)."""
    number = _HOUSE_PREFIX.sub("", normalize_identifier(number))
    if number.startswith("HLCU") and len(number) >= 14 and number[-1] in "OS":
        number = number[:-1] + {"O": "0", "S": "5"}[number[-1]]
    return number


def house_bill_in_name(name):
    found = _HOUSE_IN_NAME.search(name or "")
    return normalize_identifier(found.group(1) or found.group(2)) if found else ""


# Characters a reading mixes up: one of these pairs is a misreading, two other digits are another number.
_LOOK_ALIKE = [set(pair) for pair in ("0O", "0D", "0Q", "1I", "1L", "5S", "8B", "2Z", "6G", "UV")]


def reference_number(text):
    """The booking number a PO's Vendor Reference holds: 31978034-2 (one PO of a split) is 31978034; the
    carrier code is dropped, as on the booking."""
    first = re.split(r"[\s/]|-(?=\d{1,2}$)", (text or "").strip())[0] if text else ""
    return _SCAC.sub("", normalize_identifier(first))


def same_identifier(a, b):
    """Booking and bill numbers that are the same apart from the carrier code before them, or one
    character read wrong, lost or added (from the tenth character: short numbers must match exactly)."""
    a, b = _SCAC.sub("", normalize_identifier(a)), _SCAC.sub("", normalize_identifier(b))
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 9 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        # Only a look-alike character read wrong: EBKG18500445 and EBKG18500447 are two bookings in a row.
        diffs = [{x, y} for x, y in zip(a, b) if x != y]
        return len(diffs) == 1 and diffs[0] in _LOOK_ALIKE
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))
