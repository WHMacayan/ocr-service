"""The automatic export's schedule, worked out as Odoo's Scheduled Actions."""

from datetime import datetime

from app.payables import schedule


def at(text):
    return schedule.parse(text)


def test_every_unit_moves_on_one_interval_at_the_same_clock_time():
    start = at("2026-11-10 07:00")
    now = at("2026-11-10 07:00:30")
    assert schedule.following(start, 15, "minutes", now) == at("2026-11-10 07:15")
    assert schedule.following(start, 2, "hours", now) == at("2026-11-10 09:00")
    assert schedule.following(start, 1, "days", now) == at("2026-11-11 07:00")
    assert schedule.following(start, 1, "weeks", now) == at("2026-11-17 07:00")
    assert schedule.following(start, 1, "months", now) == at("2026-12-10 07:00")


def test_a_month_on_keeps_the_day_or_takes_the_months_last():
    assert schedule.add(at("2026-01-31 07:00"), 1, "months") == at("2026-02-28 07:00")
    assert schedule.add(at("2028-01-31 07:00"), 1, "months") == at("2028-02-29 07:00")   # leap year
    assert schedule.add(at("2026-11-30 07:00"), 3, "months") == at("2027-02-28 07:00")   # into the next year


def test_missed_exports_are_caught_up_by_one_run_not_a_burst():
    # Down from Monday to Thursday noon: the next export is Friday 07:00, not Tuesday, Wednesday, Thursday.
    assert schedule.following(at("2026-11-09 07:00"), 1, "days", at("2026-11-12 12:00")) == at("2026-11-13 07:00")


def test_once_runs_a_single_time():
    assert schedule.following(at("2026-11-10 07:00"), 1, "once", at("2026-11-10 08:00")) is None
    assert schedule.first(1, "once", at("2026-11-10 08:00")) == at("2026-11-10 08:00")


def test_switched_on_without_a_time_it_starts_one_interval_from_now():
    assert schedule.first(1, "days", datetime(2026, 11, 10, 8, 5, 42)) == at("2026-11-11 08:05")


def test_times_are_read_as_stored_or_as_the_page_sends_them():
    for text in ("2026-11-10 07:00:00", "2026-11-10 07:00", "2026-11-10T07:00", "2026-11-10T07:00:00Z"):
        assert schedule.parse(text) == datetime(2026, 11, 10, 7, 0)
    assert schedule.parse("next tuesday") is None and schedule.parse("") is None
