"""The service's own record of each freight booking, set against what Odoo holds now.

The service's side is rebuilt from odoo_change: per field, the last value the service wrote and the
email or file it came from. Odoo's side is read live. A field differs when Odoo now holds something
other than what the service last wrote: a person changed it, or something else did.
"""

import json

from .store import reference_changes

BOOKING = "b2b.ocr.booking"

# The Odoo form's sections, in its order.
SECTIONS = [
    ("Parties", ["freight_mode", "forwarder_id", "forwarder_name", "factory_id", "factory_name", "warehouse_id"]),
    ("Shipment", ["shipment_reference", "master_bl", "master_bl_confirmed", "house_bl", "house_bl_confirmed",
                  "vessel", "voyage"]),
    ("Route", ["port_of_loading", "port_of_discharge", "etd", "eta"]),
    ("Freight", ["container_qty", "cost_per_container", "mapping_cost_per_container", "effective_cost_per_container",
                 "freight_currency_id", "freight_cost", "additional_charges", "freight_product_id",
                 "mapping_freight_product_id", "purchase_order_id"]),
]
STATUS_FIELDS = ["booking_number", "state", "review_state", "missing_fields", "open_conflict_count",
                 "open_container_change_count", "has_uncertain_match"]
CHILDREN = {
    "containers": ("b2b.ocr.booking.container", ["container_no", "container_type", "quantity", "state", "confirmed", "source"]),
    "bls": ("b2b.ocr.booking.bl", ["bl_number", "bl_type", "confirmed", "source"]),
    "invoices": ("b2b.ocr.booking.invoice", ["invoice_number", "invoice_date", "amount", "currency_id", "confirmed", "source"]),
    "conflicts": ("b2b.ocr.booking.conflict", ["field_label", "existing_value", "new_value", "state", "source"]),
    "changes": ("b2b.ocr.booking.container.change", ["new_container_lines", "state", "source"]),
}
ALL_FIELDS = [f for _title, fields in SECTIONS for f in fields] + STATUS_FIELDS
_fields_cache = {}


def booking_ids(db):
    """Every booking the service has touched: recorded changes, and files from before they were recorded."""
    mine, args = reference_changes(db)
    ids = {r[0] for r in db.execute(f"SELECT DISTINCT record_id FROM odoo_change c WHERE model = ? AND {mine}",
                                    (BOOKING, *args))}
    ids |= {r[0] for r in db.execute(
        "SELECT DISTINCT f.odoo_id FROM file f JOIN message m ON m.id = f.message_id JOIN mailbox b "
        "ON b.id = m.mailbox_id WHERE f.odoo_id IS NOT NULL AND b.purpose = 'freight_booking'")}
    return ids


def service_values(db, booking_ids_):
    """{booking id: {field: {value, source, at, message_id}}}: the last value the service wrote per field."""
    if not booking_ids_:
        return {}
    marks = ", ".join("?" * len(booking_ids_))
    mine, args = reference_changes(db)
    out = {}
    for r in db.execute(
            f"SELECT c.record_id, c.values_json, c.at, c.message_id, f.filename, m.subject FROM odoo_change c "
            f"JOIN message m ON m.id = c.message_id LEFT JOIN file f ON f.id = c.file_id "
            f"WHERE c.model = ? AND c.record_id IN ({marks}) AND c.values_json IS NOT NULL AND {mine} ORDER BY c.id",
            (BOOKING, *booking_ids_, *args)):
        for field, value in json.loads(r["values_json"]).items():
            out.setdefault(r["record_id"], {})[field] = {
                "value": value, "at": r["at"], "message_id": r["message_id"],
                "source": r["filename"] or f"Email text: {r['subject'] or 'no subject'}"}
    return out


