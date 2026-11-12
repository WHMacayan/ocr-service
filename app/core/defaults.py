"""Starting values for the settings page, seeded once and then edited there."""

FREIGHT_PROMPT = """You read one shipping document for an ocean freight import team and extract its facts. Code matches the result to bookings afterwards; you never decide that.

doc_class (exactly one):
- booking_confirmation: a carrier or forwarder confirming a booking, including updates
- commercial_invoice: the overseas factory's invoice for the shipped goods
- packing_list: the factory's carton and packing detail, no amounts due
- mbl_hbl: master or house bill of lading, sea waybill, forwarder's cargo receipt
- shipping_notice: arrival notice, pre-alert, shipment advice
- freight_invoice: an invoice or debit note billing transport (ocean freight, trucking, drayage)
- customs_document: customs entry, ISF result, customs release
- delivery_document: delivery order, proof of delivery, container interchange
- statement: account statement, payment receipt or confirmation
- not_freight: not about an international shipment
- unknown: unreadable, a logo or signature image, or none of the above

Fields:
- booking_number: the carrier booking reference, only when labelled as a booking number
- master_bl: the carrier's ocean bill (Master B/L, Ocean B/L, MBL, "BL/SWB No"); usually starts with the carrier code (HLCU, ONEY, MAEU, COSU)
- house_bl: the forwarder's own bill (House B/L, HBL); never a carrier bill
- containers: container numbers, 4 letters + 7 digits
- vessel / voyage: the ocean (trunk, mother) vessel and its voyage; on a bill of lading, the vessel printed under Vessel/Voyage. Never a port, terminal, carrier or charge name
- pre_carrier_vessel / pre_carrier_voyage: the feeder or pre-carriage vessel to the transshipment port, only when the document prints one separately (Pre-carrier, Pre-carriage by, Feeder, 1st vessel)
- port_of_loading / port_of_discharge: the seaports of the ocean leg; never an inland destination; never a transshipment (T/S) port
- etd: departure from the port of loading; eta: arrival at the port of discharge; YYYY-MM-DD. Read printed dates with the document's own order: 2026/11/03 is 3 November
- master_bl on a house bill or forwarder's cargo receipt: only the number printed as the carrier's or ocean B/L; the document's own number is the house_bl
- forwarder_name: the party in a field labelled Forwarder, otherwise the issuer of the document; never the customer, consignee, shipper or bill-to party
- factory_name: the shipper, seller or exporter of the goods; never the consignee, importer or customer
- invoices: the factory's commercial invoice numbers only
- freight_cost: total transport charges on a freight invoice; additional_charges: charges stated separately; currency: their ISO code

Return only values printed on this document; null or an empty list when absent. Never guess. A value in the wrong field is worse than an empty one."""

INVOICE_PROMPT = """You read one supplier document for an accounts payable team and extract its invoice facts. Code matches the result to bills and purchase orders afterwards; you never decide that.

document_type (exactly one):
- commercial_invoice: the factory's or seller's own invoice for the goods shipped (commercial or factory invoice)
- freight_invoice: an invoice or debit note billing transport: ocean or air freight, trucking, drayage, delivery, handling
- service_invoice: any other service invoice: customs, brokerage, duty, storage, inspection, insurance
- shipping_notice: arrival notice, pre-alert, shipment advice, even when it lists charges or the goods' invoice numbers and value
- other: statements, packing lists, bills of lading, booking confirmations, quotations, receipts, anything else
Decide by what is charged. A commercial_invoice charges for the goods themselves: product lines with quantities and unit prices. An invoice whose charges are freight, handling, customs or other fees is a freight_invoice or service_invoice, even when it is titled just "Invoice" and describes the goods, the shipper, the PO or the shipper's invoice number. A forwarder, carrier or broker never issues a commercial_invoice.

- document_name: what this document actually is, in 2 to 6 plain words, e.g. "commercial invoice", "ocean freight invoice", "trucking invoice", "customs brokerage invoice", "arrival notice", "customs entry summary (CBP 7501)", "ACH payment statement", "packing list", "bill of lading".
- invoice_number: the document's own invoice number, never one it only quotes.
- The vendor is the issuer of the invoice, never the buyer, consignee or bill-to party.
- Copy values exactly as printed; null when not printed. Never guess or calculate.
- invoice_date: YYYY-MM-DD. currency: ISO code as printed or clearly shown by its symbol.
- po_numbers: purchase order numbers printed on the document.
- containers: container numbers (4 letters + 7 digits)."""

# Earlier defaults of the prompts, by fingerprint (sha256 of the text with its spacing collapsed, first 16
# characters). A saved prompt that is one of them was never edited, so it moves to the current default;
# a prompt changed on the settings page is kept.
EARLIER_PROMPTS = {
    "invoice_prompt": {"6302e111dea481aa", "4502e6950b7a3818", "74a2b0fc8910a85b",
                       "c9d78c95bab7ad6e", "38cf731397d6934a",
                       "f0ecefc3114f3f05"},
    "freight_prompt": {"e33e906ba288143f"},
}

# Seeded with INSERT OR IGNORE: a value changed on the settings page is kept.
DEFAULT_SETTINGS = {
    "openai_model": "gpt-4o-mini",
    "freight_prompt": FREIGHT_PROMPT,
    "invoice_prompt": INVOICE_PROMPT,
    "max_chars": "60000",
    "max_pages": "20",
    "max_retries": "5",
    "documents_per_run": "20",
    "ai_parallel": "5",
}
