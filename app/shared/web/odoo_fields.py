"""Odoo fields as the pages show them: their labels and kinds (read once per model), and a value as people read it."""

_fields_cache = {}


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
