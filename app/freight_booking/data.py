"""Freight Booking's data sets: the bookings, as Odoo holds them. An import updates a booking (matched by its Odoo ID or its
booking number) or adds one; its containers, factory invoices and bills of lading are exported as lists and not imported
(they come from the shipping documents)."""

from app.freight_booking import compare
from app.shared.data.odoo import OdooDataSet

BOOKINGS = OdooDataSet(
    "bookings", "Bookings", "booking", compare.BOOKING, lambda ctx, filters: compare.booking_ids(ctx.db),
    "web.booking_list", match=("booking_number",), importable=True, creatable=True,
    default=("booking_number", "state", "forwarder_id", "factory_id", "warehouse_id", "master_bl", "house_bl", "vessel", "voyage",
             "port_of_loading", "port_of_discharge", "etd", "eta", "container_ids", "invoice_ids", "cost_per_container",
             "additional_charges", "freight_mode", "missing_fields"),
    children={"container_ids": ("b2b.ocr.booking.container", "container_no"),
              "invoice_ids": ("b2b.ocr.booking.invoice", "invoice_number"),
              "bl_ids": ("b2b.ocr.booking.bl", "bl_number")})

DATASETS = (BOOKINGS,)
