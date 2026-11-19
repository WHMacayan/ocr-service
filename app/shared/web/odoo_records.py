"""A job's Odoo records, as their forms show them, parent to child: a list and a record page (the jobs' pages use
these: scraped_bills/pages.py, customs/pages.py). A record page's Emails tab is its whole conversation."""

from flask import abort, g, render_template

from app.shared.core import store
from app.shared.web import odoo_forms
from app.shared.web.base import _odoo_or_error, reference_url
from app.shared.web.conversation import conversation
from app.shared.web.ui import group_by, group_ids, group_list, group_rows, pager


def _odoo_link(model, record_id):
    return f"{reference_url()}/web#model={model}&id={record_id}&view_type=form"


def _odoo_record(page, record_id, crumbs, purpose=None):
    feeding = odoo_forms.emails(g.db, purpose, record_id) if purpose else []
    odoo, error = _odoo_or_error()
    record = None
    if odoo:
        try:
            record = odoo_forms.read(odoo, page, record_id)
        except Exception as exc:
            error = str(exc)
    if not record and not error and not feeding:
        abort(404)
    # When the service first and last wrote this record (its own log), not Odoo's dates.
    created, modified = store.record_times(g.db, page["model"], [record_id]).get(record_id, (None, None))
    thread, _files = conversation(g.db, feeding, page["model"], record_id)
    return render_template("records/odoo_record.html", title=(record or {}).get("title") or f"#{record_id}", record=record,
                           crumbs=crumbs, emails=feeding, thread=thread, error=error,
                           odoo_link=_odoo_link(page["model"], record_id), created=created, modified=modified)


# The kinds of Odoo field a list can be grouped on, as Odoo allows (amounts and counts are summed there instead).
GROUPABLE = {"char", "selection", "many2one", "date", "boolean"}


def _odoo_list(title, intro, model, ids, columns, endpoint, id_arg, empty, purpose=None, group=None, data=()):
    """A job's records as a table. It opens grouped on ``group`` (a column) until another is chosen. ``data``: the data sets
    its Import / Export menu offers."""
    odoo, error = _odoo_or_error()
    heads, rows, info, page, groups = [], [], {}, None, []
    counts = odoo_forms.email_counts(g.db, purpose) if purpose else {}

    def read(some):
        nonlocal heads
        if not some:
            return []
        heads, found = odoo_forms.list_rows(odoo, model, set(some), columns)
        times = store.record_times(g.db, model, [row["id"] for row in found])
        for row in found:
            row["emails"] = counts.get(row["id"], 0)
            row["created"], row["modified"] = times.get(row["id"], (None, None))
        return found

    try:
        if odoo and ids:
            info = odoo_forms.fields_info(odoo, model)
    except Exception as exc:
        error = str(exc)
    heads = [(c, info[c]["string"]) for c in columns if c in info]
    # Group By: the columns after the record's name that hold a name, a choice or a date.
    options = {c: label for c, label in heads[1:] if info[c].get("type") in GROUPABLE}
    groupby = group_by(options, group)
    try:
        if groupby:
            # Every record under its group, as Odoo groups; Odoo is asked for the open group's rows only.
            values = {r["id"]: odoo_forms.shown(r.get(groupby), info[groupby]) for r in odoo.call(
                model, "search_read", [("id", "in", sorted(ids))], fields=[groupby], context={"active_test": False})}
            members = group_ids({i: values.get(i) for i in ids})
            groups = group_list({v: len(m) for v, m in members.items()},
                                lambda v, offset, limit: read(members[v][offset:offset + limit]))
        else:
            page = pager(len(ids))
            # newest first, Odoo asked for the page shown only
            rows = read(sorted(ids, reverse=True)[page["offset"]:page["offset"] + page["limit"]] if odoo else [])
            groups = group_rows(rows)
    except Exception as exc:
        error = str(exc)
    return render_template("records/odoo_list.html", title=title, intro=intro, heads=heads, rows=rows, groups=groups,
                           groupby=groupby, group_options=options, endpoint=endpoint, id_arg=id_arg, empty=empty,
                           error=error, pager=page, data_keys=list(data))
