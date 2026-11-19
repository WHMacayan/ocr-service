"""Booking rules: the same value written another way, the schedule moving with later documents, the
feeder beside the ocean vessel, the carrier beside the forwarder, impossible values, near numbers."""

from app.freight_booking import rules as freight
from app.freight_booking.rules import (BOOKING, CONFLICT, Booker, plausible_date, plausible_vessel, same_identifier,
    same_value)

from fakes import doc, mapping, odoo


class History:
    """What the pipeline would know: when each document arrived, the feeders the booking's papers named."""

    def __init__(self, received=None, feeders=()):
        self._received, self._feeders = received or {}, set(feeders)

    def received(self, booking_id, source):
        return self._received.get(source)

    def pre_carriers(self, booking_id):
        return self._feeders


def booker(fake, history=None, mappings=()):
    fake.add("res.company", name="Wholesale Tire Distributors", partner_id=1, currency_id=2)
    return Booker(fake, 1, list(mappings), lambda _doc, _booking: False, history)


def booking(fake, **values):
    return fake.add(BOOKING, company_id=1, booking_number="BK1", **values)


def conflicts(fake):
    return {(c["field_name"], c["new_value"]) for c in fake.rows(CONFLICT).values()}


def test_ports_companies_and_vessels_written_another_way_are_the_same():
    assert same_value("port_of_discharge", "USCHS CHARLESTON, SC", "CHARLESTON, SC, US")
    assert same_value("port_of_loading", "VNHPH HAIPHONG", "HAIPHONG ,VIETNAM")
    assert same_value("port_of_loading", "HAI PHONG", "HAIPHONG, VIETNAM")
    assert same_value("port_of_loading", "MAPTM TANGER MED", "MATPM TANGER MED")
    assert same_value("port_of_discharge", "SAV/Savannah (Country = US)", "SAVANNAH, GA")
    assert not same_value("port_of_loading", "HO CHI MINH CITY", "VUNG TAU ,VIET NAM")
    assert not same_value("port_of_discharge", "LOS ANGELES, CA", "HOUSTON,TX")
    assert same_value("forwarder_name", "TIANSHI LOGISTICS (VIETNAM) CO.,LTD", "TIANSHI LOGISTICS CO.,LTD.")
    assert same_value("forwarder_name", "DE WELL CONTAINER SHIPPING, INC.", "DeWell Group")
    cofo = mapping(role="factory", document_name="COFO", partner_id=25)
    assert same_value("factory_name", "VIETNAM COFO TIRES COMPANY LIMITED", "CÔNG TY TNHH LỐP COFO VIỆT NAM", [cofo])
    assert not same_value("forwarder_name", "K-APEX (THAILAND) CO., LTD.", "KLN Freight (USA) Inc.")
    assert same_value("vessel", "MAERSK MEMPHIS", "HAPAG-LLOYD MEMPHIS")
    assert not same_value("vessel", "YM WIND", "PHUC KHANH")


def test_impossible_dates_and_vessels_are_not_values():
    received = "2026-11-02 08:12:44"
    assert plausible_date("etd", "2026-11-03", received) == "2026-11-03"
    assert plausible_date("etd", "2026-03-11", received) == "2026-11-03"    # day and month read the wrong way round
    assert plausible_date("etd", "2026-06-11", received) == "2026-11-06"
    assert plausible_date("etd", "2024-01-05", received) is None            # fits neither way
    assert plausible_date("eta", "2027-01-05", received) == "2027-01-05"
    assert not plausible_vessel("SGSIN SINGAPORE") and not plausible_vessel("HAPAG-LLOYD DEMURRAGE")
    assert plausible_vessel("ONE MEISHAN") and plausible_vessel("MAPLE LEAF") and plausible_vessel("WAN HAI A19")


def test_numbers_apart_from_the_carrier_code_or_one_misread_character_are_the_same():
    assert same_identifier("SELGE0071900", "ONEYSELGE0071900")
    assert same_identifier("ONEYHANGH903840", "ONEYHANGH9038400")              # the last digit lost
    assert same_identifier("HLCUSGN2610CISM0", "HLCUSGN2610CISMO")              # 0 read as O
    assert not same_identifier("ONEYHANGK6515900", "ONEYHANGK6527500")
    assert not same_identifier("A26113", "A26114")                              # short numbers match exactly


