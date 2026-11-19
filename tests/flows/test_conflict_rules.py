"""Conflicts that were no disagreement (first test run in 1111, Nov 2026): a terminal against its port, one
date with day and month swapped, an ETA before the ETD, a misspelt rail ramp, a container number as the B/L."""

from app.freight_booking.rules import BOOKING, CONFLICT

from fakes import doc, make_booker, odoo


def booking_of(fake):
    (row,) = fake.rows(BOOKING).values()
    return row


def test_a_terminal_never_disputes_its_port_and_the_port_replaces_a_terminal():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="bc.pdf", booking_number="94427409", port_of_discharge="LOS ANGELES, CA"))
    booker.take(doc("booking_confirmation", source="bc2.pdf", booking_number="94427409",
                    port_of_discharge="APM TERMINALS PIER 400"))
    assert booking_of(fake)["port_of_discharge"] == "LOS ANGELES, CA" and not fake.rows(CONFLICT)

    fake = odoo()
    fake.add(BOOKING, company_id=1, booking_number="94427409", port_of_discharge="APM TERMINALS PIER 400")
    make_booker(fake).take(doc("mbl_hbl", source="bl.pdf", booking_number="94427409", port_of_discharge="LOS ANGELES, CA"))
    assert booking_of(fake)["port_of_discharge"] == "LOS ANGELES, CA" and not fake.rows(CONFLICT)


def test_a_date_with_day_and_month_swapped_is_the_same_date():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="bc.pdf", booking_number="BKK6A5027200", etd="2026-10-08",
                    received="2026-10-05 03:38:11"))
    booker.take(doc("mbl_hbl", source="dfcr.pdf", booking_number="BKK6A5027200", etd="2026-08-10",
                    received="2026-10-05 03:38:11"))
    assert booking_of(fake)["etd"] == "2026-10-08" and not fake.rows(CONFLICT)


def test_an_eta_before_the_etd_is_not_an_eta():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="bc.pdf", booking_number="SGN3495861", received="2026-10-01 08:00:00"))
    booker.take(doc("shipping_notice", source="notice.pdf", booking_number="SGN3495861", etd="2026-10-11",
                    eta="2026-10-08", received="2026-10-02 08:46:21"))
    row = booking_of(fake)
    assert row["etd"] == "2026-10-11" and not row.get("eta")


def test_a_misspelt_rail_ramp_held_as_port_gives_way_to_the_seaport():
    fake = odoo()
    fake.add(BOOKING, company_id=1, booking_number="HANGG3403700", port_of_discharge="ATLTANTA")
    make_booker(fake).take(doc("mbl_hbl", source="bl.pdf", booking_number="HANGG3403700", port_of_discharge="SAVANNAH, GA"))
    row = booking_of(fake)
    assert row["port_of_discharge"] == "SAVANNAH, GA" and row["rail_ramp"] == "ATLTANTA" and not fake.rows(CONFLICT)


def test_a_container_number_is_never_the_bill_of_lading():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="bc.pdf", booking_number="BKKGAL261300", master_bl="ONEYBKKGAL261300"))
    booker.take(doc("mbl_hbl", source="dfcr.pdf", booking_number="BKKGAL261300", master_bl="ONEU0312535"))
    assert booking_of(fake)["master_bl"] == "ONEYBKKGAL261300" and not fake.rows(CONFLICT)


def test_saint_paul_is_a_rail_ramp():
    from app.freight_booking.rules import is_inland_point
    assert is_inland_point("SAINT PAUL, MN") and is_inland_point("ST. PAUL") and not is_inland_point("TACOMA, UNITED STATES")


def test_bookings_in_a_row_are_not_one_booking():
    from app.freight_booking.rules import same_identifier
    assert not same_identifier("EBKG18500445", "EBKG18500447")
    assert not same_identifier("HLCUCA6260901520", "HLCUCA6260901530")
    assert same_identifier("HLCUSGN2610CISM0", "HLCUSGN2610CISMO")          # 0 read as O is still one number


