"""Bookings: the service's record of each booking set against what Odoo holds now."""

from flask import abort, g, render_template

from . import booking_compare, bp
from .base import _odoo_or_error, reference_url
from .ui import pager
from ..core import store


# â”€â”€ bookings: the service's record against Odoo â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@bp.route("/bookings")
def booking_list():
    every = sorted(booking_compare.booking_ids(g.db), reverse=True)
    page = pager(len(every))
    ids = every[page["offset"]:page["offset"] + page["limit"]]  # Odoo is asked for the page shown only
    written = booking_compare.service_values(g.db, ids)
    odoo, error = _odoo_or_error()
    odoo_rows, info = {}, {}
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
    rows = []
    for booking_id in ids:
        mine, theirs = written.get(booking_id, {}), odoo_rows.get(booking_id)
        differs = [f for f, w in mine.items() if theirs and f in info and not booking_compare.same(w["value"], theirs.get(f), info[f])]
        label = (theirs or {}).get("booking_number") or (mine.get("booking_number") or {}).get("value") or f"#{booking_id}"
        rows.append({"id": booking_id, "label": label, "odoo": theirs, "recorded": bool(mine), "differs": differs,
                     "emails": emails_per.get(booking_id, 0), "missing_in_odoo": bool(odoo_rows) and theirs is None,
                     **{f"{f}_shown": booking_compare.shown((theirs or {}).get(f), info.get(f))
                        for f in ("state", "factory_id", "forwarder_id", "etd", "eta", "port_of_loading", "port_of_discharge")}})
    return render_template("bookings.html", title="Bookings", rows=rows, error=error, pager=page)


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
        "booking.html", title=f"Booking {(odoo_row or {}).get('booking_number') or booking_id}", booking_id=booking_id,
        odoo_row=odoo_row, info=info, sections=sections, written=written, children=children, emails=feeding,
        made_by_service=booking_compare.created_children(g.db, booking_id), error=error, steps=steps,
        state_label=booking_compare.shown((odoo_row or {}).get("state"), info.get("state")),
        odoo_link=f"{odoo_url}/web#model={booking_compare.BOOKING}&id={booking_id}&view_type=form",
        differs=sum(1 for _t, rows in sections for r in rows if r["state"] == "differs"))