def emails(db, booking_id):
    """The emails that fed this booking, newest first."""
    mine, args = reference_changes(db)
    return db.execute(
        "SELECT DISTINCT m.id, m.subject, m.sender, m.received, m.state FROM message m WHERE m.id IN ("
        f" SELECT message_id FROM odoo_change c WHERE model = ? AND record_id = ? AND {mine}"
        " UNION SELECT f.message_id FROM file f WHERE f.odoo_id = ?) ORDER BY m.received DESC",
        (BOOKING, booking_id, *args, booking_id)).fetchall()


def fields_info(odoo, model):
    if model not in _fields_cache:
        _fields_cache[model] = odoo.call(model, "fields_get", attributes=["string", "type", "selection"])
    return _fields_cache[model]


def shown(value, info):
    """A value as people read it: names for links, labels for choices, nothing for empty."""
    if value is False or value is None or value == "":
        return ""
    kind = (info or {}).get("type")
    if isinstance(value, list) and len(value) == 2:  # many2one from Odoo
        return value[1]
    if kind == "selection":
        return dict(info.get("selection") or []).get(value, value)
    if kind == "boolean":
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def same(service, odoo_value, info):
    """Whether Odoo still holds what the service wrote."""
    kind = (info or {}).get("type")
    if kind == "many2one":
        return (service or False) == (odoo_value[0] if odoo_value else False)
    if kind in ("float", "monetary", "integer"):
        return float(service or 0) == float(odoo_value or 0)
    if kind == "boolean":
        return bool(service) == bool(odoo_value)
    return str(service or "").strip() == str(odoo_value or "").strip()


def compare(info, odoo_row, written):
    """[(section, [row])] with row = {field, label, odoo, odoo_source, service, service_source, state}.
    state: same | differs | not_written."""
    sections = []
    for title, fields in SECTIONS:
        rows = []
        for field in fields:
            if field not in info:
                continue
            odoo_value = odoo_row.get(field, False) if odoo_row else None
            mine = written.get(field)
            row = {"field": field, "label": info[field]["string"],
                   "odoo": shown(odoo_value, info[field]) if odoo_row else None,
                   "odoo_source": (odoo_row or {}).get(f"{field}_source") or "",
                   "service": None, "service_source": None, "message_id": None, "state": "not_written"}
            if mine:
                row.update(service=mine["value"], service_source=mine["source"], message_id=mine["message_id"])
                row["state"] = "same" if odoo_row is None or same(mine["value"], odoo_value, info[field]) else "differs"
                row["service_shown"] = (shown(odoo_value, info[field]) if row["state"] == "same" and odoo_row
                                        else shown(mine["value"], info[field]))
            rows.append(row)
        sections.append((title, rows))
    return sections


def read_odoo(odoo, booking_id):
    """(fields info, booking row or None, {child kind: rows})."""
    info = fields_info(odoo, BOOKING)
    wanted = [f for f in ALL_FIELDS if f in info] + [f"{f}_source" for f in ALL_FIELDS if f"{f}_source" in info]
    rows = odoo.call(BOOKING, "read", [booking_id], wanted)
    children = {}
    for kind, (model, fields) in CHILDREN.items():
        child_info = fields_info(odoo, model)
        found = odoo.call(model, "search_read", [("booking_id", "=", booking_id)],
                          fields=[f for f in fields if f in child_info])
        children[kind] = [{**r, **{f"{f}_shown": shown(r.get(f), child_info.get(f)) for f in fields}} for r in found]
    return info, (rows[0] if rows else None), children


def created_children(db, booking_id):
    """Child records (containers, B/Ls…) the service created, by (model, id): the email they came from."""
    mine, args = reference_changes(db)
    rows = db.execute(
        "SELECT c.model, c.record_id, c.message_id, c.values_json, f.filename FROM odoo_change c "
        "LEFT JOIN file f ON f.id = c.file_id WHERE c.action = 'created' AND c.model != ? AND c.values_json LIKE ? "
        f"AND {mine}", (BOOKING, f'%"booking_id": {booking_id}%', *args)).fetchall()
    return {(r["model"], r["record_id"]): r for r in rows
            if json.loads(r["values_json"]).get("booking_id") == booking_id}
