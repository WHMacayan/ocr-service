"""The activity log page."""

from datetime import date, datetime, timedelta, timezone

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


PURPOSE_LABELS = {"freight_booking": "Bookings", "bill_matching": "Scraped bills", "customs_entry": "Customs",
                  "freight_payables": "Freight payables"}
DAYS_SHOWN = 7


def runs(entries):
    """The day's entries, newest first, with the service's routine work folded: back-to-back info entries of the
    worker become one block {entries, emails, writes, first, last}; anything else stays one entry."""
    blocks = []
    for a in entries:
        routine = a["actor"] == "worker" and a["level"] == "info"
        if routine and blocks and isinstance(blocks[-1], dict):
            blocks[-1]["entries"].append(a)
        elif routine:
            blocks.append({"entries": [a]})
        else:
            blocks.append(a)
    for b in blocks:
        if isinstance(b, dict):
            b["emails"] = sum(1 for a in b["entries"] if a["kind"] == "processing")
            b["writes"] = sum(1 for a in b["entries"] if a["kind"] == "odoo")
            b["last"], b["first"] = b["entries"][0]["at"], b["entries"][-1]["at"]
    return blocks


def digest():
    """Activity as a check-up: each mailbox now, the last day's problems, and a summary per day with the
    chosen day's entries (?day=, the newest by default)."""
    mailboxes = g.db.execute(
        "SELECT id, name, purpose, active, auto_sync, status, last_sync, last_error, "
        "CASE WHEN auto_sync AND last_sync IS NOT NULL THEN datetime(last_sync, '+' || sync_interval || ' minutes') END "
        "AS next_sync FROM mailbox ORDER BY active DESC, name").fetchall()
    attention = g.db.execute("SELECT * FROM activity WHERE level IN ('error', 'warning') AND at >= datetime('now', '-1 day') "
                             "ORDER BY id DESC LIMIT 30").fetchall()
    offset = max(request.args.get("days", 0, type=int), 0)
    days = [r[0] for r in g.db.execute("SELECT DISTINCT date(at) FROM activity ORDER BY 1 DESC LIMIT ? OFFSET ?",
                                       (DAYS_SHOWN + 1, offset))]
    older = len(days) > DAYS_SHOWN
    days = days[:DAYS_SHOWN]
    summary = {}
    if days:
        marks = ", ".join("?" * len(days))
        for day, kind, level, count in g.db.execute(
                f"SELECT date(at), kind, level, COUNT(*) FROM activity WHERE date(at) IN ({marks}) "
                f"GROUP BY 1, 2, 3", days):
            s = summary.setdefault(day, {"processing": 0, "odoo": 0, "settings": 0, "security": 0, "problems": 0, "total": 0})
            s[kind] = s.get(kind, 0) + count
            s["total"] += count
            if level in ("error", "warning"):
                s["problems"] += count
    today = datetime.now(timezone.utc).date()  # shortcut: days are UTC days, as stored; a viewer far from UTC sees evening entries on the next day
    names = {today.isoformat(): "Today", (today - timedelta(days=1)).isoformat(): "Yesterday"}
    day_labels = {d: names.get(d) or date.fromisoformat(d).strftime("%a %d %b %Y") for d in days}
    opened = request.args.get("day") if request.args.get("day") in days else (days[0] if days else None)
    blocks = runs(g.db.execute("SELECT * FROM activity WHERE date(at) = ? ORDER BY id DESC LIMIT 2000",
                               (opened,)).fetchall()) if opened else []
    return render_template("activity/digest.html", title="Activity", mailboxes=mailboxes, attention=attention,
                           days=days, day_labels=day_labels, summary=summary, opened=opened, blocks=blocks, older=older, offset=offset,
                           newer=max(offset - DAYS_SHOWN, 0) if offset else None, purposes=PURPOSE_LABELS,
                           labels=ACTIVITY_LABELS, actor_label=actor_label, keep_days=store.get_setting(g.db, "activity_days"))


@bp.route("/activity")
def activity():
    kind = request.args.get("kind")
    query = (request.args.get("q") or "").strip()
    if not (kind or query or request.args.get("groupby") or request.args.get("view") == "log"):
        return digest()
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
