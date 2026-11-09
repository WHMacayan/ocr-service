"""The activity log page."""

from flask import g, render_template, request

from . import bp
from .ui import group_by, group_list, group_rows, pager
from ..core import store


ACTIVITY_LABELS = {"settings": "Setting changes", "security": "Sign-ins and security",
                   "processing": "Processing", "odoo": "Odoo writes"}
# Group By: the column offered, and the SQL its rows are sorted and counted on.
ACTIVITY_GROUPS = {"kind": "Type", "actor": "By", "level": "Level", "day": "Day"}
GROUP_SQL = {"kind": "kind", "actor": "actor", "level": "level", "day": "date(at)"}


def actor_label(actor):
    return "The service" if actor == "worker" else (actor or "").replace("admin from ", "Admin, ")


GROUP_LABELS = {"kind": lambda k: ACTIVITY_LABELS.get(k, k), "actor": actor_label, "level": str.capitalize}


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
    groupby = group_by(ACTIVITY_GROUPS)
    page, rows = None, []
    if groupby:
        # Every group of every entry the filter keeps, newest day first; a group's entries load when it opens.
        column = f"NULLIF({GROUP_SQL[groupby]}, '')"
        totals = dict(g.db.execute(f"SELECT {column}, COUNT(*) FROM activity{sql_where} GROUP BY 1", args).fetchall())
        groups = group_list(totals, lambda value, offset, limit: g.db.execute(
            f"SELECT * FROM activity{sql_where}{' AND' if where else ' WHERE'} {column} IS ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?", (*args, value, limit, offset)).fetchall(),
            GROUP_LABELS.get(groupby), reverse=groupby == "day")
    else:
        page = pager(g.db.execute(f"SELECT COUNT(*) FROM activity{sql_where}", args).fetchone()[0])
        rows = g.db.execute(f"SELECT * FROM activity{sql_where} ORDER BY id DESC LIMIT ? OFFSET ?",
                            (*args, page["limit"], page["offset"])).fetchall()
        groups = group_rows(rows)
    counts = dict(g.db.execute("SELECT kind, COUNT(*) FROM activity GROUP BY kind").fetchall())
    return render_template("activity/activity.html", title="Activity", groups=groups, rows=rows, kind=kind, query=query,
                           pager=page, labels=ACTIVITY_LABELS, counts=counts, total=sum(counts.values()),
                           groupby=groupby, group_options=ACTIVITY_GROUPS, actor_label=actor_label,
                           keep_days=store.get_setting(g.db, "activity_days"))
