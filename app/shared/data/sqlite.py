"""Data sets over the service's own tables: the setup lists (mailboxes, vendor mappings, subject formats), which can be
imported (rows are matched, checked as the Add and Edit forms check them, then created or updated), and read-only
lists (emails, activity), which can only be exported: they are the record of what happened, so a file never edits them.
"""

from app.shared.core import store
from app.shared.data import Column, DataSet, Plan, RowPlan
from app.shared.data import convert
from app.shared.odoo import mapping_check
from app.shared.odoo.client import Odoo

KIND = {"text": "char", "textarea": "text", "pattern": "char", "int": "int", "float": "float", "bool": "bool",
        "date": "date", "select": "select", "odoo": "int"}


def _show(column, value):
    """A stored value as a file shows it."""
    if value is None or value == "":
        return None
    if column.kind == "bool":
        return "Yes" if value else "No"
    if column.kind == "select":
        return dict(column.options).get(value, value)
    if column.kind == "date":
        return str(value)[:10]
    if column.kind in ("int", "float"):
        return value
    return str(value)


def _canon(column, value):
    """A value as the table stores it, for telling a change from no change."""
    if value is None or value == "":
        return None
    if column.kind == "bool":
        return 1 if value else 0
    if column.kind in ("int", "float"):
        return float(value)
    return str(value)


