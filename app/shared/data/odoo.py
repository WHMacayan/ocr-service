"""Data sets over Odoo models (bookings, scraped bills, customs). The fields come from Odoo's own ``fields_get``, so what
can be picked always matches what Odoo holds, and what Odoo does not let anyone edit (read-only and computed fields)
is not offered for an import.

An import is as Odoo's: a row is matched to its record by ID, or by the data set's key, then created or updated; a
related record (a forwarder, a factory) is found by its name; nothing is written until the file is applied.
"""

from app.shared.data import Column, DataSet, Plan, RowPlan
from app.shared.data import convert

SCALAR = {"char": "char", "text": "text", "integer": "int", "float": "float", "monetary": "float", "boolean": "bool",
          "date": "date", "datetime": "datetime", "selection": "select", "many2one": "many2one"}
_TECHNICAL = ("message_", "activity_", "website_", "my_activity")
CHUNK = 400


def _fault(exc):
    """What Odoo said went wrong: the last line of its message (the rest is a server traceback)."""
    return (str(getattr(exc, "faultString", exc)).strip().splitlines() or ["unknown error"])[-1][:300]


class OdooDataSet(DataSet):
    writes_to = "odoo"
    max_rows = 300  # a request must finish before the web server's timeout; Odoo takes about a tenth of a second a write

    def __init__(self, key, label, singular, model, ids, endpoint, endpoint_args=None, default=(), match=(),
                 importable=False, creatable=False, children=None, exclude=(), keep_out=()):
        """ids(ctx, filters): the ids of the records the page lists. children: {one2many field: (child model, the child's
        display field)}, exported as a list of those values. exclude: fields never offered. keep_out: fields offered for an
        export but not for an import."""
        self.key, self.label, self.singular, self.model, self.ids = key, label, singular, model, ids
        self.endpoint, self.endpoint_args, self.default = endpoint, endpoint_args or {}, tuple(default)
        self.match, self.importable, self.creatable = tuple(match), importable, creatable
        self.children, self.exclude, self.keep_out = children or {}, set(exclude), set(keep_out)

    def _info(self, ctx):
        cache = ctx.cache
        if ("fields", self.model) not in cache:
            cache[("fields", self.model)] = ctx.odoo().call(
                self.model, "fields_get",
                attributes=["string", "type", "selection", "relation", "relation_field", "readonly", "required", "store"])
        return cache[("fields", self.model)]

    def columns(self, ctx):
        info = self._info(ctx)
        cols = [Column("id", "ID", "int", default=True, note="The Odoo record's ID: leave empty for a new record")]
        offered = {}
        for name, f in info.items():
            if name == "id" or name.startswith(_TECHNICAL) or name in self.exclude:
                continue
            if f["type"] in ("one2many", "many2many"):
                if name in self.children:
                    offered[name] = Column(name, f["string"], "list", importable=False, default=name in self.default)
                continue
            kind = SCALAR.get(f["type"])
            if not kind:
                continue
            can_write = not f.get("readonly") and f.get("store", True) and name not in self.keep_out
            offered[name] = Column(name, f["string"], kind, importable=can_write, default=name in self.default,
                                   options=tuple((k, lbl) for k, lbl in f.get("selection") or []),
                                   relation=f.get("relation") or "", required=bool(f.get("required")))
        first = [offered.pop(n) for n in self.default if n in offered]
        return cols + first + sorted(offered.values(), key=lambda c: c.label.lower())

    # ── export ───────────────────────────────────────────────────────

    def rows(self, ctx, names, filters):
        odoo, info = ctx.odoo(), self._info(ctx)
        fields = [n for n in names if n != "id" and n in info and n not in self.children]
        kids = [n for n in names if n in self.children and n in info]
        ids = sorted(self.ids(ctx, filters), reverse=True)
        out = []
        for start in range(0, len(ids), CHUNK):
            chunk = ids[start:start + CHUNK]
            found = {r["id"]: r for r in odoo.call(self.model, "search_read", [("id", "in", chunk)],
                                                   fields=fields, context={"active_test": False})}
            lists = {}
            for name in kids:
                child, display = self.children[name]
                inverse = info[name].get("relation_field")
                for c in odoo.call(child, "search_read", [(inverse, "in", chunk)], fields=[inverse, display],
                                   order="id", context={"active_test": False}):
                    owner = c[inverse][0] if isinstance(c[inverse], list) else c[inverse]
                    lists.setdefault((name, owner), []).append(str(c[display]))
            for record_id in chunk:
                record = found.get(record_id)
                if not record:
                    continue
                row = {"id": record_id}
                for name in fields:
                    row[name] = self._show(info[name], record.get(name))
                for name in kids:
                    row[name] = ", ".join(lists.get((name, record_id), [])) or None
                out.append({n: row.get(n) for n in names})
        return out

    @staticmethod
    def _show(f, value):
        if f["type"] == "boolean":  # Odoo gives False for a box that is not ticked: that is No, not empty
            return "Yes" if value else "No"
        if value is False or value is None or value == "":
            return 0.0 if f["type"] in ("float", "monetary") and value is False else None
        if f["type"] == "many2one":
            return value[1] if isinstance(value, list) else value
        if f["type"] == "selection":
            return dict(f.get("selection") or []).get(value, value)
        if f["type"] == "boolean":
            return "Yes" if value else "No"
        return value

    # ── import ───────────────────────────────────────────────────────

    def target(self, ctx):
        return f"Odoo: {getattr(ctx.odoo(), 'name', None) or 'the connection in use'}"

    def _resolve(self, ctx, column, text):
        """The id of the related record named ``text``; ValueError when none or several carry the name."""
        cache = ctx.cache.setdefault("names", {})
        key = (column.relation, text.lower())
        if key not in cache:
            odoo = ctx.odoo()
            found = odoo.call(column.relation, "name_search", text, operator="=", limit=3)
            if not found:
                found = odoo.call(column.relation, "name_search", text, operator="ilike", limit=3)
            cache[key] = found
            by_id = ctx.cache.setdefault("by_id", {})  # their names, for showing a change in words
            for record_id, name in found:
                by_id[(column.relation, record_id)] = name
        found = cache[key]
        if not found:
            raise ValueError(f'no {column.label} is named "{text}"')
        if len(found) > 1:
            raise ValueError(f'several {column.label} match "{text}": write the full name')
        return found[0][0]

    def _convert(self, ctx, column, cell):
        if column.kind == "many2one":
            return self._resolve(ctx, column, convert.to_text(cell))
        if column.kind == "select":
            return convert.to_choice(cell, column.options)
        return {"int": convert.to_int, "float": convert.to_float, "bool": convert.to_bool, "date": convert.to_date,
                "datetime": convert.to_datetime}.get(column.kind, convert.to_text)(cell)

    @staticmethod
    def _plain(column, value):
        """A record's value as a comparison sees it."""
        if value is False or value is None or value == "":
            return None
        if column.kind == "many2one":
            return value[0] if isinstance(value, list) else value
        if column.kind in ("int", "float"):
            return float(value)
        return value

    def _shown(self, ctx, column, value):
        """A value as the test result shows it: a related record by its name, a choice by its label."""
        if column.kind == "many2one" and isinstance(value, list):
            return value[1] if len(value) > 1 else value[0]
        value = self._plain(column, value)
        if value is None:
            return None
        if column.kind == "many2one":
            return ctx.cache.get("by_id", {}).get((column.relation, value), value)
        if column.kind == "select":
            return dict(column.options).get(value, value)
        return value

    def plan(self, ctx, rows):
        cols = {c.name: c for c in self.columns(ctx)}
        mapped = set()
        for _number, raw in rows:
            mapped |= set(raw)
        if not self.match and "id" not in mapped:
            return Plan([], [f"Map the ID column: it tells which {self.singular} a row is."])
        if self.match and "id" not in mapped and not set(self.match) <= mapped:
            labels = ", ".join(cols[k].label for k in self.match)
            return Plan([], [f"Map the ID column, or {labels}: they tell which {self.singular} a row is."])
        if len(rows) > self.max_rows:
            return Plan([], [f"Import at most {self.max_rows} rows at a time: split the file."])
        odoo = ctx.odoo()
        converted = []
        for number, raw in rows:
            plan, values = RowPlan(number), {}
            for name, cell in raw.items():
                column = cols.get(name)
                if column is None or name == "id" or not column.importable or convert.is_blank(cell):
                    continue
                try:
                    values[name] = self._convert(ctx, column, cell)
                except ValueError as exc:
                    plan.errors.append(f"{column.label}: {exc}")
            converted.append((plan, raw, values))
        # The records the rows are: by ID, else by the key (one query for the whole file when the key is one field).
        ids, keyed = set(), {}
        for plan, raw, values in converted:
            if not convert.is_blank(raw.get("id")):
                try:
                    ids.add(convert.to_int(raw["id"]))
                except ValueError:
                    plan.errors.append("ID: write a whole number")
        if self.match and len(self.match) == 1 and self.match[0] in mapped:
            name = self.match[0]
            wanted = sorted({values[name] for _p, raw, values in converted if name in values and convert.is_blank(raw.get("id"))})
            for start in range(0, len(wanted), CHUNK):
                for r in odoo.call(self.model, "search_read", [(name, "in", wanted[start:start + CHUNK])], fields=[name],
                                   context={"active_test": False}):
                    keyed.setdefault(r[name], []).append(r["id"])
        written = sorted({n for _p, _r, values in converted for n in values})
        current = {}
        ids_to_read = sorted(ids | {i for found in keyed.values() for i in found})
        for start in range(0, len(ids_to_read), CHUNK):
            for r in odoo.call(self.model, "search_read", [("id", "in", ids_to_read[start:start + CHUNK])], fields=written,
                               context={"active_test": False}):
                current[r["id"]] = r
        defaults = None
        for plan, raw, values in converted:
            target = None
            if plan.errors:
                pass
            elif not convert.is_blank(raw.get("id")):
                rid = convert.to_int(raw["id"])
                target = current.get(rid)
                if not target:
                    plan.errors.append(f"No {self.singular} has ID {rid} in Odoo.")
            elif self.match:
                found = []
                if len(self.match) == 1:
                    found = keyed.get(values.get(self.match[0]), [])
                elif all(k in values for k in self.match):
                    found = odoo.call(self.model, "search", [(k, "=", values[k]) for k in self.match], limit=2)
                if len(found) == 1:
                    target = current.get(found[0]) or {"id": found[0]}
                elif len(found) > 1:
                    plan.errors.append(f"Several {self.label.lower()} have this {cols[self.match[0]].label}: use the ID column.")
                elif not all(k in values for k in self.match):
                    plan.errors.append(f"The {', '.join(cols[k].label for k in self.match)} is empty: write it, or the ID.")
            if not plan.errors and target is None:
                if not self.creatable:
                    plan.errors.append(f"This list only updates records: no {self.singular} matches this row.")
                else:
                    needed = [c.name for c in cols.values() if c.required and c.importable and c.name not in values]
                    if needed:
                        if defaults is None:
                            defaults = odoo.call(self.model, "default_get", [c.name for c in cols.values() if c.required]) or {}
                        missing = [cols[n].label for n in needed if defaults.get(n) in (None, False, "")]
                        if missing:
                            plan.errors.append("Required: " + ", ".join(missing))
            if plan.errors:
                plan.action = "error"
            elif target is None:
                plan.action, plan.values = "create", values
            else:
                plan.record_id = target["id"]
                changed = {n: v for n, v in values.items() if self._plain(cols[n], target.get(n)) != self._plain(cols[n], v)}
                plan.values = changed
                plan.changes = [(cols[n].label, self._shown(ctx, cols[n], target.get(n)), self._shown(ctx, cols[n], v))
                                for n, v in changed.items()]
                plan.action = "update" if changed else "unchanged"
            plan.label = str(values.get(self.match[0]) if self.match and values.get(self.match[0]) else
                             f"#{plan.record_id}" if plan.record_id else "new")
        seen = {}
        for plan, _raw, _values in converted:
            if plan.action in ("create", "update", "unchanged") and plan.record_id:
                if plan.record_id in seen:
                    plan.errors.append(f"Same {self.singular} as row {seen[plan.record_id]}.")
                    plan.action = "error"
                seen.setdefault(plan.record_id, plan.number)
        return Plan([p for p, _r, _v in converted])

    def apply(self, ctx, plan):
        odoo = ctx.odoo()
        odoo.activity_db = None  # the page logs one entry for the whole import, not one per row
        for p in plan.rows:
            if p.action not in ("create", "update") or p.errors:
                continue
            try:
                if p.action == "update":
                    odoo.call(self.model, "write", [p.record_id], p.values)
                else:
                    p.record_id = odoo.call(self.model, "create", p.values)
                p.done = True
            except Exception as exc:  # Odoo refused this row; the others still go in
                p.errors.append(_fault(exc))
                p.action = "error"
