"""Ready for PO that was not (check of the 28 in 1115, 17 Nov 2026): an ETA at the first stop, an arrival notice
moving the sailing, a carrier's confirmation sent by the forwarder, a team PO missed or found too late, and a
forwarder the Booking Report names differently."""

from app.freight_booking.booking_fill import forwarder_check, learn_forwarders
from app.freight_booking.rules import BOOKING, CONFLICT, CONTAINER, link_team_pos, resolve_mapping

from fakes import doc, make_booker, mapping, odoo


def booking_of(fake):
    (row,) = fake.rows(BOOKING).values()
    return row


# ── dates ──────────────────────────────────────────────────────────────

def test_an_eta_beside_a_foreign_port_of_discharge_is_the_first_stop_not_the_arrival():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="HL-37643170 BC.pdf", booking_number="37643170",
                    port_of_loading="LAEM CHABANG", port_of_discharge="YANTIAN", etd="2026-10-22", eta="2026-10-29",
                    received="2026-10-14 05:12:00"))
    assert booking_of(fake)["etd"] == "2026-10-22" and not booking_of(fake).get("eta")
    booker.take(doc("commercial_invoice", source="afterload-37643170.pdf", booking_number="37643170",
                    port_of_discharge="ST. PAUL, MN", etd="2026-10-22", eta="2026-12-08", invoices=["GRKA-P-2026455"],
                    received="2026-10-18 03:49:00"))
    assert booking_of(fake)["eta"] == "2026-12-08"


def test_an_arrival_notice_never_moves_the_sailing_date():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="bc.pdf", booking_number="28052913", port_of_loading="TANGER MED",
                    port_of_discharge="NEW YORK, NY", etd="2026-11-05", received="2026-11-01 08:00:00"))
    booker.take(doc("shipping_notice", source="ANSE0301.pdf", booking_number="28052913", port_of_discharge="NEW YORK, NY",
                    etd="2026-11-13", eta="2026-11-16", received="2026-11-13 21:14:00"))
    row = booking_of(fake)
    assert row["etd"] == "2026-11-05" and row["eta"] == "2026-11-16" and not fake.rows(CONFLICT)


def test_an_eta_too_soon_after_sailing_is_dropped_when_the_sailing_moves_past_it():
    fake = odoo()
    booker = make_booker(fake)                                       # each confirmation is the newest
    booker.take(doc("booking_confirmation", source="update 7.pdf", booking_number="14546118", port_of_loading="TANGER MED",
                    port_of_discharge="USDAL DALLAS, TX", etd="2026-09-03", eta="2026-09-21", received="2026-09-01 08:00:00"))
    assert booking_of(fake)["eta"] == "2026-09-21"
    booker.take(doc("booking_confirmation", source="update 8.pdf", booking_number="14546118", etd="2026-10-02",
                    received="2026-10-01 08:00:00"))
    row = booking_of(fake)
    assert row["etd"] == "2026-10-02" and not row["eta"]
    assert any("ETA 2026-09-21 dropped" in post[2].get("body", "") for post in fake.posts)


def test_no_crossing_from_asia_takes_a_week_but_tanger_med_can():
    fake = odoo()
    make_booker(fake).take(doc("booking_confirmation", source="bc.pdf", booking_number="39936435", port_of_loading="HAIPHONG",
                               port_of_discharge="LOS ANGELES, CA", etd="2026-11-15", eta="2026-11-22"))
    assert not booking_of(fake).get("eta")
    fake = odoo()
    make_booker(fake).take(doc("booking_confirmation", source="bc.pdf", booking_number="12587155", port_of_loading="TANGER MED",
                               port_of_discharge="NEW YORK, NY", etd="2026-10-30", eta="2026-11-08"))
    assert booking_of(fake)["eta"] == "2026-11-08"


# ── forwarder ──────────────────────────────────────────────────────────

DE_WELL = mapping(document_name="DE WELL", sender_domain="dewell.com", partner_id=7,
                  partner_name="De Well Container Shipping, Inc.")
HAPAG = mapping(document_name="HAPAG-LLOYD", partner_id=8, partner_name="Hapag-Lloyd (America) LLC")


def test_a_sender_domain_covers_its_subdomains():
    assert resolve_mapping([DE_WELL], "forwarder", sender_domain="qin.dewell.com") is DE_WELL
    assert resolve_mapping([DE_WELL], "forwarder", sender_domain="notdewell.com") is None