def test_the_importer_is_never_the_factory_or_forwarder():
    fake = odoo()
    booker = make_booker(fake)
    fake.add(BOOKING, company_id=1, booking_number="QGD3625545", factory_name="STERLING CORP.,LTD")
    booker.take(doc("mbl_hbl", source="bl.pdf", booking_number="QGD3625545", factory_name="KATANA RACING INC.",
                    forwarder_name="WTD US"))
    row = booking_of(fake)
    assert row["factory_name"] == "STERLING CORP.,LTD" and not row.get("forwarder_name") and not fake.rows(CONFLICT)


def test_the_customs_broker_is_never_the_forwarder():
    fake = odoo()
    booker = make_booker(fake)
    fake.add(BOOKING, company_id=1, booking_number="93931416", forwarder_name="HAPAG-LLOYD MOROCCO SAS")
    booker.take(doc("freight_invoice", source="do.pdf", booking_number="93931416", forwarder_name="Triumph Link (USA), Inc."))
    assert booking_of(fake)["forwarder_name"] == "HAPAG-LLOYD MOROCCO SAS" and not fake.rows(CONFLICT)


def test_a_container_number_needs_its_check_digit():
    from app.shared.documents.text import valid_container_number
    assert valid_container_number("TCLU8929654") and valid_container_number("VVUU9007780")
    assert not valid_container_number("HLXU1234567")                      # a template number on a Hapag form
    fake = odoo()
    make_booker(fake).take(doc("booking_confirmation", source="bc.pdf", booking_number="31297191",
                               containers=["HAMU4530301", "HLXU1234567"]))
    assert [c["container_no"] for c in fake.rows("b2b.ocr.booking.container").values()] == ["HAMU4530301"]


def test_a_reading_by_other_instructions_is_read_again(tmp_path, monkeypatch):
    from app.shared.core import store
    from app.shared.documents import extract
    from app.shared import pipeline
    db = store.connect(tmp_path / "t.sqlite3")
    settings = {k: store.get_setting(db, k) for k in pipeline.SETTING_KEYS}
    settings["ai_parallel"] = "4"
    mb = db.execute("INSERT INTO mailbox (name, purpose, company_id) VALUES ('m', 'freight_booking', 1)").lastrowid
    msg = db.execute("INSERT INTO message (mailbox_id, dedupe_key, subject, received) VALUES (?, 'k', 's', '2026-10-01')", (mb,)).lastrowid
    path = tmp_path / "a.pdf"; path.write_bytes(b"x")
    current = extract.reading_version("freight_booking", settings)
    for name, read_with in (("old.pdf", "0000000000000000"), ("new.pdf", current)):
        db.execute("INSERT INTO file (message_id, filename, sha256, path, doc_class, extracted, read_with) VALUES (?, ?, ?, ?, 'commercial_invoice', '{}', ?)",
                   (msg, name, name, str(path), read_with))
    read = []
    monkeypatch.setattr(extract, "read_file", lambda raw, purpose, s: (read.append(1), ("certificate_of_origin", {}))[1])
    pipeline.read_ahead(db, [{"id": msg, "purpose": "freight_booking"}], settings)
    rows = {r["filename"]: r for r in db.execute("SELECT filename, doc_class, read_with FROM file")}
    assert len(read) == 1 and rows["old.pdf"]["doc_class"] == "certificate_of_origin" and rows["old.pdf"]["read_with"] == current
    assert rows["new.pdf"]["doc_class"] == "commercial_invoice"


