"""Scraped bills and customs: Odoo's records as its forms show them, parent to child."""

from flask import abort, g, render_template, request, url_for

from . import bp, odoo_forms
from ..core import store
from .base import _odoo_or_error, reference_url

from .conversation import conversation, pane_context
from .ui import group_by, group_ids, group_list, group_rows, pager


# ── the list beside every scraped bill and customs page ──────────────

BILL_STATUS = {"bill_confirmed": "ok", "bill_found": "ok", "ready_for_po": "review", "waiting": "review"}
BATCH_STATUS = {"pass": "ok", "review_required": "review"}


def _bill_item(row):
    raw = row["raw"]
    amount = f"{row['amount_total']} {row.get('currency_name') or ''}".strip() if row.get("amount_total") else ""
    return {"title": row.get("name") or f"#{row['id']}", "sub": " · ".join(v for v in (row.get("partner_id"), amount) if v),
            "status": (BILL_STATUS.get(raw.get("state"), ""), row.get("state")) if row.get("state") else None,
            "attention": raw.get("state") in ("waiting", "ready_for_po")}


def _batch_item(row):
    raw = row["raw"]
    fails, reviews = raw.get("fail_count") or 0, raw.get("review_count") or 0
    status = (("failed", f"{fails} failed") if fails else ("review", f"{reviews} to review") if reviews
              else (BATCH_STATUS.get(raw.get("status"), ""), row.get("status")) if row.get("status") else None)
    return {"title": row.get("name") or f"#{row['id']}",
            "sub": f"{raw.get('entry_count') or 0} entr{'y' if raw.get('entry_count') == 1 else 'ies'}"
                   + (f" · {row['total_amount']}" if row.get("total_amount") else ""),
            "status": status, "attention": bool(fails or reviews)}


def _customs_batch_ids():
    entry_ids = odoo_forms.record_ids(g.db, "customs_entry")
    odoo, _error = _odoo_or_error()
    if not (odoo and entry_ids):
        return set()
    try:
        return {r["batch_id"][0] for r in odoo.call(
            odoo_forms.CUSTOMS_ENTRY["model"], "search_read", [("id", "in", sorted(entry_ids))],
            fields=["batch_id"]) if r["batch_id"]}
    except Exception:
        return set()  # the list reports Odoo's error


# key: (model, list columns, the record ids, page endpoint, its id argument, item, the purpose its emails come from)
PANES = {
    "bills": lambda: (odoo_forms.SCRAPED_BILL["model"], odoo_forms.BILL_LIST, odoo_forms.record_ids(g.db, "bill_matching"),
                      "web.scraped_bill", "bill_id", _bill_item, "bill_matching"),
    "customs": lambda: (odoo_forms.CUSTOMS_BATCH["model"], odoo_forms.BATCH_LIST, _customs_batch_ids(),
                        "web.customs_batch", "batch_id", _batch_item, "customs_entry"),
}


def _pane(kind, current=None):
    model, columns, ids, endpoint, id_arg, item, purpose = PANES[kind]()
    odoo, error = _odoo_or_error()
    page = pager(len(ids))
    rows = []
    if odoo and ids:
        try:
            shown = sorted(ids, reverse=True)[page["offset"]:page["offset"] + page["limit"]]
            _heads, rows = odoo_forms.list_rows(odoo, model, set(shown), columns)
        except Exception as exc:
            error = str(exc)
    counts = odoo_forms.email_counts(g.db, purpose) if kind == "bills" else {}
    times = store.record_times(g.db, model, [r["id"] for r in rows])
    keep = {"offset": page["offset"]} if page["offset"] else {}
    items = [{"id": r["id"], "href": url_for(endpoint, **{id_arg: r["id"]}, **keep), "emails": counts.get(r["id"]),
              "when": times.get(r["id"], (None, None))[1], **item(r)} for r in rows]
    return pane_context(items, page, error, purpose, current)


# ── scraped bills and customs: Odoo's records, as its forms show them ─


def _odoo_link(model, record_id):
    return f"{reference_url()}/web#model={model}&id={record_id}&view_type=form"


