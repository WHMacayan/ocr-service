"""What Freight Booking asks the AI to read in a shipping document, and what an email's own subject and body state
without AI (the Subject Formats on the setup pages)."""

import re
from datetime import date

from app.shared.documents.extract import Reading, nullable
from app.shared.documents.text import (booking_number_in_subject, find_container_numbers, find_house_bl,
                                       find_master_bl, normalize_identifier, parse_month_date)

FREIGHT_CLASSES = (
    "booking_confirmation", "commercial_invoice", "packing_list", "certificate_of_origin", "mbl_hbl",
    "shipping_notice", "freight_invoice", "customs_document", "delivery_document", "statement", "not_freight",
    "unknown")
# A certificate of origin quotes the goods' invoice number but is no invoice: it adds nothing to a booking.
NOT_RELEVANT = ("certificate_of_origin", "statement", "not_freight", "unknown")

FREIGHT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "doc_class": {"type": "string", "enum": list(FREIGHT_CLASSES)},
        **{name: nullable("string") for name in (
            "booking_number", "master_bl", "house_bl", "bl_number", "shipment_reference", "rail_ramp", "vessel", "voyage",
            "pre_carrier_vessel",
            "pre_carrier_voyage", "port_of_loading", "port_of_discharge", "etd", "eta", "forwarder_name",
            "factory_name", "currency")},
        "containers": {"type": "array", "items": {"type": "string"}},
        "invoices": {"type": "array", "items": {"type": "string"}},
        "freight_cost": nullable("number"),
        "additional_charges": nullable("number"),
    },
}
FREIGHT_SCHEMA["required"] = list(FREIGHT_SCHEMA["properties"])

READING = Reading("freight_prompt", FREIGHT_SCHEMA, lambda values: values.get("doc_class") or "unknown")

SUBJECT_GROUPS = ("booking_number", "master_bl", "house_bl", "invoice_number", "etd", "eta")


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