def test_a_dropped_connection_to_openai_is_tried_again(monkeypatch):
    import requests
    from app.shared.documents import ai
    calls = []

    class Ok:
        status_code = 200
        text = ""
        def json(self):
            return {"choices": [{"message": {"content": '{"doc_class": "mbl_hbl"}'}}]}

    def post(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise requests.ConnectionError("Name resolution failed")
        if len(calls) == 2:
            r = Ok(); r.status_code = 429; return r
        return Ok()

    monkeypatch.setattr(ai.requests, "post", post)
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    assert ai.extract("k", "m", "p", "t", {}) == {"doc_class": "mbl_hbl"} and len(calls) == 3


def test_an_answer_cut_off_is_asked_again_on_the_first_pages(monkeypatch):
    from app.shared.documents import ai, extract
    asked = []

    def fake(key, model, prompt, text, schema):
        asked.append(len(text))
        if len(asked) == 1:
            raise ai.AnswerTooLong("cut off")
        return {"doc_class": "statement"}

    monkeypatch.setattr(ai, "extract", fake)
    monkeypatch.setattr(extract, "file_text", lambda raw, pages: "STATEMENT OF ACCOUNT " * 3000)
    doc_class, _values = extract.read_file(b"%PDF", "freight_booking", {"max_pages": "50", "max_chars": "60000",
                                                                       "openai_api_key": "k", "openai_model": "m",
                                                                       "freight_prompt": "p"})
    assert doc_class == "statement" and asked == [60000, extract.SHORT_READ_CHARS]


def test_a_transshipment_hub_held_as_port_of_discharge_gives_way_to_the_us_port():
    fake = odoo()
    fake.add(BOOKING, company_id=1, booking_number="90624224", port_of_discharge="SINGAPORE")
    make_booker(fake).take(doc("mbl_hbl", source="hbl.pdf", booking_number="90624224", port_of_discharge="SAVANNAH, GA, USA"))
    assert booking_of(fake)["port_of_discharge"] == "SAVANNAH, GA, USA" and not fake.rows(CONFLICT)
    make_booker(fake).take(doc("booking_confirmation", source="bc.pdf", booking_number="90624224", port_of_discharge="LIVORNO"))
    assert booking_of(fake)["port_of_discharge"] == "SAVANNAH, GA, USA" and not fake.rows(CONFLICT)


def test_a_bl_number_with_one_look_alike_character_is_the_same_number():
    fake = odoo()
    fake.add(BOOKING, company_id=1, booking_number="90624224", master_bl="HLCUBKK2609ATNKS")
    make_booker(fake).take(doc("mbl_hbl", source="hbl.pdf", booking_number="90624224", master_bl="HLCUBKK2609ATNK5"))
    assert booking_of(fake)["master_bl"] == "HLCUBKK2609ATNKS" and not fake.rows(CONFLICT)


def test_an_eta_on_the_day_it_sails_is_not_an_eta():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="bc.pdf", booking_number="EBKG18815176", etd="2026-10-18", received="2026-10-10 08:00:00"))
    booker.take(doc("mbl_hbl", source="h.pdf", booking_number="EBKG18815176", eta="2026-10-18", received="2026-10-19 08:00:00"))
    assert not booking_of(fake).get("eta")


def test_freight_amounts_never_open_a_conflict_and_a_customs_broker_sets_none():
    from fakes import mapping
    fake = odoo()
    fake.add("res.currency", name="USD", rounding=0.01)
    booker = make_booker(fake)
    fake.add(BOOKING, company_id=1, booking_number="20325866", freight_cost=1002.0, freight_currency_id=False)
    booker.take(doc("freight_invoice", source="do.pdf", sender="ar@tlius.com", booking_number="20325866", freight_cost=110, currency="USD"))
    assert booking_of(fake)["freight_cost"] == 1002.0 and not fake.rows(CONFLICT)
    booker.take(doc("freight_invoice", source="fi.pdf", booking_number="20325866", freight_cost=250, currency="USD"))
    assert booking_of(fake)["freight_cost"] == 1002.0 and not fake.rows(CONFLICT)


def test_a_staff_handle_on_the_forwarders_own_paper_never_disputes():
    from fakes import mapping
    fake = odoo()
    kerry = mapping(document_name="K-APEX", sender_domain="kln.com", partner_id=113522)
    booker = make_booker(fake, [kerry])
    fake.add(BOOKING, company_id=1, booking_number="BKKM99260800", forwarder_name="BRIGHT #241")
    booker.take(doc("shipping_notice", source="delay.pdf", sender="ops@kln.com", booking_number="BKKM99260800",
                    forwarder_name="K-APEX (THAILAND) CO., LTD."))
    assert booking_of(fake)["forwarder_name"] == "K-APEX (THAILAND) CO., LTD."
    booker.take(doc("shipping_notice", source="delay2.pdf", sender="ops@kln.com", booking_number="BKKM99260800",
                    forwarder_name="MARINE (HKG) LTD."))
    assert booking_of(fake)["forwarder_name"] == "K-APEX (THAILAND) CO., LTD." and not fake.rows(CONFLICT)


