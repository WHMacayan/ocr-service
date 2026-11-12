"""What one file, or a freight email's own subject and body, states.

A file's text is read once and the AI asked once; only the identifiers and
dates the text really prints are kept. Port of b2b_freight_ocr's document
reading and the mailbox's email values.
"""

import re
from datetime import date

from . import ai
from .text import (appears_in, booking_number_in_subject, date_appears_in, file_text, find_container_numbers,
                   find_house_bl, find_master_bl, is_container_number, normalize_identifier, parse_month_date)

FREIGHT_CLASSES = (
    "booking_confirmation", "commercial_invoice", "packing_list", "mbl_hbl", "shipping_notice",
    "freight_invoice", "customs_document", "delivery_document", "statement", "not_freight", "unknown")
NOT_RELEVANT = ("statement", "not_freight", "unknown")
# Only a commercial invoice becomes a scraped bill; the other types are read to be told apart from it.
INVOICE_TYPES = ("commercial_invoice", "freight_invoice", "service_invoice", "shipping_notice", "other")
_TYPE_NAMES = {"commercial_invoice": "commercial invoice", "freight_invoice": "freight invoice",
               "service_invoice": "service invoice", "shipping_notice": "shipping notice", "other": "other document"}


def _nullable(kind):
    return {"type": [kind, "null"]}


FREIGHT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "doc_class": {"type": "string", "enum": list(FREIGHT_CLASSES)},
        **{name: _nullable("string") for name in (
            "booking_number", "master_bl", "house_bl", "vessel", "voyage", "pre_carrier_vessel",
            "pre_carrier_voyage", "port_of_loading", "port_of_discharge", "etd", "eta", "forwarder_name",
            "factory_name", "currency")},
        "containers": {"type": "array", "items": {"type": "string"}},
        "invoices": {"type": "array", "items": {"type": "string"}},
        "freight_cost": _nullable("number"),
        "additional_charges": _nullable("number"),
    },
}
INVOICE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "document_type": {"type": "string", "enum": list(INVOICE_TYPES)},
        "document_name": _nullable("string"),
        **{name: _nullable("string") for name in (
            "vendor_name", "vendor_vat", "invoice_number", "invoice_date", "currency",
            "master_bl", "house_bl")},
        "amount_total": _nullable("number"),
        "po_numbers": {"type": "array", "items": {"type": "string"}},
        "containers": {"type": "array", "items": {"type": "string"}},
    },
}
for _schema in (FREIGHT_SCHEMA, INVOICE_SCHEMA):
    _schema["required"] = list(_schema["properties"])

SUBJECT_GROUPS = ("booking_number", "master_bl", "house_bl", "invoice_number", "etd", "eta")


def read_file(raw, purpose, settings):
    """``(doc_class, values)`` of a saved file; ``(None, {})`` when it has no text.

    ``settings`` maps setting keys to values (see defaults.DEFAULT_SETTINGS).
    """
    text = file_text(raw, int(settings["max_pages"]))
    if not text.strip():
        return None, {}
    freight = purpose == "freight_booking"
    parsed = ai.extract(
        settings["openai_api_key"], settings["openai_model"],
        settings["freight_prompt" if freight else "invoice_prompt"],
        text[:int(settings["max_chars"])], FREIGHT_SCHEMA if freight else INVOICE_SCHEMA)
    values = checked(parsed, text)
    if freight:
        return values.get("doc_class") or "unknown", values
    return ("supplier_invoice" if values.get("document_type") == "commercial_invoice" else "not_invoice"), values


# Names that say nothing of what the file is: its type is named instead.
_VAGUE_NAMES = {"", "invoice", "document", "unknown", "other", "n/a", "none"}


def document_name(values):
    """What a file read for Scraped Bills is, in a few plain words: the AI's name for it, else its type."""
    values = values or {}
    name = " ".join(str(values.get("document_name") or "").split()).rstrip(".")
    return name if name.lower() not in _VAGUE_NAMES else _TYPE_NAMES.get(values.get("document_type"), "unidentified document")


def not_invoice_note(values):
    """Why a file read for Scraped Bills makes no bill: "Not a commercial invoice: <what it is>"."""
    return f"Not a commercial invoice: {document_name(values)}"


def checked(parsed, text):
    """Identifiers and dates the document does not print are dropped: model
    output is a reading, not a fact."""
    values = {key: value for key, value in parsed.items() if value not in (None, "", [])}
    for key in ("booking_number", "master_bl", "house_bl", "invoice_number", "vendor_vat"):
        if key in values and not appears_in(text, values[key]):
            values.pop(key)
    for key in ("containers", "invoices", "po_numbers"):
        if key in values:
            values[key] = [normalize_identifier(v) for v in values[key] if appears_in(text, v)
                           and (key != "containers" or is_container_number(v))]
    for key in ("etd", "eta", "invoice_date"):
        if key in values and not date_appears_in(text, values[key]):
            values.pop(key)
    return values


def read_subject(formats, subject, sender_domain):
    """``(values, confirms_booking)`` from the first active subject format that
    applies to the sender and matches the subject. ``formats`` are rows in
    sequence order."""
    for row in formats:
        domains = {d.strip().lower() for d in (row["sender_domains"] or "").split(",") if d.strip()}
        if domains and (sender_domain or "").lower() not in domains:
            continue
        match = re.search(row["subject_pattern"], subject or "", re.I)
        if match:
            return ({k: v.strip() for k, v in match.groupdict().items() if v and v.strip()},
                    bool(row["confirms_booking"]))
    return {}, False


def email_values(formats, booking_number_pattern, subject, body, sender, received):
    """What a freight email's subject and newest message state, without AI.
    ``received`` is an ISO datetime string."""
    read, confirms = read_subject(formats, subject, sender.rpartition("@")[2])
    received_day = date.fromisoformat(received[:10]) if received else None
    values = {}
    for key, value in (
        ("booking_number", normalize_identifier(read.get("booking_number"))
         or booking_number_in_subject(subject, booking_number_pattern)),
        ("master_bl", normalize_identifier(read.get("master_bl"))
         or find_master_bl(subject) or find_master_bl(body)),
        ("house_bl", normalize_identifier(read.get("house_bl"))
         or find_house_bl(subject) or find_house_bl(body)),
        ("etd", parse_month_date(read.get("etd"), received_day)),
        ("eta", parse_month_date(read.get("eta"), received_day)),
        ("invoices", [normalize_identifier(read["invoice_number"])] if read.get("invoice_number") else []),
        ("containers", find_container_numbers(f"{subject}\n{body}")),
    ):
        if value:
            values[key] = value
    if values and confirms:
        values["confirms_booking"] = True
    return values
