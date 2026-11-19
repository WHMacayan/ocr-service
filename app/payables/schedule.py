"""When Freight Payables exports by itself, as Odoo's Scheduled Actions: every N minutes, hours, days,
weeks or months, or once, from a next export time that is worked out after each export and can be
changed on the Freight Payables page. Times are UTC, like every other time the service keeps.
"""

import calendar
from datetime import datetime, timedelta

UNITS = {"minutes": "Minutes", "hours": "Hours", "days": "Days", "weeks": "Weeks", "months": "Months",
         "once": "Once only"}
STORED = "%Y-%m-%d %H:%M:%S"


def parse(value):
    """A stored or typed UTC time (2026-11-10 07:00, 2026-11-10T07:00:00Z…), or None."""
    value = (value or "").strip().rstrip("Z").replace("T", " ")
    for layout in (STORED, "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(value, layout)
        except ValueError:
            pass
    return None


def add(moment, every, unit):
    """One interval later. A month later keeps the day, or the month's last day when it is shorter."""
    if unit == "months":
        month = moment.month - 1 + every
        year, month = moment.year + month // 12, month % 12 + 1
        return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))
    return moment + timedelta(**{unit: every})


def following(next_at, every, unit, now):
    """The next export after one ran at next_at: one interval on, at the same clock time; after a
    stop it moves past now, so missed exports are caught up by one run, not a burst. Once: None."""
    if unit == "once":
        return None
    later = add(next_at, every, unit)
    while later <= now:
        later = add(later, every, unit)
    return later


def first(every, unit, now):
    """A schedule switched on with no next export set: one interval from now (Once: now)."""
    return now if unit == "once" else add(now.replace(second=0, microsecond=0), every, unit)