class SqliteDataSet(DataSet):
    """A table of the service's own, imported and exported column by column."""

    def __init__(self, key, label, singular, table, columns, endpoint, endpoint_args=None, match=(), importable=False,
                 creatable=False, required=(), validate=None, odoo_ids=None, order="id"):
        self.key, self.label, self.singular, self.table = key, label, singular, table
        self._columns = list(columns)
        self.endpoint, self.endpoint_args = endpoint, endpoint_args or {}
        self.match, self.importable, self.creatable = tuple(match), importable, creatable
        self.required, self.validate, self.order = tuple(required), validate, order
        # {column: (Odoo model, the column holding its name)} for an Odoo id the service keeps beside a name
        self.odoo_ids = odoo_ids or {}

    def columns(self, ctx):
        return self._columns

    def rows(self, ctx, names, filters):
        by_name = {c.name: c for c in self._columns}
        out = []
        for row in ctx.db.execute(f"SELECT * FROM {self.table} ORDER BY {self.order}"):
            row = dict(row)
            out.append({n: _show(by_name[n], row.get(n)) for n in names if n in by_name})
        return out

    # ── import ───────────────────────────────────────────────────────

    def _convert(self, column, cell):
        if column.kind == "bool":
            return 1 if convert.to_bool(cell) else 0
        if column.kind == "int":
            return convert.to_int(cell)
        if column.kind == "float":
            return convert.to_float(cell)
        if column.kind == "date":
            return f"{convert.to_date(cell)} 00:00:00"
        if column.kind == "select":
            return convert.to_choice(cell, column.options)
        return convert.to_text(cell)

    def _key(self, values):
        return tuple(str(values.get(k) or "").strip().upper() for k in self.match)

    def target(self, ctx):
        return "the OCR service's own settings (nothing is sent to Odoo)"

    def _odoo_problems(self, ctx, wanted):
        """Why an Odoo id would point at the wrong record in a connection that is on, as saving the form checks it."""
        on = [c for c in store.connections(ctx.db, on=True) if store.is_complete(c)]
        if not wanted or not on:
            return []
        problems, reference = [], None
        for index, connection in enumerate(on):
            try:
                odoo = Odoo.from_connection(ctx.db, connection)
            except Exception as exc:
                problems.append(f"{connection['name']} could not be reached to check the Odoo records. "
                                f"Try again, or turn it off first: {exc}")
                continue
            if index == 0:
                reference = odoo
            problems += mapping_check.mismatches(odoo, connection["name"], {m: sorted(i) for m, i in wanted.items()},
                                                 None if index == 0 else reference, on[0]["name"])
        return problems

    def plan(self, ctx, rows):
        cols = {c.name: c for c in self._columns}
        mapped = set()
        for _number, raw in rows:
            mapped |= set(raw)
        problems = []
        if self.match and "id" not in mapped and not set(self.match) <= mapped:
            labels = ", ".join(cols[k].label for k in self.match)
            problems.append(f"Map the ID column, or all of {labels}: they tell which {self.singular} a row is.")
            return Plan([], problems)
        current = {r["id"]: dict(r) for r in ctx.db.execute(f"SELECT * FROM {self.table}")}
        by_key = {}
        for record in current.values():
            by_key.setdefault(self._key(record), []).append(record["id"])
        converted = []
        for number, raw in rows:
            plan, values = RowPlan(number), {}
            for name, cell in raw.items():
                column = cols.get(name)  # a name that is no column of this list (a secret, say) is not imported
                if column is None or name == "id" or not column.importable or convert.is_blank(cell):
                    continue
                try:
                    values[name] = self._convert(column, cell)
                except ValueError as exc:
                    plan.errors.append(f"{column.label}: {exc}")
            converted.append((plan, raw, values))
        # The Odoo records the rows point at: they must exist, and a name not given is read from Odoo.
        wanted = {}
        for _plan, _raw, values in converted:
            for name, (model, _name_column) in self.odoo_ids.items():
                if values.get(name):
                    wanted.setdefault(model, set()).add(values[name])
        problems += self._odoo_problems(ctx, wanted)
        known = {}
        if wanted:
            try:
                reference = ctx.odoo()
                known = {m: mapping_check.names(reference, m, sorted(ids)) for m, ids in wanted.items()}
            except Exception:
                known = {}
        seen = {}
        for plan, raw, values in converted:
            for name, (model, name_column) in self.odoo_ids.items():
                if name_column and values.get(name) and not values.get(name_column) and known.get(model, {}).get(values[name]):
                    values[name_column] = known[model][values[name]]
            target = None
            if not convert.is_blank(raw.get("id")):
                try:
                    rid = convert.to_int(raw["id"])
                    target = current.get(rid)
                    if not target:
                        plan.errors.append(f"No {self.singular} has ID {rid}.")
                except ValueError:
                    plan.errors.append("ID: write a whole number")
            elif self.match and set(self.match) <= set(raw):
                found = by_key.get(self._key(values), [])
                if len(found) == 1:
                    target = current[found[0]]
                elif len(found) > 1:
                    plan.errors.append(f"{len(found)} {self.label.lower()} match: use the ID column.")
            elif self.match:
                plan.errors.append(f"No ID, and the columns that tell which {self.singular} this is are not in the file.")
            if not plan.errors:
                merged = {**(target or {}), **values}
                if target is None:
                    if not self.creatable:
                        plan.errors.append(f"This list only updates records: no {self.singular} matches this row.")
                    for name in self.required:
                        if convert.is_blank(merged.get(name)):
                            plan.errors.append(f"{cols[name].label} is required.")
                error = self.validate(merged) if (self.validate and not plan.errors) else None
                if error:
                    plan.errors.append(error.rstrip("."))
            if not plan.errors:
                key = ("id", target["id"]) if target else ("new", self._key(values))
                if key in seen:
                    plan.errors.append(f"Same {self.singular} as row {seen[key]}.")
                seen.setdefault(key, plan.number)
            plan.values = values
            if plan.errors:
                plan.action = "error"
            elif target is None:
                plan.action = "create"
            else:
                plan.record_id = target["id"]
                plan.changes = [(cols[n].label, _show(cols[n], target.get(n)), _show(cols[n], v))
                                for n, v in values.items() if _canon(cols[n], target.get(n)) != _canon(cols[n], v)]
                plan.action = "update" if plan.changes else "unchanged"
            shown = {**(target or {}), **values}
            plan.label = next((str(shown[k]) for k in ("name", "partner_name", "document_name", "sender_domain") if shown.get(k)),
                              f"#{plan.record_id or 'new'}")
        return Plan([p for p, _r, _v in converted], problems)

    def apply(self, ctx, plan):
        with ctx.db:  # all or nothing: a failure part-way leaves the table as it was
            for p in plan.rows:
                if p.action not in ("create", "update") or p.errors:
                    continue
                if p.action == "update":
                    ctx.db.execute(f"UPDATE {self.table} SET {', '.join(f'{c} = ?' for c in p.values)} WHERE id = ?",
                                   (*p.values.values(), p.record_id))
                else:
                    p.record_id = ctx.db.execute(
                        f"INSERT INTO {self.table} ({', '.join(p.values)}) VALUES ({', '.join('?' * len(p.values))})",
                        tuple(p.values.values())).lastrowid
                p.done = True


class QueryDataSet(DataSet):
    """A list read by a query, exported only: emails, the activity log. ``query(ctx, filters)`` gives (sql, params), and
    ``show(row)`` may turn a row into what the file holds (default: its columns as they are)."""

    def __init__(self, key, label, singular, columns, query, endpoint, endpoint_args=None, show=None):
        self.key, self.label, self.singular = key, label, singular
        self._columns = list(columns)
        self.query, self.endpoint, self.endpoint_args, self.show = query, endpoint, endpoint_args or {}, show

    def columns(self, ctx):
        return self._columns

    def rows(self, ctx, names, filters):
        sql, params = self.query(ctx, filters)
        out = []
        for row in ctx.db.execute(sql, params):
            row = dict(row)
            if self.show:
                row = self.show(row)
            out.append({n: row.get(n) for n in names})
        return out


# ── the shared lists ─────────────────────────────────────────────────