def test_a_carriers_confirmation_sent_by_the_forwarder_names_the_forwarder():
    fake = odoo()
    make_booker(fake, [DE_WELL, HAPAG]).take(doc("booking_confirmation", sender="qin_op3@qin.dewell.com",
                                                 source="HL-37643170 BC 15TH UPDATE.pdf", booking_number="37643170",
                                                 forwarder_name="HAPAG-LLOYD (THAILAND) LTD."))
    row = booking_of(fake)
    assert row["forwarder_id"] == 7 and row["forwarder_name"] == "De Well Container Shipping, Inc."
    fake = odoo()                                                      # Hapag's own confirmation: Hapag
    make_booker(fake, [DE_WELL, HAPAG]).take(doc("booking_confirmation", sender="morocco@service.hlag.com",
                                                 booking_number="14546118", forwarder_name="HAPAG-LLOYD MOROCCO SAS"))
    assert booking_of(fake)["forwarder_id"] == 8


# ── the team's freight PO ──────────────────────────────────────────────

def freight_po(fake, name, partner_id, qty=1, **values):
    row = dict(name=name, partner_id=partner_id, state="done", company_id=1, invoice_ids=[], picking_type_id=False,
               partner_ref=False, master_bl=False, house_bl=False, container_number=False, date_order="2026-11-11 09:35:07")
    po = fake.add("purchase.order", **dict(row, **values))
    fake.add("purchase.order.line", order_id=po, product_id=9, product_qty=qty)
    fake.rows("product.product")[9] = {"is_storable": False}
    return po


QUARTZ = mapping(document_name="QUARTZ LOGISTICS", partner_id=5, freight_product_id=9, partner_name="Quartz Logistics (NY) Inc.")
APEX = mapping(document_name="K-APEX", sender_domain="kln.com", partner_id=6, freight_product_id=9,
               partner_name="Apex Maritime Co.,Inc.")


def test_a_bill_with_the_carrier_prefix_finds_the_teams_po():
    fake = odoo()
    po = freight_po(fake, "P70039", 5, qty=2, master_bl="WHLC035GX69537", partner_ref="WHSU5147916, WHSU6517471")
    make_booker(fake, [QUARTZ]).take(doc("booking_confirmation", source="035GX69537.pdf", booking_number="035GX69537",
                                         forwarder_name="QUARTZ LOGISTICS (NY) INC.", master_bl="035GX69537",
                                         containers=["WHSU5147916", "WHSU6517471"]))
    assert booking_of(fake)["purchase_order_id"] == po


def test_the_po_named_after_the_booking_is_its_po_even_under_another_forwarder():
    fake = odoo()
    po = freight_po(fake, "P78483", 5, partner_ref="BKKM68473200", master_bl="HDMUBKKM68473200",
                    container_number="CAIU7659943")
    make_booker(fake, [QUARTZ, APEX]).take(doc("booking_confirmation", sender="pubate.sukpat@kln.com",
                                               booking_number="BKKM68473200", forwarder_name="K-APEX (THAILAND) CO., LTD.",
                                               master_bl="HDMUBKKM68473200", containers=["CAIU7659943"]))
    row = booking_of(fake)
    assert row["purchase_order_id"] == po and row["forwarder_id"] == 5          # the PO's vendor is the payable one
    assert any("P78483" in post[2].get("body", "") and "Apex" in post[2].get("body", "") for post in fake.posts)


def test_a_po_raised_for_two_bookings_is_found_by_its_containers():
    fake = odoo()
    po = freight_po(fake, "P78536", 7, qty=2, partner_ref="22492448", master_bl="HLCUBKK260981791",
                    container_number="FANU1257999, HAMU3120350")
    booking = fake.add(BOOKING, company_id=1, booking_number="37643170", master_bl="HLCUBKK260981780", forwarder_id=7,
                       etd="2026-10-22", state="ready_for_po", purchase_order_id=False)
    fake.add(CONTAINER, booking_id=booking, container_no="HAMU3120350", state="active")
    link_team_pos(fake, [mapping(document_name="DE WELL", partner_id=7, freight_product_id=9)])
    assert booking_of(fake)["purchase_order_id"] == po


