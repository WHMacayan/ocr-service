"""The setup lists (mailboxes, vendor mappings, subject formats) as data sets: what is exported, and how a file is matched,
checked and written."""

from app.shared.data import Context, dataset, mapped_rows

from fakes import connection


def seed(db):
    with db:
        db.executemany(
            "INSERT INTO vendor_mapping (role, document_name, sender_domain, partner_id, partner_name, active, freight_mode, "
            "cost_per_container) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [("forwarder", "QUARTZ LOGISTICS", None, 5, "Quartz Logistics (NY) Inc.", 1, "ocean", 4900.0),
             ("forwarder", "K-APEX", "kln.com", 6, "Apex Maritime Co.,Inc.", 1, None, None)])


def test_a_list_is_exported_as_a_file_shows_it_and_never_with_its_secrets(db):
    seed(db)
    ds, ctx = dataset("vendor-mappings"), Context(db)
    assert ds.rows(ctx, ["id", "role", "partner_name", "active", "freight_mode", "cost_per_container"], {}) == [
        {"id": 1, "role": "Forwarder", "partner_name": "Quartz Logistics (NY) Inc.", "active": "Yes", "freight_mode": "Ocean",
         "cost_per_container": 4900.0},
        {"id": 2, "role": "Forwarder", "partner_name": "Apex Maritime Co.,Inc.", "active": "Yes", "freight_mode": None,
         "cost_per_container": None}]
    names = {c.name for c in dataset("mailboxes").columns(ctx)}
    assert {"name", "purpose", "company_id", "tenant_id", "client_id", "folder"} <= names
    assert not names & {"client_secret", "refresh_token", "access_token", "token_expires_at"}


def run(db, key, rows, mapping):
    """The plan of an import of ``rows`` (lists of cells), their columns being ``mapping`` (column names)."""
    ds = dataset(key)
    return ds, ds.plan(Context(db), mapped_rows(rows, mapping))


def test_rows_are_matched_by_id_or_by_what_identifies_a_record_then_created_or_updated(db):
    seed(db)
    with db:
        db.execute("INSERT INTO vendor_mapping (role, document_name, cost_per_container) VALUES ('forwarder', 'MSC', 3000)")
    mapping = ["id", "role", "document_name", "sender_domain", "cost_per_container"]
    ds, plan = run(db, "vendor-mappings", [
        [1, None, None, None, 6500],                        # by ID: its rate changes
        [None, "Forwarder", "k-apex", "KLN.COM", 7000],     # by role, name and domain, however they are cased
        [None, "Factory", "SENTURY", None, 100],            # nothing like it: added
        [None, "Forwarder", "MSC", None, 3000],             # as it is already
    ], mapping)
    assert [r.action for r in plan.rows] == ["update", "update", "create", "unchanged"]
    assert plan.rows[0].changes == [("Cost per container", 4900.0, 6500.0)] and plan.rows[1].record_id == 2
    assert plan.rows[2].values == {"role": "factory", "document_name": "SENTURY", "cost_per_container": 100.0}
    assert [r.label for r in plan.rows] == ["Quartz Logistics (NY) Inc.", "Apex Maritime Co.,Inc.", "SENTURY", "MSC"]       # each row says which record
    assert db.execute("SELECT COUNT(*) FROM vendor_mapping").fetchone()[0] == 3     # testing writes nothing
    assert db.execute("SELECT cost_per_container FROM vendor_mapping WHERE id = 1").fetchone()[0] == 4900.0


