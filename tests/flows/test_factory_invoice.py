"""Factory invoice as starting point (design v1, 12 Nov 2026): a commercial invoice starts the booking, with or
without a booking number; each invoice keeps its own containers (one freight PO per invoice); the forwarder's
documents find the numberless booking and complete it; a certificate of origin is no invoice."""

from app.freight_booking import reading
from app.freight_booking.rules import BOOKING, CHANGE, CONFLICT, CONTAINER, INVOICE

from fakes import doc, make_booker, odoo

SAILING = dict(vessel="MAERSK SERANGOON", voyage="627W", port_of_loading="TANGIER, MOROCCO",
               port_of_discharge="BALTIMORE, MD, USA")


def sentury_ci(number="F26AR06MO343", containers=("HLBU3090445", "BSIU8296616"), source="ci1.pdf", **extra):
    values = dict(SAILING, invoices=[number], containers=list(containers), factory_name="SENTURY TIRE MOROCCO",
                  bl_number=None, shipment_reference="CC11155BALTIMORE6-JUNE-32")
    values.update(extra)
    return doc("commercial_invoice", source=source, sender="docs@sentury.com", received="2026-07-01T08:00:00",
               **values)


def only_booking(fake):
    (booking_id,) = fake.rows(BOOKING)
    return booking_id, fake.rows(BOOKING)[booking_id]


def tags(fake):
    lines = fake.rows(INVOICE)
    return {c["container_no"]: lines[c["invoice_id"]]["invoice_number"] if c.get("invoice_id") else None
            for c in fake.rows(CONTAINER).values()}


def test_a_factory_invoice_without_booking_number_starts_the_booking():
    fake = odoo()
    state, _note, booking_id = make_booker(fake).take(sentury_ci())
    assert state == "done"
    booking = fake.rows(BOOKING)[booking_id]
    assert booking["booking_number"] is False                     # sent as False, never ""
    assert booking["vessel"] == "MAERSK SERANGOON" and booking["voyage"] == "627W"
    assert booking["port_of_discharge"] == "BALTIMORE, MD, USA"
    assert booking["shipment_reference"] == "CC11155BALTIMORE6-JUNE-32"
    (line,) = fake.rows(INVOICE).values()
    assert line["invoice_number"] == "F26AR06MO343" and line["factory_name"] == "SENTURY TIRE MOROCCO"
    assert tags(fake) == {"HLBU3090445": "F26AR06MO343", "BSIU8296616": "F26AR06MO343"}


def test_a_sentury_bl_hash_is_the_booking_number():
    fake = odoo()
    make_booker(fake).take(sentury_ci(bl_number="27302472"))
    _id, booking = only_booking(fake)
    assert booking["booking_number"] == "27302472"


def test_a_genrub_invoice_with_its_packing_list_gives_the_booking_number_and_dates():
    fake = odoo()
    ci = doc("commercial_invoice", source="afterload-035GX63876 GRKA-P-2026399.pdf", sender="x@ty-tyre.com",
             received="2026-09-24T08:00:00", invoices=["GRKAP2026399"], containers=["WHSU6361440"],
             booking_number="035GX63876", vessel="WAN HAI 276", voyage="N064", etd="2026-09-30",
             eta="2026-10-21", port_of_loading="LAEM CHABANG, THAILAND", port_of_discharge="LOS ANGELES",
             factory_name="GENERAL RUBBER (THAILAND) CO.,LTD")
    make_booker(fake).take(ci)
    _id, booking = only_booking(fake)
    assert booking["booking_number"] == "035GX63876"
    assert (booking["etd"], booking["eta"]) == ("2026-09-30", "2026-10-21")


def test_a_second_invoice_of_the_shipment_adds_its_own_containers_without_a_container_change():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(sentury_ci())
    booker.take(sentury_ci("F26AR06MO344", containers=("TGHU1234567",), source="ci2.pdf"))
    only_booking(fake)
    assert not fake.rows(CHANGE)
    assert tags(fake) == {"HLBU3090445": "F26AR06MO343", "BSIU8296616": "F26AR06MO343",
                          "TGHU1234567": "F26AR06MO344"}


def test_the_confirmation_finds_the_numberless_booking_by_its_sailing_and_gives_the_number():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(sentury_ci())
    state, _note, booking_id = booker.take(doc("booking_confirmation", source="bc.pdf", booking_number="MAEU263021528",
                                               received="2026-07-02T08:00:00", **SAILING))
    assert state == "done"
    _id, booking = only_booking(fake)                              # no second booking
    assert booking["booking_number"] == "MAEU263021528"


def test_a_confirmation_on_another_sailing_starts_its_own_booking():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(sentury_ci())
    booker.take(doc("booking_confirmation", source="bc.pdf", booking_number="MAEU999001", **dict(SAILING, voyage="701W")))
    assert len(fake.rows(BOOKING)) == 2


def test_the_factory_invoice_never_disputes_the_forwarders_schedule():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="bc.pdf", booking_number="BK100001", containers=["HLBU3090445"],
                    vessel="MAERSK SERANGOON", voyage="628W"))
    booker.take(sentury_ci())                                      # found by its container
    _id, booking = only_booking(fake)
    assert booking["voyage"] == "628W" and not fake.rows(CONFLICT)


def test_a_revised_invoice_finds_its_booking_by_the_invoice_number():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(sentury_ci(vessel=None))
    state, _note, _id = booker.take(sentury_ci(source="ci1-revised.pdf", containers=(), vessel=None))
    assert state == "done" and len(fake.rows(BOOKING)) == 1


