"""What Scraped Bills asks the AI to read in a supplier's file: only a commercial invoice becomes a scraped bill; the
other types are read to be told apart from it."""

from app.shared.documents.extract import Reading, nullable

INVOICE_TYPES = ("commercial_invoice", "freight_invoice", "service_invoice", "shipping_notice", "other")
_TYPE_NAMES = {"commercial_invoice": "commercial invoice", "freight_invoice": "freight invoice",
               "service_invoice": "service invoice", "shipping_notice": "shipping notice", "other": "other document"}

INVOICE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "document_type": {"type": "string", "enum": list(INVOICE_TYPES)},
        "document_name": nullable("string"),
        **{name: nullable("string") for name in (
            "vendor_name", "vendor_vat", "invoice_number", "invoice_date", "currency",
            "master_bl", "house_bl")},
        "amount_total": nullable("number"),
        "po_numbers": {"type": "array", "items": {"type": "string"}},
        "containers": {"type": "array", "items": {"type": "string"}},
    },
}
INVOICE_SCHEMA["required"] = list(INVOICE_SCHEMA["properties"])

READING = Reading("invoice_prompt", INVOICE_SCHEMA,
                  lambda values: "supplier_invoice" if values.get("document_type") == "commercial_invoice" else "not_invoice")

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
