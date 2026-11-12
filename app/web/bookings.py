"""Bookings: the service's record of each booking set against what Odoo holds now."""

from flask import abort, g, render_template

from . import booking_compare, bp
from .base import _odoo_or_error, reference_url
from .ui import group_by, group_ids, group_list, group_rows, pager
from ..core import store


# ── bookings: the service's record against Odoo ─────────────────────

# Group By, over every booking as Odoo groups: {key: (label, the Odoo field it groups on)}; Check is the service's own.
BOOKING_GROUPS = {"state": ("Odoo state", "state"), "factory": ("Factory", "factory_id"),
                  "forwarder": ("Forwarder", "forwarder_id"), "loading": ("Port of loading", "port_of_loading"),
                  "discharge": ("Port of discharge", "port_of_discharge"), "check": ("Check", None)}


def check(row):
    """How the service's record of a booking compares with Odoo, as the Check column says it."""
    if row["missing_in_odoo"]:
        return "Not in Odoo"
    if not row["recorded"]:
        return "Not recorded"
    if row["differs"]:
        return "Differs"
    return "Matches" if row["odoo"] else None


def booking_rows(odoo, ids):
    """(rows, error): the bookings with these ids, as the list shows them, in the order given."""
    written = booking_compare.service_values(g.db, ids)
    odoo_rows, info, error = {}, {}, None
    if odoo and ids:
        try:
            info = booking_compare.fields_info(odoo, booking_compare.BOOKING)
            fields = [f for f in ("booking_number", "state", "review_state", "factory_id", "forwarder_id", "etd", "eta",
                                  "port_of_loading", "port_of_discharge", "open_conflict_count")
                      + tuple(written_field for row in written.values() for written_field in row) if f in info]
            odoo_rows = {r["id"]: r for r in odoo.call(booking_compare.BOOKING, "search_read", [("id", "in", ids)],
                                                       fields=sorted(set(fields)), context={"active_test": False})}
        except Exception as exc:
            error = str(exc)
    mine, args = store.reference_changes(g.db)
    emails_per = dict(g.db.execute(
        f"SELECT record_id, COUNT(DISTINCT message_id) FROM odoo_change c WHERE model = ? AND {mine} GROUP BY record_id",
        (booking_compare.BOOKING, *args)).fetchall())
    times = store.record_times(g.db, booking_compare.BOOKING, list(ids))
    rows = []
    for booking_id in ids:
        mine, theirs = written.get(booking_id, {}), odoo_rows.get(booking_id)
        differs = [f for f, w in mine.items() if theirs and f in info and not booking_compare.same(w["value"], theirs.get(f), info[f])]
        label = (theirs or {}).get("booking_number") or (mine.get("booking_number") or {}).get("value") or f"#{booking_id}"
        rows.append({"id": booking_id, "label": label, "odoo": theirs, "recorded": bool(mine), "differs": differs,
                     "emails": emails_per.get(booking_id, 0), "missing_in_odoo": bool(odoo_rows) and theirs is None,
                     **{f"{f}_shown": booking_compare.shown((theirs or {}).get(f), info.get(f))
                        for f in ("state", "factory_id", "forwarder_id", "etd", "eta", "port_of_loading", "port_of_discharge")}})
        rows[-1]["created"], rows[-1]["modified"] = times.get(booking_id, (None, None))
        rows[-1]["check"] = check(rows[-1])
    return rows, error


def grouped_bookings(odoo, every, groupby):
    """(groups, error): every booking under its group, the opened group's rows read from Odoo."""
    field = BOOKING_GROUPS[groupby][1]
    if not field:
        # Check compares every booking with Odoo, so every row is read; the open group's are kept.
        rows, error = booking_rows(odoo, every)
        by_id = {r["id"]: r for r in rows}
        members = group_ids({r["id"]: r["check"] for r in rows})
        return group_list({v: len(ids) for v, ids in members.items()},
                          lambda v, offset, limit: [by_id[i] for i in members[v][offset:offset + limit]]), error
    values, error = {}, None
    if odoo and every:
        try:
            info = booking_compare.fields_info(odoo, booking_compare.BOOKING)
            values = {r["id"]: booking_compare.shown(r.get(field), info.get(field)) for r in odoo.call(
                booking_compare.BOOKING, "search_read", [("id", "in", every)], fields=[field] if field in info else [],
                context={"active_test": False})}
        except Exception as exc:
            error = str(exc)
    members = group_ids({i: values.get(i) for i in every})
    opened = {}

    def rows(value, offset, limit):
        found, opened["error"] = booking_rows(odoo, members[value][offset:offset + limit])
        return found

    groups = group_list({v: len(ids) for v, ids in members.items()}, rows)
    return groups, error or opened.get("error")


@bp.route("/bookings")
def booking_list():
    every = sorted(booking_compare.booking_ids(g.db), reverse=True)
    odoo, error = _odoo_or_error()
    options = {k: label for k, (label, _field) in BOOKING_GROUPS.items()}
    groupby = group_by(options)
    page, rows = None, []
    if groupby:
        groups, group_error = grouped_bookings(odoo, every, groupby)
        error = error or group_error
    else:
        page = pager(len(every))
        # Odoo is asked for the page shown only
        rows, rows_error = booking_rows(odoo, every[page["offset"]:page["offset"] + page["limit"]])
        error = error or rows_error
        groups = group_rows(rows)
    return render_template("records/bookings.html", title="Bookings", rows=rows, groups=groups, groupby=groupby,
                           group_options=options, error=error, pager=page)


@bp.route("/bookings/<int:booking_id>")
def booking(booking_id):
    written = booking_compare.service_values(g.db, [booking_id]).get(booking_id, {})
    feeding = booking_compare.emails(g.db, booking_id)
    if not written and not feeding:
        abort(404)
    odoo, error = _odoo_or_error()
    info, odoo_row, children = {}, None, {}
    if odoo:
        try:
            info, odoo_row, children = booking_compare.read_odoo(odoo, booking_id)
        except Exception as exc:
            error = str(exc)
    sections = booking_compare.compare(info, odoo_row, written) if info else []
    current = (odoo_row or {}).get("state")
    steps = [(label, value == current) for value, label in (info.get("state") or {}).get("selection") or []]
    odoo_url = reference_url()
    return render_template(
        "records/booking.html", title=f"Booking {(odoo_row or {}).get('booking_number') or booking_id}", booking_id=booking_id,
        odoo_row=odoo_row, info=info, sections=sections, written=written, children=children, emails=feeding,
        made_by_service=booking_compare.created_children(g.db, booking_id), error=error, steps=steps,
        state_label=booking_compare.shown((odoo_row or {}).get("state"), info.get("state")),
        odoo_link=f"{odoo_url}/web#model={booking_compare.BOOKING}&id={booking_id}&view_type=form",
        differs=sum(1 for _t, rows in sections for r in rows if r["state"] == "differs"),
        times=store.record_times(g.db, booking_compare.BOOKING, [booking_id]).get(booking_id, (None, None)))
