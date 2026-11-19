"""The bookings status workbook (Import / Export): every freight booking in the Odoo connections that are on, one
sheet per connection, with its state, what is still missing, B/Ls, factory invoices and a link to it."""

import io

from openpyxl import Workbook
from openpyxl.styles import Font


BOOKING_COLUMNS = (("Booking", "booking_number"), ("State", "state"), ("Still missing", "missing_fields"),
                   ("Open conflicts", "open_conflict_count"), ("Forwarder", "forwarder_id"),
                   ("Factory", "factory_id"), ("Deliver To", "warehouse_id"), ("MBL", "master_bl"),
                   ("MBL from", "master_bl_source"), ("HBL", "house_bl"), ("Vessel", "vessel"), ("Voyage", "voyage"),
                   ("Port of loading", "port_of_loading"), ("Port of discharge", "port_of_discharge"),
                   ("Rail ramp", "rail_ramp"), ("ETD", "etd"), ("ETA", "eta"), ("Containers", "container_qty"),
                   ("Rate per container", "effective_cost_per_container"), ("Freight PO", "purchase_order_id"),
                   ("Shipment reference", "shipment_reference"))
STATE_NAMES = {"incomplete": "Incomplete", "vendor_mapping_required": "Vendor Mapping Required",
               "conflict_detected": "Conflict", "ready_for_review": "Ready for Review", "ready_for_po": "Ready for PO",
               "po_matched": "PO Already Existing", "po_created": "PO Created"}


def bookings_workbook(connections):
    """[(connection name, odoo)] -> the bookings status workbook as bytes: one sheet per Odoo connection."""
    wb = Workbook()
    wb.remove(wb.active)
    for name, odoo in connections:
        ws = wb.create_sheet(name[:31])
        fields = odoo.call("b2b.ocr.booking", "fields_get", attributes=["type"])
        columns = [(label, f) for label, f in BOOKING_COLUMNS if f in fields]
        rows = odoo.search_read("b2b.ocr.booking", [], [f for _l, f in columns] + ["invoice_ids"], order="state, etd")
        invoices = {}
        ids = [i for r in rows for i in r["invoice_ids"]]
        for i in range(0, len(ids), 500):
            for line in odoo.call("b2b.ocr.booking.invoice", "read", ids[i:i + 500], ["booking_id", "invoice_number"]):
                invoices.setdefault(line["booking_id"][0], []).append(line["invoice_number"])
        ws.append([label for label, _f in columns] + ["Factory invoices", "Odoo link"])
        for cell in ws[1]:
            cell.font = Font(name="Arial", bold=True)
        for r in rows:
            values = []
            for _label, f in columns:
                v = r.get(f)
                if isinstance(v, list):
                    v = v[1] if len(v) > 1 else v[0]
                if f == "state":
                    v = STATE_NAMES.get(v, v)
                values.append(None if v is False else v)
            link = f"{odoo.url}/web#model=b2b.ocr.booking&id={r['id']}&view_type=form"
            ws.append(values + [", ".join(invoices.get(r["id"], [])) or None, link])
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions
        for i in range(1, ws.max_column + 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = 18
    if not wb.sheetnames:
        wb.create_sheet("No Odoo connection on")
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
