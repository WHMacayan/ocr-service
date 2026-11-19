"""The Booking Report's agreed rate per container reaches the booking's own Cost per Container."""

from openpyxl import Workbook

from app.freight_booking import booking_report as br
from app.freight_booking.rules import BOOKING

from fakes import FakeOdoo


def report(*rows, tab="26OCT", header_row=1):
    wb = Workbook()
    ws = wb.active
    ws.title = tab
    for _ in range(header_row - 1):
        ws.append(["LOADED BOOKINGS", "WITH INVOICE"])
    ws.append(["PO#", "Factory", "BOOKING DATE", "FORWARDER", "FREIGHT", "Departure Port", "PORT OF DISCHARGE",
               "Booked Place", "40HQ", "ETD", "ETA", "Booking confirmation"])
    for po, forwarder, freight, number in rows:
        ws.append([po, "x", None, forwarder, freight, None, None, None, 1, None, None, number])
    return wb


def test_rates_are_read_as_people_type_them():
    assert br.rate_of(6900) == ("rate", 6900.0)
    assert br.rate_of(" 5,500 ") == ("rate", 5500.0)
    assert br.rate_of("FREIGHT COLLECT")[0] == br.rate_of("FRT COLLECT")[0] == br.rate_of("collect")[0] == "collect"
    assert br.rate_of("4900 / 5000")[0] == "two"
    assert br.rate_of(None)[0] == "none" and br.rate_of("TBA")[0] == "unreadable"


def test_rows_are_read_from_every_month_tab_below_their_header():
    rows = br.read_rows(report(("CC1-OCT-01", "APEX", 6900, "BKKM94310800"), header_row=3))
    assert rows == [{"tab": "26OCT", "row": 4, "po": "CC1-OCT-01", "forwarder": "APEX", "numbers": ["BKKM94310800"],
                     "freight": 6900}]
    assert br.read_rows(Workbook()) == []  # a tab without the Booking Report's headers is skipped


def test_the_rate_goes_to_the_booking_found_by_number_or_inside_its_mbl():
    odoo = FakeOdoo()
    by_number = odoo.add(BOOKING, booking_number="BKKM94310800", master_bl=False, cost_per_container=0)
    by_mbl = odoo.add(BOOKING, booking_number=False, master_bl="ONEYSUBG27923900", cost_per_container=0)
    rows = br.read_rows(report(("A", "APEX", 6900, "BKKM94310800"), ("B", "QUARTZ", 6600, "SUBG27923900")))

    outcomes, summary = br.import_rates(odoo, rows)  # dry run
    assert summary[br.SET] == 2 and odoo.rows(BOOKING)[by_number]["cost_per_container"] == 0

    br.import_rates(odoo, rows, apply=True)
    assert odoo.rows(BOOKING)[by_number]["cost_per_container"] == 6900
    assert odoo.rows(BOOKING)[by_mbl]["cost_per_container"] == 6600


def test_a_rate_a_person_entered_is_never_replaced():
    odoo = FakeOdoo()
    booking = odoo.add(BOOKING, booking_number="15579649", master_bl=False, cost_per_container=7000)
    rows = br.read_rows(report(("A", "DEWELL", 7350, "15579649"), ("B", "DEWELL", 7000, "15579649")))
    outcomes, summary = br.import_rates(odoo, rows, apply=True)
    assert odoo.rows(BOOKING)[booking]["cost_per_container"] == 7000
    assert [o["outcome"] for o in outcomes] == [br.CONFLICT, br.CONFLICT]  # the report itself disagrees


def test_what_is_not_a_clear_rate_is_reported_not_written():
    odoo = FakeOdoo()
    for number in ("11111111", "22222222", "33333333", "44444444"):
        odoo.add(BOOKING, booking_number=number, master_bl=False, cost_per_container=0)
    rows = br.read_rows(report(("A", "HYT", "FREIGHT COLLECT", "11111111"), ("B", "APEX", "4900 / 5000", "22222222"),
                               ("C", "APEX", 20, "33333333"), ("D", "CMA", None, "44444444"),
                               ("E", "APEX", 6900, "99999999"), ("F", "APEX", 6900, None)))
    outcomes, _summary = br.import_rates(odoo, rows, apply=True)
    assert [o["outcome"] for o in outcomes] == [br.FREIGHT_COLLECT, br.TWO_RATES, br.SUSPICIOUS, br.NO_RATE,
                                               br.NOT_FOUND, br.NO_NUMBER]
    assert not any(b["cost_per_container"] for b in odoo.rows(BOOKING).values())


def test_the_row_by_row_result_is_saved_for_the_team(tmp_path):
    odoo = FakeOdoo()
    odoo.add(BOOKING, booking_number="15579649", master_bl=False, cost_per_container=0)
    outcomes, _ = br.import_rates(odoo, br.read_rows(report(("A", "DEWELL", 7350, "15579649"))))
    br.write_report(tmp_path / "result.xlsx", outcomes)
    from openpyxl import load_workbook
    ws = load_workbook(tmp_path / "result.xlsx").active
    assert [c.value for c in ws[2]][-1] == br.SET and ws[2][4].value == "15579649"