def test_an_old_po_holding_a_reused_container_is_not_this_shipments():
    fake = odoo()
    freight_po(fake, "P60001", 7, container_number="HAMU3120350", date_order="2026-05-02 00:00:00")
    fake.add(BOOKING, company_id=1, booking_number="37643170", forwarder_id=7, etd="2026-10-22", state="ready_for_po",
             purchase_order_id=False)
    fake.add(CONTAINER, booking_id=next(iter(fake.rows(BOOKING))), container_no="HAMU3120350", state="active")
    link_team_pos(fake, [mapping(document_name="DE WELL", partner_id=7, freight_product_id=9)])
    assert not booking_of(fake).get("purchase_order_id")


def test_a_po_the_team_uploads_later_is_linked_on_the_hourly_pass():
    fake = odoo()
    booker = make_booker(fake, [QUARTZ])
    booker.take(doc("booking_confirmation", booking_number="BKKM81888800", forwarder_name="QUARTZ LOGISTICS",
                    master_bl="HDMUBKKM81888800"))
    assert not booking_of(fake).get("purchase_order_id")
    po = freight_po(fake, "P79600", 5, partner_ref="BKKM81888800")
    link_team_pos(fake, [QUARTZ])
    assert booking_of(fake)["purchase_order_id"] == po and booking_of(fake)["purchase_order_matched"]


# ── the Booking Report's forwarder ─────────────────────────────────────

ROWS = [{"tab": "26OCT", "row": 240, "numbers": ["BKKM68473200"], "forwarder": "QUARTZ"},
        {"tab": "26OCT", "row": 241, "numbers": ["BKKGAC940700"], "forwarder": "QUARTZ"},
        {"tab": "26OCT", "row": 243, "numbers": ["BKKGAC936500"], "forwarder": "QUARTZ"},
        {"tab": "26OCT", "row": 242, "numbers": ["EBKG19094732"], "forwarder": "APEX"},
        {"tab": "26OCT", "row": 244, "numbers": ["EBKG19094735"], "forwarder": "APEX"},
        {"tab": "26OCT", "row": 245, "numbers": ["37691856"], "forwarder": "APEX / DEWELL"},
        {"tab": "26OCT", "row": 246, "numbers": ["40700393"], "forwarder": "APEX / DEWELL"},
        {"tab": "26OCT", "row": 247, "numbers": ["99999999"], "forwarder": "WWL"}]
ORDERS = {"BKKGAC940700": [{"partner_id": [5, "Quartz Logistics (NY) Inc."]}],
          "BKKGAC936500": [{"partner_id": [5, "Quartz Logistics (NY) Inc."]}],
          "EBKG19094732": [{"partner_id": [6, "Apex Maritime Co.,Inc."]}],
          "EBKG19094735": [{"partner_id": [6, "Apex Maritime Co.,Inc."]}],
          "37691856": [{"partner_id": [7, "De Well Container Shipping, Inc."]}],
          "40700393": [{"partner_id": [7, "De Well Container Shipping, Inc."]}],
          "99999999": [{"partner_id": [3, "Qingdao Sun Star International Logistics Co.,Ltd."]}]}


def test_the_report_forwarder_names_are_learned_from_the_teams_pos():
    # Two forwarders in one cell, or a name seen on one PO only, teach nothing.
    assert learn_forwarders(ROWS, ORDERS) == {"QUARTZ": (5, "Quartz Logistics (NY) Inc."), "APEX": (6, "Apex Maritime Co.,Inc.")}


def test_the_booking_reports_forwarder_is_the_one_paid():
    """Sir Nic, 18 Nov 2026: Booking PO, booking number, forwarder and rate go from the Booking Report to the Import
    Logsheet, from which the PO is made; the logsheet's forwarder is the report's on 599 of 603 bookings."""
    learned = learn_forwarders(ROWS, ORDERS)
    booking = {"forwarder_id": [6, "Apex Maritime Co.,Inc."], "forwarder_name": "K-APEX (THAILAND) CO., LTD."}
    assert forwarder_check(booking, ROWS[0], learned) == ("fill", 5, "Quartz Logistics (NY) Inc.")
    assert forwarder_check({"forwarder_id": False, "forwarder_name": False}, ROWS[0], learned) == ("fill", 5, "Quartz Logistics (NY) Inc.")
    assert forwarder_check({"forwarder_id": [5, "Quartz Logistics (NY) Inc."], "forwarder_name": "TIANSHI"}, ROWS[0], learned) is None


