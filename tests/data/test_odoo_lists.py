"""Bookings and scraped bills as data sets: the fields Odoo offers, what is exported, and how a file is matched, tested and
written to Odoo."""

import pytest

from app.shared.data import Context, dataset, mapped_rows

from data_fakes import DataOdoo, patch_odoo, seed_bookings


@pytest.fixture
def odoo(monkeypatch):
    return patch_odoo(monkeypatch, DataOdoo())


def run(db, key, rows, mapping):
    ds = dataset(key)
    return ds, ds.plan(Context(db), mapped_rows(rows, mapping))


def test_the_fields_offered_are_odoos_own_and_what_odoo_will_not_let_anyone_edit_is_export_only(db, odoo):
    names = {c.name: c for c in dataset("bookings").columns(Context(db))}
    cols = dataset("bookings").columns(Context(db))
    assert cols[0].name == "id" and [c.name for c in cols[1:5]] == ["booking_number", "state", "forwarder_id", "factory_id"]
    assert names["booking_number"].importable and names["etd"].importable and names["forwarder_id"].importable
    assert not names["state"].importable and not names["missing_fields"].importable      # read-only in Odoo
    assert names["container_ids"].kind == "list" and not names["container_ids"].importable
    assert "message_ids" not in names and "message_follower_ids" not in names             # technical
    assert names["forwarder_id"].label == "Forwarder (Payable Vendor)" and names["freight_mode"].options == (("ocean", "Ocean"), ("inland", "Inland"))


def test_the_bookings_are_exported_as_odoo_shows_them(db, odoo):
    seed_bookings(db)
    rows = dataset("bookings").rows(Context(db), ["id", "booking_number", "state", "forwarder_id", "etd", "cost_per_container",
                                                    "no_additional_charges", "freight_mode", "container_ids"], {})
    assert rows == [
        {"id": 42, "booking_number": "BK200", "state": "Ready for PO", "forwarder_id": "Apex Maritime Co.,Inc.", "etd": None,
         "cost_per_container": 0.0, "no_additional_charges": "Yes", "freight_mode": None, "container_ids": None},
        {"id": 41, "booking_number": "BK123", "state": "Incomplete", "forwarder_id": "Quartz Logistics (NY) Inc.",
         "etd": "2026-11-04", "cost_per_container": 4900.0, "no_additional_charges": "No", "freight_mode": "Ocean",
         "container_ids": "MSCU1234566, TGHU7654320"}]


MAPPING = ["id", "booking_number", "forwarder_id", "etd", "vessel", "freight_mode", "no_additional_charges", "state"]


def test_a_file_is_tested_row_by_row_and_nothing_is_written_to_odoo_until_it_is_applied(db, odoo):
    ds, plan = run(db, "bookings", [
        [41, None, None, "2026-12-01", "EVER GIVEN", None, None, "Ready for PO"],      # by ID: two fields change; State is read-only
        [None, "BK200", "Quartz Logistics (NY) Inc.", None, None, None, None, None],    # by booking number: the forwarder changes
        [None, "BK999", "Apex Maritime Co.,Inc.", None, "NEW SHIP", "Inland", "Yes", None],  # no such booking: added
        [None, "BK300", None, None, "CMA SHIP", None, None, None],                     # as it is already
        [41, None, "Nobody Ltd", None, None, None, None, None],                         # no such forwarder
        [41, None, "Apex Shipping Ltd", None, None, None, None, None],                  # two forwarders carry that name
        [999, None, None, None, "X", None, None, None],                                 # no such booking
        [41, None, None, None, None, "Air", None, None],                                # not a freight mode
        [41, None, None, "04/11/2026", None, None, None, None],                         # a date written day first
    ], MAPPING)
    assert [r.action for r in plan.rows] == ["update", "update", "create", "unchanged", "error", "error", "error", "error", "error"]
    assert plan.rows[0].changes == [("ETD", "2026-11-04", "2026-12-01"), ("Vessel", "MSC ANNA", "EVER GIVEN")]
    assert plan.rows[0].values == {"etd": "2026-12-01", "vessel": "EVER GIVEN"}        # State was not offered: read-only
    assert plan.rows[1].record_id == 42 and plan.rows[1].changes == [
        ("Forwarder (Payable Vendor)", "Apex Maritime Co.,Inc.", "Quartz Logistics (NY) Inc.")]
    assert plan.rows[2].values == {"booking_number": "BK999", "forwarder_id": 6, "vessel": "NEW SHIP", "freight_mode": "inland",
                                   "no_additional_charges": True}
    assert plan.rows[4].errors == ['Forwarder (Payable Vendor): no Forwarder (Payable Vendor) is named "Nobody Ltd"']
    assert "several Forwarder (Payable Vendor) match" in plan.rows[5].errors[0]
    assert plan.rows[6].errors == ["No booking has ID 999 in Odoo."]
    assert plan.rows[7].errors == ["Freight Mode: choose one of: Ocean, Inland"]
    assert plan.rows[8].errors == ["ETD: write the date as YYYY-MM-DD"]
    assert odoo.writes == []                                                            # a test never writes