def test_a_later_document_moves_the_schedule_and_an_older_one_never_disputes_it():
    fake = odoo()
    b = booking(fake, etd="2026-10-29", etd_source="HL-Booking Update 1.pdf")
    history = History(received={"HL-Booking Update 1.pdf": "2026-10-23 07:46:48"})
    bk = booker(fake, history)
    bk.consolidate(b, doc("mbl_hbl", source="DeWell HBL DRAFT.pdf", received="2026-11-08 05:14:47", etd="2026-11-12"))
    assert fake.rows(BOOKING)[b]["etd"] == "2026-11-12" and not conflicts(fake)
    assert any("updated from 2026-10-29 to 2026-11-12" in str(p) for p in fake.posts)

    fake.rows(BOOKING)[b]["etd_source"] = "DeWell HBL DRAFT.pdf"
    history._received["DeWell HBL DRAFT.pdf"] = "2026-11-08 05:14:47"
    bk.consolidate(b, doc("booking_confirmation", source="HL-Booking Update 1.pdf", received="2026-10-23 07:46:48",
                          etd="2026-10-29"))
    assert fake.rows(BOOKING)[b]["etd"] == "2026-11-12" and not conflicts(fake)


def test_documents_of_the_same_moment_that_disagree_still_open_a_conflict():
    fake = odoo()
    b = booking(fake, etd="2026-11-12", etd_source="a.pdf")
    bk = booker(fake, History(received={"a.pdf": "2026-11-08 05:00:00"}))
    bk.consolidate(b, doc("mbl_hbl", source="b.pdf", received="2026-11-08 05:00:00", etd="2026-11-20"))
    assert conflicts(fake) == {("etd", "2026-11-20")}


def test_the_feeder_vessel_on_a_bill_is_not_a_conflict():
    fake = odoo()
    b = booking(fake, vessel="ONE MEISHAN", voyage="033E(EC2)")
    bk = booker(fake, History(feeders={"PHUCKHANH", "441S"}))
    bk.consolidate(b, doc("mbl_hbl", source="FCR.PDF", vessel="PHUC KHANH", voyage="441S"))
    assert fake.rows(BOOKING)[b]["vessel"] == "ONE MEISHAN" and not conflicts(fake)

    fake2 = odoo()
    b2 = booking(fake2, vessel="PHUC KHANH")                       # the feeder was taken first
    booker(fake2, History(feeders={"PHUCKHANH"})).consolidate(b2, doc("booking_confirmation", vessel="ONE MEISHAN"))
    assert fake2.rows(BOOKING)[b2]["vessel"] == "ONE MEISHAN" and not conflicts(fake2)


def test_a_carrier_never_disputes_the_forwarder_and_a_forwarder_replaces_a_carrier():
    fake = odoo()
    b = booking(fake, forwarder_name="DE WELL CONTAINER SHIPPING, INC.")
    booker(fake).consolidate(b, doc("booking_confirmation", forwarder_name="HAPAG-LLOYD (VIETNAM) LTD"))
    assert fake.rows(BOOKING)[b]["forwarder_name"] == "DE WELL CONTAINER SHIPPING, INC." and not conflicts(fake)

    fake2 = odoo()
    b2 = booking(fake2, forwarder_name="HAPAG-LLOYD (VIETNAM) LTD", forwarder_id=3)
    dewell = mapping(document_name="DE WELL", partner_id=4)
    booker(fake2, mappings=[dewell]).consolidate(b2, doc("mbl_hbl", forwarder_name="DE WELL CONTAINER SHIPPING,INC."))
    assert fake2.rows(BOOKING)[b2]["forwarder_name"] == "DE WELL CONTAINER SHIPPING,INC."
    assert fake2.rows(BOOKING)[b2]["forwarder_id"] == 4 and not conflicts(fake2)


def test_impossible_values_never_reach_the_booking():
    fake = odoo()
    b = booking(fake, vessel="WAN HAI A19", etd="2026-11-03")
    booker(fake).consolidate(b, doc("mbl_hbl", received="2026-11-02 08:12:44", vessel="SGSIN SINGAPORE", etd="2026-03-11"))
    assert fake.rows(BOOKING)[b]["vessel"] == "WAN HAI A19" and fake.rows(BOOKING)[b]["etd"] == "2026-11-03"
    assert not conflicts(fake)


def test_an_invoice_finds_its_booking_by_a_number_written_another_way():
    fake = odoo()
    b = fake.add(BOOKING, company_id=1, booking_number="ONEYSELGE0071900")
    c = fake.add(BOOKING, company_id=1, booking_number="X1", master_bl="ONEYHANGH9038400")
    bk = booker(fake)
    assert bk.take(doc("freight_invoice", booking_number="SELGE0071900"))[2] == b
    assert bk.take(doc("freight_invoice", master_bl="ONEYHANGH903840"))[2] == c
    assert bk.take(doc("freight_invoice", master_bl="ONEYHANGK6527500"))[0] == "waiting"


def test_without_the_pipelines_records_the_old_rules_hold():
    assert isinstance(Booker(odoo(), 1, [], lambda *_: True).history, freight._History)