def test_a_paper_naming_another_forwarder_is_a_note_not_a_conflict():
    fake = odoo()
    fake.add(BOOKING, company_id=1, booking_number="BKKM68473200", forwarder_id=5, forwarder_name="Quartz Logistics (NY) Inc.")
    make_booker(fake, [QUARTZ, APEX]).take(doc("mbl_hbl", source="A2611310404-ISF Information.pdf", sender="amporn.chantree@kln.com",
                                               booking_number="BKKM68473200", forwarder_name="K-APEX (THAILAND) CO., LTD."))
    assert booking_of(fake)["forwarder_id"] == 5 and not fake.rows(CONFLICT)
    assert any("K-APEX" in post[2].get("body", "") for post in fake.posts)


def test_the_booking_po_is_read_from_the_reports_po_column():
    import io
    from openpyxl import Workbook, load_workbook
    from app.freight_booking.booking_fill import read_details
    wb = Workbook()
    for title, column in (("26SEPT", "P.O"), ("26OCT", "PO#")):
        ws = wb.create_sheet(title)
        ws.append([column, "FORWARDER", "FREIGHT", "BOOKING CONFIRMATION", "FD"])
        ws.append([f"CC11280DALLAS1-{title[2:]}-32", "HAPAG", 3420, "14546118" if title == "26SEPT" else "28052913", "DALLAS"])
    out = io.BytesIO()
    wb.save(out)
    rows = read_details(load_workbook(io.BytesIO(out.getvalue()), read_only=True))
    assert [r["booking_po"] for r in rows] == ["CC11280DALLAS1-SEPT-32", "CC11280DALLAS1-OCT-32"]


def test_the_hourly_pass_looks_for_team_pos_even_without_a_booking_report(db, monkeypatch):
    from app.freight_booking import hourly
    from fakes import connection
    connection(db)
    calls = []
    monkeypatch.setattr(hourly.Odoo, "from_connection", lambda _db, _connection: "odoo")
    monkeypatch.setattr("app.freight_booking.rules.link_team_pos", lambda odoo, mappings: calls.append(odoo) or 0)
    hourly.fill_bookings(db)
    assert calls == ["odoo"]
    hourly.fill_bookings(db)                                          # once an hour
    assert calls == ["odoo"]


def test_the_worker_runs_each_jobs_own_task_after_its_pass(db, monkeypatch):
    """The shared worker never names a job: Freight Booking's hourly fill comes from its Job (app/freight_booking)."""
    from app.shared import worker
    ran = []
    monkeypatch.setattr("app.freight_booking.hourly.fill_bookings", lambda db: ran.append("freight"))
    monkeypatch.setattr(worker, "read_mailboxes", lambda *a, **k: None)
    monkeypatch.setattr(worker, "process_files", lambda *a, **k: None)
    worker.run_once(db, purposes=("bill_matching",))
    assert ran == []
    worker.run_once(db, purposes=("freight_booking",))
    assert ran == ["freight"]


def test_the_reports_forwarder_conflict_is_settled_once_the_booking_agrees():
    from app.freight_booking.booking_fill import settle_report_conflicts
    fake = odoo()
    booking = fake.add(BOOKING, company_id=1, booking_number="37643170")
    fake.add(CONFLICT, booking_id=booking, field_name="forwarder_name", new_value="De Well Container Shipping, Inc.",
             source="Booking Report 26OCT row 58 FORWARDER DEWELL")
    fake.add(CONFLICT, booking_id=booking, field_name="etd", new_value="2026-10-22", source="bc.pdf")
    assert settle_report_conflicts(fake, booking) == 1
    assert sorted(c["state"] for c in fake.rows(CONFLICT).values()) == ["open", "resolved"]


def test_a_file_whose_records_were_cleared_points_at_no_booking(db, tmp_path):
    """A clean start deletes the bookings and the file's records: the Bookings page must not list them as 'None'."""
    from app.shared.core import store
    from app.freight_booking.compare import booking_ids
    from fakes import email
    message_id = email(db, tmp_path, state="done")
    with db:
        db.execute("UPDATE file SET odoo_id = 674 WHERE message_id = ?", (message_id,))
        db.execute("DELETE FROM message_target WHERE message_id = ?", (message_id,))
        store.refresh_messages(db, [message_id])
    assert booking_ids(db) == set()