def _form_columns(form, required):
    cols = [Column("id", "ID", "int", default=True, note="Leave empty to add a new record")]
    for f in form.fields:
        if f.kind == "secret":
            continue  # a secret never leaves the service
        label = f"{f.label} (Odoo ID)" if f.kind == "odoo" else f.label
        cols.append(Column(f.name, label, KIND[f.kind], options=tuple(f.options) if f.kind == "select" else (),
                           default=f.name in form.columns, required=f.name in required, note=f.help))
        if f.kind == "odoo" and f.name_column:
            cols.append(Column(f.name_column, f.label, "char", default=f.name_column in form.columns,
                               note="Read from Odoo when empty"))
    named = {c.name for c in cols}
    for extra in form.columns:
        if extra not in named:  # shown in the list, not a form field (a mailbox's read status)
            cols.append(Column(extra, extra.replace("_", " ").capitalize(), "char", importable=False, default=True))
    return cols


def _form_dataset(key):
    from app.shared.web import forms
    form = forms.FORMS[key]
    required = tuple(forms.REQUIRED.get(form.table, []))
    odoo_ids = {f.name: (mapping_check.OPTION_MODELS[f.options], f.name_column)
                for f in form.fields if f.kind == "odoo"}
    match = {"vendor_mapping": ("role", "document_name", "sender_domain"), "mailbox": ("name",),
             "subject_format": ("name",)}[form.table]
    return SqliteDataSet(key, form.title, form.singular, form.table, _form_columns(form, required), "web.records",
                         {"key": key}, match=match, importable=True, creatable=True, required=required,
                         validate=lambda values: forms.validate(form, values), odoo_ids=odoo_ids)


def _emails(ctx, filters):
    from app.shared.web import search
    from app.shared.web.base import STATE_LABELS
    where, args = search.email_where(search.parse(filters.get("q") or ""))
    if filters.get("state") in STATE_LABELS:
        where, args = f"{where} AND m.state = ?", [*args, filters["state"]]
    return ("SELECT m.id, b.name AS mailbox, m.subject, m.sender, m.sender_domain, m.received, m.state, m.attempts, m.note, "
            "(SELECT COUNT(*) FROM file f WHERE f.message_id = m.id) AS files, m.conversation_id, m.internet_message_id, "
            "m.created_at, m.processed_at, m.body_text FROM message m JOIN mailbox b ON b.id = m.mailbox_id "
            f"WHERE {where} ORDER BY m.received DESC, m.id DESC"), args


def _email_row(row):
    from app.shared.web.base import STATE_LABELS
    return dict(row, state=STATE_LABELS.get(row["state"], row["state"]))


EMAIL_COLUMNS = (
    Column("id", "ID", "int", importable=False, default=True), Column("mailbox", "Mailbox", importable=False, default=True),
    Column("subject", "Subject", importable=False, default=True), Column("sender", "Sender", importable=False, default=True),
    Column("sender_domain", "Sender domain", importable=False), Column("received", "Received (UTC)", "datetime", importable=False, default=True),
    Column("state", "State", importable=False, default=True), Column("attempts", "Attempts", "int", importable=False),
    Column("note", "Note", importable=False), Column("files", "Files", "int", importable=False, default=True),
    Column("conversation_id", "Conversation ID", importable=False), Column("internet_message_id", "Message ID", importable=False),
    Column("created_at", "Saved by the service", "datetime", importable=False), Column("processed_at", "Last processed", "datetime", importable=False),
    Column("body_text", "Text of the newest message", "text", importable=False))


def _activity(ctx, filters):
    from app.shared.web.activity import ACTIVITY_LABELS
    where, args = [], []
    if filters.get("kind") in ACTIVITY_LABELS:
        where.append("kind = ?")
        args.append(filters["kind"])
    if (filters.get("q") or "").strip():
        where.append("(summary LIKE ? OR detail LIKE ?)")
        args += [f"%{filters['q'].strip()}%"] * 2
    return (f"SELECT id, at, kind, level, actor, summary, detail, link FROM activity"
            f"{' WHERE ' + ' AND '.join(where) if where else ''} ORDER BY id DESC"), args


ACTIVITY_COLUMNS = (
    Column("id", "ID", "int", importable=False), Column("at", "When (UTC)", "datetime", importable=False, default=True),
    Column("kind", "Type", importable=False, default=True), Column("level", "Level", importable=False, default=True),
    Column("actor", "By", importable=False, default=True), Column("summary", "Summary", importable=False, default=True),
    Column("detail", "Detail", "text", importable=False), Column("link", "Link", importable=False))

_datasets = []


def datasets():
    """The shared data sets (built on first use: the setup forms are loaded with the pages)."""
    if not _datasets:
        _datasets.extend([_form_dataset("mailboxes"), _form_dataset("vendor-mappings"), _form_dataset("subject-formats"),
                          QueryDataSet("emails", "Emails", "email", EMAIL_COLUMNS, _emails, "web.messages", show=_email_row),
                          QueryDataSet("activity", "Activity", "entry", ACTIVITY_COLUMNS, _activity, "web.activity")])
    return _datasets