def test_a_row_that_cannot_be_used_says_why_and_the_others_go_in(db):
    seed(db)
    mapping = ["id", "role", "document_name", "sender_domain", "cost_per_container", "active"]
    ds, plan = run(db, "vendor-mappings", [
        [99, None, None, None, 1, None],                    # no such ID
        [None, "Carrier", "MAERSK", None, None, None],      # not a role
        [None, "Forwarder", "MSC", None, "cheap", None],    # not a number
        [None, None, "ORPHAN", None, None, None],           # a record needs its role
        [None, "Forwarder", "NEW ONE", None, 3000, "Yes"],  # fine
        [None, "Forwarder", "NEW ONE", None, 3100, "Yes"],  # the same record twice in the file
    ], mapping)
    assert [r.action for r in plan.rows] == ["error", "error", "error", "error", "create", "error"]
    assert plan.rows[0].errors == ["No vendor mapping has ID 99."]
    assert plan.rows[1].errors == ["Role: choose one of: Forwarder, Factory"]
    assert plan.rows[2].errors == ["Cost per container: write a number, such as 4900 or 4900.50"]
    assert plan.rows[3].errors == ["Role is required."] and plan.rows[5].errors == ["Same vendor mapping as row 6."]
    ds.apply(Context(db), plan)
    assert [r.done for r in plan.rows] == [False, False, False, False, True, False]
    assert db.execute("SELECT COUNT(*) FROM vendor_mapping").fetchone()[0] == 3
    added = db.execute("SELECT role, document_name, cost_per_container, active FROM vendor_mapping WHERE id = ?",
                       (plan.rows[4].record_id,)).fetchone()
    assert tuple(added) == ("forwarder", "NEW ONE", 3000.0, 1)


def test_a_file_that_does_not_say_which_record_a_row_is_stops_the_import(db):
    seed(db)
    ds, plan = run(db, "vendor-mappings", [["Forwarder", 5000]], ["role", "cost_per_container"])
    assert plan.rows == [] and "Map the ID column, or all of Role, Name on documents, Sender domain" in plan.problems[0]


def test_the_same_checks_as_the_form_apply_to_each_row(db):
    ds, plan = run(db, "subject-formats", [
        ["Maersk", r"BKG (?P<booking_number>\d+)", "Yes"],
        ["Broken", r"BKG (\d+", "No"],                      # not a regular expression
        ["Nameless pattern", r"BKG \d+", "No"],             # no named group
        ["Odd group", r"(?P<colour>\w+)", "No"],            # a group the service does not know
    ], ["name", "subject_pattern", "confirms_booking"])
    assert [r.action for r in plan.rows] == ["create", "error", "error", "error"]
    assert "not a valid regular expression" in plan.rows[1].errors[0]
    assert "needs at least one named group" in plan.rows[2].errors[0] and "Unknown group(s) colour" in plan.rows[3].errors[0]
    ds.apply(Context(db), plan)
    assert [tuple(r) for r in db.execute("SELECT name, confirms_booking FROM subject_format")] == [("Maersk", 1)]


def test_a_mailbox_is_added_by_its_purpose_name_and_a_secret_cannot_be_imported(db):
    ds, plan = run(db, "mailboxes", [["Freight Inbox", "Freight Booking", 1, "https://x.test", "oops"]],
                   ["name", "purpose", "company_id", "tenant_id", "client_secret"])
    ds.apply(Context(db), plan)
    row = db.execute("SELECT name, purpose, company_id, tenant_id, client_secret FROM mailbox").fetchone()
    assert tuple(row)[:4] == ("Freight Inbox", "freight_booking", 1, "https://x.test") and row["client_secret"] is None


def test_an_odoo_record_a_row_points_at_must_exist_and_its_name_is_filled_in(db, monkeypatch):
    connection(db)

    class Odoo:
        name = "Staging (1115)"

        def search_read(self, model, domain, fields, **kwargs):
            names = {5: "Quartz Logistics (NY) Inc."}
            return [{"id": i, "display_name": names[i]} for i in domain[0][2] if i in names]

    monkeypatch.setattr("app.shared.odoo.client.Odoo.from_connection", lambda db, c: Odoo())
    monkeypatch.setattr("app.shared.odoo.client.Odoo.from_store", lambda db: Odoo())
    ds, plan = run(db, "vendor-mappings", [["Forwarder", "QUARTZ", None, 5, None], ["Forwarder", "GHOST", None, 77, None]],
                   ["role", "document_name", "sender_domain", "partner_id", "partner_name"])
    assert plan.problems == ["Vendor partner #77: not found in Production."]
    assert plan.rows[0].values["partner_name"] == "Quartz Logistics (NY) Inc."        # read from Odoo: none was given