def test_another_databases_booking_is_not_listed_against_this_one(db, tmp_path):
    """A file read in the 1111 test run holds 1111's booking id: the page reading 1115 must not list it."""
    from app.shared.core import store
    from app.freight_booking.compare import booking_ids
    from fakes import connection, email
    message_id = email(db, tmp_path)                                   # not yet processed in the connection that is on
    connection(db)
    old = connection(db, name="Old test", database="1111", active=0)
    file_id = db.execute("SELECT id FROM file WHERE message_id = ?", (message_id,)).fetchone()[0]
    with db:
        db.execute("INSERT INTO message_target (message_id, connection_id, state) VALUES (?, ?, 'done')", (message_id, old))
        db.execute("INSERT INTO file_target (file_id, connection_id, state, odoo_id) VALUES (?, ?, 'done', 1861)", (file_id, old))
        store.refresh_messages(db, [message_id])
    assert db.execute("SELECT odoo_id FROM file WHERE id = ?", (file_id,)).fetchone()[0] is None
    assert booking_ids(db) == set()


def test_only_the_ocean_freight_po_teaches_the_reports_forwarder():
    """A booking's customs PO (Triumph Link) is a freight PO too: MENTFIELD was learned as Triumph Link (17 Nov)."""
    rows = [{"tab": "26OCT", "row": 189, "numbers": ["SGN3495889"], "forwarder": "MENTFIELD"}]
    rows = rows + [{"tab": "26OCT", "row": 190, "numbers": ["SGN3495861"], "forwarder": "MENTFIELD"}]
    orders = {n: [{"id": 1, "partner_id": [9, "Triumph Link Inc."]}, {"id": 2, "partner_id": [4, "Mentfield QV Logistics VN Co., Ltd"]}]
              for n in ("SGN3495889", "SGN3495861")}
    assert learn_forwarders(rows, orders, ocean_ids={2}) == {"MENTFIELD": (4, "Mentfield QV Logistics VN Co., Ltd")}


def test_a_papers_invoice_written_without_dashes_is_the_reports_invoice():
    """EBKG19094541: the fill added GRKA-P-2026497, the load plan then GRKAP2026497 as a second invoice line."""
    from app.freight_booking.rules import INVOICE
    fake = odoo()
    booking = fake.add(BOOKING, company_id=1, booking_number="EBKG19094541")
    fake.add(INVOICE, booking_id=booking, invoice_number="GRKA-P-2026497", source="Booking Report 26OCT row 34")
    make_booker(fake).take(doc("commercial_invoice", source="afterload-EBKG19094541.pdf", booking_number="EBKG19094541",
                               invoices=["GRKAP2026497"], containers=["MSCU1234566"]))
    lines = list(fake.rows(INVOICE).values())
    assert [line["invoice_number"] for line in lines] == ["GRKA-P-2026497"]
    (container,) = fake.rows(CONTAINER).values()
    assert container["invoice_id"] == next(iter(fake.rows(INVOICE)))


def test_an_impossible_eta_never_replaces_a_good_one():
    """97851501: Hapag's update gave 27 Oct to 29 Oct (the feeder); the held 8 Nov was replaced, then dropped."""
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="BC ORIGINAL.pdf", booking_number="97851501", port_of_loading="TANGER MED",
                    port_of_discharge="NEW YORK, NY", etd="2026-10-30", eta="2026-11-08", received="2026-10-20 08:00:00"))
    booker.take(doc("booking_confirmation", source="Booking Update 1.pdf", booking_number="97851501",
                    port_of_loading="MAPTM TANGER MED", port_of_discharge="USNYC NEW YORK, NY", etd="2026-10-27",
                    eta="2026-10-29", received="2026-10-22 08:00:00"))
    row = booking_of(fake)
    assert row["etd"] == "2026-10-27" and row["eta"] == "2026-11-08"


def test_a_booking_linked_to_the_teams_po_keeps_the_pos_vendor():
    """31652425: linked to the team's Hapag PO, then a trucker's invoice (Canary Yellow) made Mentfield the forwarder."""
    fake = odoo()
    po = freight_po(fake, "P70100", 8)
    fake.add(BOOKING, company_id=1, booking_number="31652425", master_bl="HLCUCA6260902617", forwarder_id=8,
             forwarder_name="HAPAG-LLOYD MOROCCO SAS", purchase_order_id=po, purchase_order_matched=True)
    make_booker(fake, [HAPAG, mapping(document_name="CANARY YELLOW", partner_id=4, partner_name="Mentfield QV Logistics VN Co., Ltd")]).take(
        doc("freight_invoice", source="Invoice #26169 - Canary Yellow Logistics.pdf", sender="anika@cylltd.com",
            forwarder_name="Canary Yellow Logistics, Ltd.", master_bl="HLCUCA6260902617", containers=["BEAU4987728"]))
    row = booking_of(fake)
    assert row["forwarder_id"] == 8 and row["forwarder_name"] == "HAPAG-LLOYD MOROCCO SAS" and not fake.rows(CONFLICT)