def test_a_bill_listing_every_invoiced_container_and_more_adds_them_untagged():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(sentury_ci())
    booker.take(doc("mbl_hbl", source="bl.pdf", master_bl="HLCUCA6260609209",
                    containers=["HLBU3090445", "BSIU8296616", "TGHU1234567"]))
    assert not fake.rows(CHANGE)
    assert tags(fake)["TGHU1234567"] is None                       # its invoice will tag it
    booker.take(sentury_ci("F26AR06MO344", containers=("TGHU1234567",), source="ci2.pdf"))
    assert tags(fake)["TGHU1234567"] == "F26AR06MO344"


def test_a_bill_naming_other_containers_still_goes_to_a_person():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(sentury_ci(bl_number="27302472"))
    booker.take(doc("mbl_hbl", source="bl.pdf", master_bl="HLCUCA6260609209", booking_number="27302472",
                    containers=["TGHU1234567"]))
    assert len(fake.rows(CHANGE)) == 1


def test_an_odoo_still_needing_booking_numbers_keeps_the_invoice_waiting():
    fake = odoo()
    fake.required.add((BOOKING, "booking_number"))
    state, note, _id = make_booker(fake).take(sentury_ci())
    assert state == "waiting" and "needs one" in note and not fake.rows(BOOKING)


def test_a_bill_tying_the_numberless_booking_to_another_numbered_one_waits_for_a_person():
    fake = odoo()
    booker = make_booker(fake)
    fake.add(BOOKING, company_id=1, booking_number="MAEU263021528", vessel="OTHER SHIP")
    booker.take(sentury_ci())
    state, note, _id = booker.take(doc("mbl_hbl", source="bl.pdf", master_bl="HLCUCA6260609209",
                                       containers=["HLBU3090445"], booking_number="MAEU263021528"))
    assert state == "done"                     # the booking number decides: the numbered booking takes it
    numberless = [b for b in fake.rows(BOOKING).values() if not b["booking_number"]]
    assert len(numberless) == 1                # the invoice's booking is not given a number another one holds


def test_a_certificate_of_origin_is_not_relevant_and_is_a_known_type():
    assert "certificate_of_origin" in reading.FREIGHT_CLASSES
    assert "certificate_of_origin" in reading.NOT_RELEVANT


def test_a_late_invoice_joins_the_booking_whose_other_invoices_have_their_pos():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(sentury_ci())
    booking_id, booking = only_booking(fake)
    booking["state"] = "po_created"                                # the first invoice's PO is drafted
    booker.take(sentury_ci("F26AR06MO344", containers=("HLBU3090445", "TGHU1234567"), source="ci2.pdf"))
    only_booking(fake)
    assert tags(fake)["TGHU1234567"] == "F26AR06MO344"


def test_a_reused_container_on_another_vessel_starts_a_new_booking():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(sentury_ci())
    _id, booking = only_booking(fake)
    booking["state"] = "po_created"
    booker.take(sentury_ci("F26AR09MO400", containers=("HLBU3090445",), source="ci9.pdf", vessel="MSC ANNA",
                           voyage="901W"))
    assert len(fake.rows(BOOKING)) == 2


def test_freight_invoices_of_a_shipment_with_several_factory_invoices_are_noted_not_disputed():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(sentury_ci(bl_number="27302472"))
    booker.take(sentury_ci("F26AR06MO344", containers=("TGHU1234567",), source="ci2.pdf", bl_number="27302472"))
    for n, cost in (("1", 5500.0), ("2", 11000.0)):
        booker.take(doc("freight_invoice", source=f"fi{n}.pdf", booking_number="27302472", freight_cost=cost,
                        currency="USD"))
    _id, booking = only_booking(fake)
    assert booking["freight_cost"] == 5500.0 and not fake.rows(CONFLICT)
    assert any("the booking keeps" in str(p) for p in fake.posts)


def test_a_late_invoice_with_new_containers_joins_its_po_created_booking_by_booking_number():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(sentury_ci(bl_number="27302472"))
    _id, booking = only_booking(fake)
    booking["state"] = "po_created"                                # its only invoice has its PO
    state, _note, booking_id = booker.take(sentury_ci("F26AR06MO344", containers=("TGHU1234567",),
                                                      source="ci2.pdf", bl_number="27302472", vessel="OTHER"))
    assert state == "done" and booking_id == _id and len(fake.rows(BOOKING)) == 1
    assert tags(fake)["TGHU1234567"] == "F26AR06MO344" and not fake.rows(CHANGE)


def test_a_licence_number_read_as_a_house_bill_ties_no_shipments_together():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="bc1.pdf", booking_number="HANGJ7753600", house_bl="IT1841NVO"))
    state, _note, _id = booker.take(doc("mbl_hbl", source="fcr.pdf", master_bl="ONEYHANGF2823400",
                                        house_bl="IT1841NVO", containers=["ONEU5918680"]))
    assert state == "waiting" and not fake.rows(CONFLICT)          # not pulled into HANGJ7753600


def test_tangier_and_tanger_med_are_one_port():
    from app.freight_booking.rules import same_value
    assert same_value("port_of_loading", "TANGIER", "MAPTM TANGER MED")
    assert same_value("port_of_discharge", "USPEF PORT", "PORT EVERGLADES, FL")
    assert same_value("port_of_discharge", "HONOLULU, HI,UNITED STATES OF AMERICA", "HONOLULU, HI,U.S.A")
    assert not same_value("port_of_discharge", "MOBILE, AL", "SAVANNAH, GA")
