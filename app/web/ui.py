"""What the templates use to show things: times in the viewer's zone, search words marked, and Odoo's
pager."""

import re
from datetime import datetime, timezone

from flask import request, url_for
from markupsafe import escape, Markup

from . import bp


@bp.app_template_filter("when")
def when(value, short=False):
    """A stored UTC time, marked so the page script shows it in the viewer's time zone.
    Without the script it reads as UTC; UTC also stays in the tooltip. Short: the time
    alone today, else the date, for lists."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return escape(value)
    if moment.tzinfo:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    utc = moment.strftime("%Y-%m-%d %H:%M:%S")
    extra = " data-short" if short else ""
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