def test_a_near_date_read_day_first_never_moves_the_schedule():
    from app.freight_booking.rules import swapped_date
    assert swapped_date("2026-10-07", "2026-08-10")            # 8 October read as 10 August, beside 7 October
    assert not swapped_date("2026-10-01", "2026-10-04")        # a real re-plan
    assert not swapped_date("2026-11-03", "2026-10-18")


def test_waiting_emails_take_turns(tmp_path):
    from app.shared.core import store
    from app.shared import pipeline
    db = store.connect(tmp_path / "t.sqlite3")
    conn = db.execute("INSERT INTO odoo_connection (name, url, database, login, active) VALUES ('t', 'u', 'd', 'x', 1)").lastrowid
    mb = db.execute("INSERT INTO mailbox (name, purpose, company_id) VALUES ('m', 'freight_booking', 1)").lastrowid
    ids = [db.execute("INSERT INTO message (mailbox_id, dedupe_key, subject, received) VALUES (?, ?, 's', ?)",
                      (mb, f"k{i}", f"2026-10-0{i + 1}")).lastrowid for i in range(3)]
    for i, mid in enumerate(ids):  # the oldest email was tried most recently
        pipeline.set_target(db, mid, conn, state="review", processed_at=f"2026-11-17 0{3 - i}:00:00")
    rows = db.execute("SELECT m.id FROM message m JOIN message_target t ON t.message_id = m.id AND t.connection_id = ? "
                      "WHERE t.state = 'review' ORDER BY COALESCE(t.processed_at, ''), m.received, m.id", (conn,)).fetchall()
    assert [r[0] for r in rows] == list(reversed(ids))


def test_port_codes_and_port_areas_never_dispute():
    fake = odoo()
    fake.add(BOOKING, company_id=1, booking_number="31978034", port_of_loading="LAEM CHABANG ,THAILAND",
             port_of_discharge="LOS ANGELES, CA, USA")
    make_booker(fake).take(doc("shipping_notice", source="an.pdf", booking_number="31978034", port_of_loading="THLCH",
                               port_of_discharge="USDAL"))
    row = booking_of(fake)
    assert row["port_of_discharge"] == "LOS ANGELES, CA, USA" and row["rail_ramp"] == "USDAL" and not fake.rows(CONFLICT)
    make_booker(fake).take(doc("mbl_hbl", source="bl.pdf", booking_number="31978034", port_of_discharge="USLAX"))
    assert not fake.rows(CONFLICT)                                  # the code of the same port


def test_house_bill_rules_from_november_documents():
    fake = odoo()
    fake.add(BOOKING, company_id=1, booking_number="BKKGU8570600", master_bl="ONEYBKKGU8570600", house_bl="ONEYBKKGU8570600")
    booker = make_booker(fake)
    booker.take(doc("shipping_notice", source="ARRIVAL NOTICE - THSE26090458.pdf", booking_number="BKKGU8570600",
                    house_bl="THSE26090458"))
    assert booking_of(fake)["house_bl"] == "THSE26090458" and not fake.rows(CONFLICT)   # only repeated the MBL
    booker.take(doc("mbl_hbl", source="HBL.pdf", booking_number="BKKGU8570600", house_bl="F26AR10THA0693"))
    assert booking_of(fake)["house_bl"] == "THSE26090458" and not fake.rows(CONFLICT)   # an invoice number


def test_a_kerry_house_bill_named_after_its_file_is_not_the_master():
    fake = odoo()
    fake.add(BOOKING, company_id=1, booking_number="BKKM92207500", master_bl="HDMUBKKM92207500")
    make_booker(fake).take(doc("mbl_hbl", source="A2611310055H.pdf", booking_number="BKKM92207500", master_bl="A2611310055"))
    row = booking_of(fake)
    assert row["master_bl"] == "HDMUBKKM92207500" and row["house_bl"] == "A2611310055" and not fake.rows(CONFLICT)