def _odoo_record(page, record_id, crumbs, purpose=None, pane=None, pane_current=None):
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
    thread, files = conversation(g.db, feeding, page["model"], record_id)
    return render_template("records/odoo_record.html", title=(record or {}).get("title") or f"#{record_id}", record=record,
                           kind=pane, thread=thread, files=files, **_pane(pane, pane_current or record_id),
                           crumbs=crumbs, emails=feeding, error=error, odoo_link=_odoo_link(page["model"], record_id),
                           created=created, modified=modified)


# The kinds of Odoo field a list can be grouped on, as Odoo allows (amounts and counts are summed there instead).
GROUPABLE = {"char", "selection", "many2one", "date", "boolean"}


def _odoo_list(title, intro, model, ids, columns, endpoint, id_arg, empty, purpose=None, pane=None):
    if pane and not request.args.get("groupby") and request.args.get("view") != "table":
        return render_template("records/workspace.html", title=title, kind=pane, **_pane(pane))
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
    groupby = group_by(options)
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
                           error=error, pager=page)


@bp.route("/scraped-bills")
def scraped_bills():
    return _odoo_list("Scraped bills", "Every supplier invoice the service sent to Odoo, as Odoo holds it now.",
                      odoo_forms.SCRAPED_BILL["model"], odoo_forms.record_ids(g.db, "bill_matching"),
                      odoo_forms.BILL_LIST, "web.scraped_bill", "bill_id", "Scraped bills appear here once the service has "
                      "sent an invoice from a Bill Matching mailbox to Odoo.", purpose="bill_matching", pane="bills")


@bp.route("/scraped-bills/<int:bill_id>")
def scraped_bill(bill_id):
    return _odoo_record(odoo_forms.SCRAPED_BILL, bill_id, [("Scraped bills", url_for("web.scraped_bills"))],
                        purpose="bill_matching", pane="bills")


@bp.route("/customs")
def customs_batches():
    entry_ids = odoo_forms.record_ids(g.db, "customs_entry")
    odoo, _error = _odoo_or_error()
    batch_ids = set()
    if odoo and entry_ids:
        try:
            batch_ids = {r["batch_id"][0] for r in odoo.call(
                odoo_forms.CUSTOMS_ENTRY["model"], "search_read", [("id", "in", sorted(entry_ids))],
                fields=["batch_id"]) if r["batch_id"]}
        except Exception:
            pass  # the list below reports Odoo's error
    return _odoo_list("Customs", "The customs audit batches holding the entry summaries the service uploaded, "
                      "as Odoo holds them now. Open a batch for its entries, and an entry for its findings.",
                      odoo_forms.CUSTOMS_BATCH["model"], batch_ids, odoo_forms.BATCH_LIST, "web.customs_batch", "batch_id",
                      "Batches appear here once the service has uploaded an entry summary from a Customs Entry "
                      "Summaries mailbox.", pane="customs")


@bp.route("/customs/batches/<int:batch_id>")
def customs_batch(batch_id):
    return _odoo_record(odoo_forms.CUSTOMS_BATCH, batch_id, [("Customs", url_for("web.customs_batches"))], pane="customs")


@bp.route("/customs/entries/<int:entry_id>")
def customs_entry(entry_id):
    crumbs = [("Customs", url_for("web.customs_batches"))]
    batch = None
    odoo, _error = _odoo_or_error()
    if odoo:
        try:
            found = odoo.call(odoo_forms.CUSTOMS_ENTRY["model"], "search_read", [("id", "=", entry_id)],
                              fields=["batch_id"])
            if found and found[0]["batch_id"]:
                batch = found[0]["batch_id"][0]
                crumbs.append((found[0]["batch_id"][1], url_for("web.customs_batch", batch_id=batch)))
        except Exception:
            pass  # the page reports Odoo's error
    return _odoo_record(odoo_forms.CUSTOMS_ENTRY, entry_id, crumbs, purpose="customs_entry", pane="customs",
                        pane_current=batch or -1)
