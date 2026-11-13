"""What the templates use to show things: times in the viewer's zone, search words marked, and Odoo's
pager."""

import re
from datetime import datetime, timezone

from flask import request, url_for
from markupsafe import escape, Markup

from . import bp


@bp.app_template_filter("when")
def when(value, short=False, time=False):
    """A stored UTC time, marked so the page script shows it in the viewer's time zone.
    Without the script it reads as UTC; UTC also stays in the tooltip. Short: the time
    alone today, else the date, for lists. Time: the time of day alone, for entries listed under their day."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return escape(value)
    if moment.tzinfo:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    utc = moment.strftime("%Y-%m-%d %H:%M:%S")
    extra = " data-time" if time else (" data-short" if short else "")
    return Markup(f'<time datetime="{moment:%Y-%m-%dT%H:%M:%S}Z" title="{utc} UTC"{extra}>{utc} UTC</time>')


@bp.app_template_filter("highlight")
def highlight(text, terms):
    """The text, escaped, with every search term wrapped in <mark>."""
    text = str(text or "")
    words = sorted({t for t in terms or [] if t}, key=len, reverse=True)
    if not words:
        return escape(text)
    pattern = re.compile("|".join(re.escape(w) for w in words), re.IGNORECASE)
    out, last = [], 0
    for m in pattern.finditer(text):
        out += [escape(text[last:m.start()]), Markup("<mark>"), escape(m.group(0)), Markup("</mark>")]
        last = m.end()
    out.append(escape(text[last:]))
    return Markup("").join(out)


# ── the pager, as Odoo's: "1-80 / 374", previous / next, and a range you can type ──

PAGE_SIZE = 80
PAGE_MAX = 500
_RANGE = re.compile(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*")


def pager(total):
    """The page of a list to show: ?range=1-200 (typed into the pager) or ?offset=&limit=.
    Like Odoo's pager, previous on the first page goes to the last one and next on the last to the first."""
    offset = max(request.args.get("offset", 0, type=int), 0)
    limit = request.args.get("limit", PAGE_SIZE, type=int)
    typed = _RANGE.fullmatch(request.args.get("range") or "")
    if typed:
        first = max(int(typed.group(1)), 1)
        last = max(int(typed.group(2) or first), first)
        offset, limit = first - 1, last - first + 1
    limit = min(max(limit, 1), PAGE_MAX)
    if offset >= total:
        offset = (max(total - 1, 0) // limit) * limit
    keep = {k: v for k, v in request.args.items() if k not in ("offset", "limit", "range")}

    def link(at):
        return url_for(request.endpoint, **(request.view_args or {}), **keep, offset=at or None,
                       limit=None if limit == PAGE_SIZE else limit)

    last_page = (max(total - 1, 0) // limit) * limit
    return {"offset": offset, "limit": limit, "total": total, "first": offset + 1 if total else 0,
            "last": min(offset + limit, total), "keep": keep, "turns": total > limit,
            "prev": link(offset - limit if offset else last_page), "next": link(offset + limit if offset + limit < total else 0)}


# ── group by, as Odoo's: ?groupby= picks a column; every record comes under a header per value ──
#
# As in Odoo the groups cover every record the filter keeps, not one page: each header shows its
# group's count and opens on its first rows; an open group has its own pager (?group= is the open
# group, ?offset= where its page starts).

NONE_LABEL = "None"
GROUP_PAGE = 80


def group_by(options):
    """The column chosen with ?groupby=, if the page offers it; options: {key: label}."""
    key = request.args.get("groupby")
    return key if key in options else None


@bp.app_template_global()
def group_menu(options):
    """The Group By menu: the chosen column and a link per column (and one to ungroup), keeping the
    page's filters but going back to its first page."""
    keep = {k: v for k, v in request.args.items() if k not in ("offset", "range", "groupby", "group")}

    def link(key):
        return url_for(request.endpoint, **(request.view_args or {}), **keep, groupby=key)

    current = group_by(options)
    return {"current": current, "label": options.get(current), "none": link(None),
            "links": [(key, label, link(key)) for key, label in options.items()]}


def value_order(value):
    """Groups A to Z."""
    return str(value).lower() if isinstance(value, str) else value


def in_order(values, order=value_order, reverse=False):
    """The values sorted, the empty one (None) last."""
    return sorted((v for v in values if v is not None), key=order, reverse=reverse) + [None] * (None in values)


def group_list(counts, rows, label=None, order=value_order, reverse=False):
    """Every group of a grouped list: [{value, key, label, count, url, rows, pager}], folded (rows None)
    but for the one opened with ?group=, which holds a page of its rows from ?offset= and, when it has
    more than a page, its pager (as Odoo's: previous on the first page goes to the last).
    counts: {value: n} over every record; rows(value, offset, limit): a group's rows.
    Groups come in order(value) order (reversed: newest day first), the empty one last. key is the
    value as ?group= carries it ("" for no value)."""
    asked = request.args.get("group")
    offset = max(request.args.get("offset", 0, type=int), 0)
    keep = {k: v for k, v in request.args.items() if k not in ("offset", "range", "group")}

    def link(key, at=0):
        return url_for(request.endpoint, **(request.view_args or {}), **keep, group=key, offset=at or None)

    groups = []
    for value in in_order(counts, order, reverse):
        key = "" if value is None else str(value)
        count = counts[value]
        group = {"value": value, "key": key, "count": count, "url": link(key), "rows": None, "pager": None,
                 "label": NONE_LABEL if value is None else (label(value) if label else str(value))}
        if asked == key:
            last_page = (max(count - 1, 0) // GROUP_PAGE) * GROUP_PAGE
            at = min(offset, last_page)
            group["rows"] = rows(value, at, GROUP_PAGE)
            if count > GROUP_PAGE:
                group["pager"] = {"first": at + 1, "last": min(at + GROUP_PAGE, count), "total": count,
                                  "prev": link(key, at - GROUP_PAGE if at else last_page),
                                  "next": link(key, at + GROUP_PAGE if at + GROUP_PAGE < count else 0)}
        groups.append(group)
    return groups


def group_ids(values):
    """{value: [ids, newest first]} from {id: value}, for lists read from Odoo by id."""
    out = {}
    for record_id in sorted(values, reverse=True):
        value = values[record_id]
        out.setdefault(None if value in (None, "") else value, []).append(record_id)
    return out


def group_rows(rows, value=None, label=None):
    """[{value, key, label, count, rows}]: a short list's rows, all of them shown, under one open
    header per value (A to Z, empty ones last). No value: one group without a header (none for no rows)."""
    if value is None:
        return [{"value": None, "key": None, "label": None, "count": len(rows), "rows": rows}] if rows else []
    groups = {}
    for row in rows:
        v = value(row)
        groups.setdefault(None if v in (None, "") else v, []).append(row)
    return [{"value": v, "key": "" if v is None else str(v), "count": len(groups[v]), "rows": groups[v],
             "label": NONE_LABEL if v is None else (label(v) if label else str(v))}
            for v in in_order(groups)]
