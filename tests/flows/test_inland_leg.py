"""Inland (rail) leg: an ocean booking whose container goes on by rail to an inland ramp. The ramp is kept apart
from the port of discharge (60 port conflicts in 1103 were MEMPHIS against SAVANNAH)."""

from app.freight_booking.rules import BOOKING, CONFLICT

from fakes import doc, make_booker, odoo


def booking_of(fake):
    (row,) = fake.rows(BOOKING).values()
    return row


def test_an_inland_city_read_as_port_of_discharge_becomes_the_rail_ramp():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="bc.pdf", booking_number="EBKG18603226",
                    port_of_discharge="SAVANNAH, GA"))
    booker.take(doc("mbl_hbl", source="bl.pdf", booking_number="EBKG18603226", port_of_discharge="USMEM MEMPHIS, TN"))
    row = booking_of(fake)
    assert row["port_of_discharge"] == "SAVANNAH, GA" and row["rail_ramp"] == "USMEM MEMPHIS, TN"
    assert not fake.rows(CONFLICT)


def test_an_inland_city_never_becomes_the_port_even_first():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="bc.pdf", booking_number="EBKG18603226", port_of_discharge="DALLAS"))
    row = booking_of(fake)
    assert not row.get("port_of_discharge") and row["rail_ramp"] == "DALLAS"


def test_the_seaport_replaces_an_inland_city_an_older_booking_holds_as_port():
    fake = odoo()
    booker = make_booker(fake)
    fake.add(BOOKING, company_id=1, booking_number="EBKG18603226", port_of_discharge="USMEM MEMPHIS, TN")
    booker.take(doc("mbl_hbl", source="bl.pdf", booking_number="EBKG18603226", port_of_discharge="SAVANNAH, GA"))
    row = booking_of(fake)
    assert row["port_of_discharge"] == "SAVANNAH, GA" and row["rail_ramp"] == "USMEM MEMPHIS, TN"
    assert not fake.rows(CONFLICT)


def test_the_reference_naming_an_inland_destination_gives_the_ramp_until_a_document_prints_one():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="bc.pdf", booking_number="EBKG18602883",
                    subject="SI&VGM: RELEASE BKG : EBKG18602883 // PO# DALLAS18-26AUG-12 // CY-CY"))
    row = booking_of(fake)
    assert row["rail_ramp"] == "DALLAS" and row["rail_ramp_source"].startswith("shipment reference")
    booker.take(doc("shipping_notice", source="an.pdf", booking_number="EBKG18602883", rail_ramp="DALLAS RAMP, TX"))
    assert booking_of(fake)["rail_ramp"] == "DALLAS RAMP, TX"


def test_a_seaport_destination_has_no_inland_leg():
    fake = odoo()
    make_booker(fake).take(doc("booking_confirmation", source="bc.pdf", booking_number="EBKG19094572",
                               subject="RELEASE BKG : EBKG19094572 // PO# LAX19-26-OCT-11B-ARS", port_of_discharge="LOS ANGELES"))
    assert not booking_of(fake).get("rail_ramp")


def test_an_odoo_without_the_ramp_field_keeps_the_old_behaviour():
    fake = odoo()
    fake.extra_fields["b2b.ocr.booking"] -= {"rail_ramp", "rail_ramp_source"}
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="bc.pdf", booking_number="EBKG18603226", port_of_discharge="SAVANNAH"))
    booker.take(doc("mbl_hbl", source="bl.pdf", booking_number="EBKG18603226", port_of_discharge="MEMPHIS, TN"))
    row = booking_of(fake)
    assert row["port_of_discharge"] == "SAVANNAH" and "rail_ramp" not in row
