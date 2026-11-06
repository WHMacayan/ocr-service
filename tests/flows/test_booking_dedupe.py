"""One booking per booking number and company, however the number is written."""

from fakes import doc, FakeOdoo, make_booker



def test_same_booking_number_in_another_spelling_updates_the_one_booking():
    odoo = FakeOdoo()
    booker = make_booker(odoo)

    first = booker.take(doc("booking_confirmation", "bc.pdf", booking_number="BK123456", vessel="EVER GIVEN",
                            etd="2026-10-18"))
    second = booker.take(doc("commercial_invoice", "ci.pdf", booking_number=" bk-123456 ",
                             containers=["MSCU1234567"], invoices=["INV-77"]))
    third = booker.take(doc("booking_confirmation", "bc2.pdf", booking_number="bk 123456", eta="2026-11-20"))

    bookings = odoo.rows("b2b.ocr.booking")
    assert len(bookings) == 1
    [(booking_id, row)] = bookings.items()
    assert [first[2], second[2], third[2]] == [booking_id] * 3
    assert {first[0], second[0], third[0]} == {"done"}
    assert row["booking_number"] == "BK123456"
    assert row["vessel"] == "EVER GIVEN" and row["etd"] == "2026-10-18" and row["eta"] == "2026-11-20"


def test_same_booking_number_in_another_company_is_a_separate_booking():
    odoo = FakeOdoo()
    make_booker(odoo).take(doc("booking_confirmation", booking_number="BK123456"))
    other = make_booker(odoo)
    other.company_id = 2
    other.take(doc("booking_confirmation", booking_number="BK123456"))

    assert sorted(r["company_id"] for r in odoo.rows("b2b.ocr.booking").values()) == [1, 2]


def test_printed_factory_wins_over_a_shared_sender_domain():
    from test_pipeline_logic import mapping
    odoo = FakeOdoo()
    thailand = mapping(role="factory", sender_domain="senturytire.com", document_name="SENTURY TIRE (THAILAND)",
                       partner_id=113820)
    morocco = mapping(role="factory", document_name="SENTURY TIRE MOROCCO", partner_id=113821)
    booker = make_booker(odoo, [thailand, morocco])
    booker.take(doc("booking_confirmation", booking_number="BK1"))

    booker.take(doc("commercial_invoice", "ci.pdf", sender="cherry@senturytire.com", booking_number="BK1",
                    factory_name="SENTURY TIRE MOROCCO"))
    assert next(iter(odoo.rows("b2b.ocr.booking").values()))["factory_id"] == 113821

    booker.take(doc("booking_confirmation", booking_number="BK2"))
    booker.take(doc("commercial_invoice", "ci2.pdf", sender="vicky@senturytire.com", booking_number="BK2",
                    factory_name="SENTURY TIRE (THAILAND) CO., LTD"))
    rows = {r["booking_number"]: r for r in odoo.rows("b2b.ocr.booking").values()}
    assert rows["BK2"]["factory_id"] == 113820


def test_longest_printed_name_wins():
    from test_pipeline_logic import mapping
    from app.flows.freight import resolve_mapping
    short = mapping(role="factory", document_name="SENTURY TIRE", partner_id=1)
    long = mapping(role="factory", document_name="SENTURY TIRE MOROCCO", partner_id=2)
    assert resolve_mapping([short, long], "factory", name="Sentury Tire Morocco SARL")["partner_id"] == 2