# ── B/L numbers as the team writes them (scorecard against 209 team POs, 17 Nov 2026) ──

def test_klns_house_bill_is_stored_without_its_code():
    fake = odoo()
    make_booker(fake).take(doc("booking_confirmation", booking_number="EBKG19094732", master_bl="MEDUXL831740",
                               house_bl="KFUNA2611310452"))
    assert booking_of(fake)["house_bl"] == "A2611310452"


def test_a_hapag_bill_never_ends_in_o_or_s():
    """7,288 Hapag B/Ls on the team's POs: none ends in O or S; the AI read 0 as O and 5 as S on 5 bookings."""
    fake = odoo()
    make_booker(fake).take(doc("booking_confirmation", booking_number="40672235", master_bl="HLCUBKK2609AZEKO"))
    assert booking_of(fake)["master_bl"] == "HLCUBKK2609AZEK0"
    fake = odoo()
    make_booker(fake).take(doc("booking_confirmation", booking_number="90624224", master_bl="HLCUBKK2609ATNKS"))
    assert booking_of(fake)["master_bl"] == "HLCUBKK2609ATNK5"


def test_de_wells_house_bill_is_taken_from_its_file_name():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="HL-94822694 BC.pdf", booking_number="94822694",
                    master_bl="HLCUBKK2609BDDQ8"))
    assert booking_of(fake)["house_bl"] == "HLCUBKK2609BDDQ8"                     # assumed: the MBL repeated
    booker.take(doc("mbl_hbl", source="DeWell HBL DRAFT - HBL#SBKKS0027412 - LS-KATRACVC -  - MBL#HLCUBKK2609BDDQ8 - "
                    "22 Oct 2026.PDF", master_bl="HLCUBKK2609BDDQ8"))
    assert booking_of(fake)["house_bl"] == "SBKKS0027412"


def test_tianshis_cargo_receipt_names_its_booking_and_house_bill():
    fake = odoo()
    booker = make_booker(fake)
    booker.take(doc("booking_confirmation", source="Booking Confirmation_13204963.pdf", booking_number="13204963"))
    state, _note, _id = booker.take(doc("mbl_hbl", source="DRAFT-13204963-THSE26100423_TS FCR (DFCR).PDF",
                                        port_of_loading="LAEM CHABANG"))
    assert state == "done" and booking_of(fake)["house_bl"] == "THSE26100423"


def test_the_booking_number_alone_is_not_the_master_bill():
    """Only 35 of 15,763 team POs carry the bare booking number as the MBL (13204963 had it, the PO HLCUBKK2609ARTE7)."""
    fake = odoo()
    make_booker(fake).take(doc("booking_confirmation", booking_number="13204963", master_bl="13204963"))
    assert not booking_of(fake).get("master_bl")


def test_the_carriers_own_invoice_gives_an_eta_none_held_but_a_truckers_does_not():
    fake = odoo()
    booker = make_booker(fake, [HAPAG, mapping(document_name="CANARY YELLOW", partner_id=4)])
    booker.take(doc("booking_confirmation", sender="morocco@service.hlag.com", booking_number="28052913",
                    forwarder_name="HAPAG-LLOYD MOROCCO SAS", master_bl="HLCUCA6261001927", port_of_loading="TANGER MED",
                    etd="2026-11-05"))
    booker.take(doc("freight_invoice", source="Invoice #26170 - Canary Yellow.pdf", sender="anika@cylltd.com",
                    forwarder_name="Canary Yellow Logistics, Ltd.", master_bl="HLCUCA6261001927", eta="2026-11-28"))
    assert not booking_of(fake).get("eta")
    booker.take(doc("freight_invoice", source="INVP0201_663312826.pdf", sender="cnamuncale@wtdus.com",
                    forwarder_name="Hapag-Lloyd (America) LLC.", master_bl="HLCUCA6261001927",
                    port_of_discharge="NEW YORK, NY", eta="2026-11-15"))
    assert booking_of(fake)["eta"] == "2026-11-15"