def test_applying_writes_only_what_changed_and_a_row_odoo_refuses_does_not_stop_the_others(db, odoo):
    odoo.refuse = {"BK200": "Constraint: a booking cannot be moved to this forwarder"}
    ds, plan = run(db, "bookings", [
        [41, None, None, "2026-12-01", "EVER GIVEN", None, None, None],
        [None, "BK200", "Quartz Logistics (NY) Inc.", None, None, None, None, None],
        [None, "BK999", "Apex Maritime Co.,Inc.", None, "NEW SHIP", "Inland", "Yes", None],
    ], MAPPING)
    ds.apply(Context(db), plan)
    assert odoo.writes == [("write", "b2b.ocr.booking", [41], {"etd": "2026-12-01", "vessel": "EVER GIVEN"}),
                           ("create", "b2b.ocr.booking", {"booking_number": "BK999", "forwarder_id": 6, "vessel": "NEW SHIP",
                                                          "freight_mode": "inland", "no_additional_charges": True})]
    assert [r.done for r in plan.rows] == [True, False, True]
    assert plan.rows[1].action == "error" and plan.rows[1].errors == ["Constraint: a booking cannot be moved to this forwarder"]
    assert plan.rows[2].record_id > 100 and odoo.rows["b2b.ocr.booking"][plan.rows[2].record_id]["forwarder_id"] == [6, "Apex Maritime Co.,Inc."]
    assert odoo.activity_db is None            # one entry for the import is logged by the page, not one per row


def test_a_blank_cell_leaves_the_field_as_it_is(db, odoo):
    ds, plan = run(db, "bookings", [[41, None, None, None, None, None, None, None]], MAPPING)
    assert plan.rows[0].action == "unchanged" and plan.rows[0].values == {}


def test_a_new_booking_needs_what_odoo_requires_and_has_no_default_for(db, odoo):
    odoo.defaults = {}
    ds, plan = run(db, "bookings", [[None, "BK999", None, None, "NEW SHIP", None, None, None]],
                   MAPPING)
    assert plan.rows[0].action == "error" and plan.rows[0].errors == ["Required: Company"]


def test_a_file_is_refused_whole_when_it_cannot_tell_which_booking_a_row_is_or_is_too_long(db, odoo, monkeypatch):
    ds, plan = run(db, "bookings", [["EVER GIVEN"]], ["vessel"])
    assert plan.rows == [] and "Map the ID column, or Booking Number" in plan.problems[0]
    monkeypatch.setattr(ds, "max_rows", 2)
    ds, plan = run(db, "bookings", [[41, "A"], [42, "B"], [43, "C"]], ["id", "vessel"])
    assert plan.rows == [] and plan.problems == ["Import at most 2 rows at a time: split the file."]


def test_scraped_bills_can_be_updated_by_id_and_never_added(db, odoo):
    ds, plan = run(db, "scraped-bills", [[7, "TIMAC AGRO"], [99, "X"]], ["id", "vendor_name"])
    assert [r.action for r in plan.rows] == ["update", "error"] and plan.rows[1].errors == ["No scraped bill has ID 99 in Odoo."]
    ds, plan = run(db, "scraped-bills", [["INV-9", "X"]], ["name", "vendor_name"])
    assert plan.rows == [] and "Map the ID column" in plan.problems[0]
    assert not {c.name for c in ds.columns(Context(db)) if c.importable} & {"name", "state"}   # read-only in Odoo
    ds.apply(Context(db), run(db, "scraped-bills", [[7, "TIMAC AGRO"]], ["id", "vendor_name"])[1])
    assert odoo.writes == [("write", "b2b.ocr.scraped.bill", [7], {"vendor_name": "TIMAC AGRO"})]


def test_customs_and_payables_lists_are_export_only():
    from app.shared.data import all_datasets
    sets = {d.key: d for d in all_datasets()}
    assert {"emails", "activity", "customs-batches", "customs-entries", "payables-emails"} <= set(sets)
    assert [k for k, d in sets.items() if not d.importable] == ["emails", "activity", "customs-batches", "customs-entries", "payables-emails"]
    assert {k for k, d in sets.items() if d.importable} == {"mailboxes", "vendor-mappings", "subject-formats", "bookings", "scraped-bills"}
