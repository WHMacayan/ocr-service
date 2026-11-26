"""Odoo fields as the pages show them: their labels and kinds (read once per model), and a value as people read it."""

import time

_fields_cache = {}
# An Odoo upgrade can add or rename a field: what was read is trusted for this long.
FIELDS_TTL_SECONDS = 600


def fields_info(odoo, model):
    """Labels and kinds of a model's fields, read once per connection and model (review F15: two Odoo databases
    can differ in the same model), and again after FIELDS_TTL_SECONDS."""
    key = (getattr(odoo, "connection_id", None), getattr(odoo, "url", None), getattr(odoo, "db", None), model)
    cached = _fields_cache.get(key)
    if cached and time.monotonic() - cached[0] < FIELDS_TTL_SECONDS:
        return cached[1]
    value = odoo.call(model, "fields_get", attributes=["string", "type", "selection"])
    _fields_cache[key] = (time.monotonic(), value)
    return value


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
