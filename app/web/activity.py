"""The activity log page."""

from flask import g, render_template, request

from . import bp
from .ui import pager
from ..core import store


ACTIVITY_LABELS = {"settings": "Setting changes", "security": "Sign-ins and security",
                   "processing": "Processing", "odoo": "Odoo writes"}


@bp.route("/activity")
def activity():
    kind = request.args.get("kind")
    query = (request.args.get("q") or "").strip()
    where, args = [], []
    if kind in ACTIVITY_LABELS:
        where.append("kind = ?")
        args.append(kind)
    if query:
        where.append("(summary LIKE ? OR detail LIKE ?)")
        args += [f"%{query}%"] * 2
    sql_where = " WHERE " + " AND ".join(where) if where else ""
    page = pager(g.db.execute(f"SELECT COUNT(*) FROM activity{sql_where}", args).fetchone()[0])
    rows = g.db.execute(f"SELECT * FROM activity{sql_where} ORDER BY id DESC LIMIT ? OFFSET ?",
                        (*args, page["limit"], page["offset"])).fetchall()
    counts = dict(g.db.execute("SELECT kind, COUNT(*) FROM activity GROUP BY kind").fetchall())
    return render_template("activity/activity.html", title="Activity", rows=rows, kind=kind, query=query, pager=page,
                           labels=ACTIVITY_LABELS, counts=counts, total=sum(counts.values()),
                           keep_days=store.get_setting(g.db, "activity_days"))
