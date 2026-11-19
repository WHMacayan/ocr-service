"""The Odoo models a booking is made of, and which document types state which values."""



BOOKING = "b2b.ocr.booking"


CONTAINER = "b2b.ocr.booking.container"


INVOICE = "b2b.ocr.booking.invoice"


BL = "b2b.ocr.booking.bl"


CONFLICT = "b2b.ocr.booking.conflict"


CHANGE = "b2b.ocr.booking.container.change"


_SCHEDULE = ("booking_confirmation", "mbl_hbl", "shipping_notice")


# The factory's own papers. They start a booking and fill its shipment block, but never dispute a value
# already held: the carrier's and forwarder's documents are the source of the schedule.
FACTORY_DOCS = ("commercial_invoice", "packing_list")


# Which document types state which values. Every type prints other parties'
# details, so a value is taken only from a type whose job is to state it.
TRUSTED = {
    "schedule": _SCHEDULE,
    "factory_schedule": FACTORY_DOCS,
    "email_schedule": ("email_message",),
    "bl": _SCHEDULE + ("freight_invoice", "email_message"),
    "forwarder": _SCHEDULE + ("freight_invoice",),
    "factory": ("commercial_invoice", "packing_list", "mbl_hbl"),
    "containers": _SCHEDULE + FACTORY_DOCS + ("freight_invoice", "email_message"),
    # A packing list prints the order number where an invoice number would be: never an invoice.
    "invoices": ("commercial_invoice", "email_message"),
    "amounts": ("freight_invoice",),
    # Fills a booking that started without a number (from a factory invoice); never replaces one.
    "booking_number": _SCHEDULE + FACTORY_DOCS + ("freight_invoice",),
    "reference": FACTORY_DOCS,
}


# A booking still open for documents: no freight PO drafted or linked yet. A container is reused
# shipment after shipment, and a sailing carries many shipments: only open bookings are matched by them.
OPEN = [("purchase_order_id", "=", False), ("state", "not in", ["po_created", "po_matched"])]


# A newer booking confirmation replaces these: carriers reissue them as the schedule moves.
UPDATED_BY_CONFIRMATION = ("vessel", "voyage", "etd", "eta")


SCHEDULE_FIELDS = ("vessel", "voyage", "port_of_loading", "port_of_discharge", "etd", "eta")


_READ = (
    "booking_number", "forwarder_id", "forwarder_name", "factory_id", "factory_name", "master_bl",
    "master_bl_confirmed", "master_bl_assumed", "house_bl", "house_bl_confirmed", "house_bl_assumed",
    *SCHEDULE_FIELDS, *(f"{name}_source" for name in UPDATED_BY_CONFIRMATION),
    "freight_currency_id", "freight_cost", "additional_charges", "freight_mode",
    "freight_product_id", "mapping_freight_product_id", "purchase_order_id", "warehouse_id", "shipment_reference",
)
