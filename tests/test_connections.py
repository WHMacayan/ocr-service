"""Several Odoo connections: each that is on gets the same writes and keeps its own history."""

from app.flows import pipeline
from app.core import store
from app import worker
from app.odoo.client import Odoo
from fakes import connection, mailbox
from test_pipeline_end_to_end import ai, email, odoo, row  # noqa: F401 (ai is a fixture)
from test_store_web import client, csrf, signed_in  # noqa: F401 (client is a fixture)


def target(db, message_id, connection_id):
    return db.execute("SELECT * FROM message_target WHERE message_id = ? AND connection_id = ?",
                      (message_id, connection_id)).fetchone()


class Down:
    def __getattr__(self, name):
        raise RuntimeError("Odoo is down")


def test_each_connection_gets_the_writes_and_the_ai_reads_once(db, tmp_path, ai):
    store.set_setting(db, "openai_api_key", "sk-test")
    production, staging = connection(db), connection(db, "Staging", is_production=0)
    message_id = email(db, tmp_path)
    live, test = odoo(), odoo()

    pipeline.process(db, live, production)
    pipeline.process(db, test, staging)

    assert ai.calls == 1
    assert len(live.rows("b2b.ocr.booking")) == 1 and len(test.rows("b2b.ocr.booking")) == 1
    assert target(db, message_id, production)["state"] == target(db, message_id, staging)["state"] == "done"
    assert row(db, "message", message_id)["state"] == "done"


def test_writes_are_recorded_against_their_connection(db):
    class Server:
        def execute_kw(self, *args):
            return 41
    staging = connection(db, "Staging", is_production=0)
    odoo_ = Odoo.__new__(Odoo)
    odoo_.url, odoo_.db, odoo_.uid, odoo_.api_key, odoo_.activity_db = "http://odoo", "d", 7, "k", db
    odoo_.connection_id, odoo_.name, odoo_.source, odoo_._object = staging, "Staging", None, Server()
    box = mailbox(db)
    with db:
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key) VALUES (1, ?, 'a')", (box["id"],))
    with odoo_.working_on(1):
        odoo_.create("b2b.ocr.booking", {"booking_number": "BK1"})
    assert db.execute("SELECT connection_id FROM odoo_change").fetchone()[0] == staging
    assert db.execute("SELECT summary FROM activity WHERE kind = 'odoo'").fetchone()[0].startswith("[Staging] Created")


def test_a_failing_connection_is_retried_alone(db, tmp_path, ai):
    store.set_setting(db, "openai_api_key", "sk-test")
    store.set_setting(db, "max_retries", "2")
    production, development = connection(db), connection(db, "Development", is_production=0)
    message_id = email(db, tmp_path)
    live = odoo()

    pipeline.process(db, live, production)
    pipeline.process(db, Down(), development)

    assert target(db, message_id, production)["state"] == "done"
    failing = target(db, message_id, development)
    assert failing["state"] == "new" and failing["attempts"] == 1
    message = row(db, "message", message_id)
    assert message["state"] == "new" and "Development: Odoo is down" in message["note"]

    pipeline.process(db, live, production)  # production has nothing left to do
    pipeline.process(db, Down(), development)
    assert len(live.rows("b2b.ocr.booking")) == 1
    assert target(db, message_id, development)["state"] == "failed"
    assert row(db, "message", message_id)["state"] == "failed"
    assert row(db, "message", message_id)["attempts"] == 2


def test_mail_before_the_start_date_is_never_written(db, tmp_path, ai):
    store.set_setting(db, "openai_api_key", "sk-test")
    staging = connection(db, "Staging", is_production=0, start_from="2026-10-01 00:00:00")
    old = email(db, tmp_path, received="2026-09-30 23:59:00", subject="Booking AB111111")
    new = email(db, tmp_path, received="2026-10-01 08:00:00")

    pipeline.process(db, odoo(), staging)

    assert target(db, old, staging) is None and row(db, "message", old)["state"] == "new"
    assert target(db, new, staging)["state"] == "done"


def test_a_blank_connection_is_never_used(db, monkeypatch):
    connection(db, "Development", is_production=0, url="", database="", login="", api_key="", active=0)
    monkeypatch.setattr(Odoo, "from_connection", classmethod(lambda cls, db, c: (_ for _ in ()).throw(
        AssertionError("no login for a blank connection"))))
    worker.process_files(db)
    assert store.reference_connection(db) is None
    assert not db.execute("SELECT 1 FROM activity WHERE level != 'info'").fetchone()


def test_the_old_single_connection_becomes_production_with_its_history(db, tmp_path):
    box = mailbox(db)
    with db:
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key, state, attempts, email_done) "
                   "VALUES (1, ?, 'a', 'done', 1, 1)", (box["id"],))
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key, state) VALUES (2, ?, 'b', 'skipped')", (box["id"],))
        db.execute("INSERT INTO file (message_id, sha256, path, state, odoo_id) VALUES (1, 'x', 'p', 'done', 41)")
        db.execute("INSERT INTO odoo_change (message_id, action, model, record_id) VALUES (1, 'created', 'm', 41)")
        db.executemany("INSERT INTO setting (key, value) VALUES (?, ?)",
                       [("odoo_url", "http://katana.local"), ("odoo_db", "1103"), ("odoo_user", "admin"),
                        ("odoo_api_key", store.encrypt(db, "abc0e6a"))])

    again = store.connect()
    [moved] = store.connections(again)
    assert (moved["name"], moved["url"], moved["database"], moved["login"], moved["api_key"]) == (
        "Production", "http://katana.local", "1103", "admin", "abc0e6a")
    assert moved["is_production"] == 1 and moved["active"] == 1 and moved["start_from"] is None
    assert [tuple(r) for r in again.execute("SELECT message_id, state, attempts, email_done FROM message_target")] == [
        (1, "done", 1, 1)]
    assert tuple(again.execute("SELECT file_id, state, odoo_id FROM file_target").fetchone()) == (1, "done", 41)
    assert again.execute("SELECT connection_id FROM odoo_change").fetchone()[0] == moved["id"]
    assert not again.execute("SELECT 1 FROM setting WHERE key LIKE 'odoo_%'").fetchone()
    again.close()


# ── the Settings page ─────────────────────────────────────────────────

def fake_odoos(monkeypatch, by_name):
    """Odoo.from_connection answers with the fake for that connection's name."""
    def login(cls, db, c):
        fake = by_name[c["name"]]
        fake.url, fake.uid = c["url"], 7
        return fake
    monkeypatch.setattr(Odoo, "from_connection", classmethod(login))


def partner(fake, partner_id, name):
    fake.rows("res.partner")[partner_id] = {"display_name": name}


def test_production_is_turned_on_only_with_its_name_typed(client, db, monkeypatch):
    token = signed_in(client)
    production = connection(db, active=0)
    fake_odoos(monkeypatch, {"Production": odoo()})

    page = client.post(f"/connections/{production}/on", data={"csrf": token, "start_from": "2026-11-01"},
                       follow_redirects=True).get_data(as_text=True)
    assert "Type Production exactly" in page and not store.connection(db, production)["active"]

    client.post(f"/connections/{production}/on",
                data={"csrf": token, "start_from": "2026-11-01", "confirm_name": "Production"})
    on = store.connection(db, production)
    assert on["active"] == 1 and on["start_from"] == "2026-11-01 00:00:00"
    entry = db.execute("SELECT level, summary FROM activity WHERE kind = 'settings' ORDER BY id DESC").fetchone()
    assert tuple(entry) == ("warning", "Turned on Odoo connection Production (production)")
    assert "Writing to PRODUCTION: Production" in client.get("/messages").get_data(as_text=True)


def test_a_connection_whose_mappings_differ_stays_off(client, db, monkeypatch):
    token = signed_in(client)
    with db:
        db.execute("INSERT INTO vendor_mapping (role, partner_id, partner_name) VALUES ('forwarder', 7, 'Maersk')")
    production, staging = connection(db), connection(db, "Staging", is_production=0, active=0)
    live, test = odoo(), odoo()
    partner(live, 7, "Maersk")
    partner(test, 7, "Hapag-Lloyd")
    fake_odoos(monkeypatch, {"Production": live, "Staging": test})

    page = client.post(f"/connections/{staging}/on", data={"csrf": token, "start_from": "2026-11-01"},
                       follow_redirects=True).get_data(as_text=True)
    assert 'Vendor partner #7: &#34;Maersk&#34; in Production, &#34;Hapag-Lloyd&#34; in Staging.' in page
    assert not store.connection(db, staging)["active"]
    assert "Check failed" in client.get("/settings").get_data(as_text=True)

    partner(test, 7, "Maersk")
    client.post(f"/connections/{staging}/on", data={"csrf": token, "start_from": "2026-11-01"})
    assert store.connection(db, staging)["active"] == 1 and store.reference_connection(db)["id"] == production


def test_a_mapping_edit_that_differs_between_connections_is_refused(client, db, monkeypatch):
    token = signed_in(client)
    connection(db)
    connection(db, "Staging", is_production=0)
    live, test = odoo(), odoo()
    partner(live, 7, "Maersk")
    fake_odoos(monkeypatch, {"Production": live, "Staging": test})

    page = client.post("/vendor-mappings/new", data={"csrf": token, "role": "forwarder", "partner_id": "7"},
                       follow_redirects=True).get_data(as_text=True)
    assert "Vendor partner #7: not found in Staging." in page
    assert not db.execute("SELECT 1 FROM vendor_mapping").fetchone()

    partner(test, 7, "Maersk")
    client.post("/vendor-mappings/new", data={"csrf": token, "role": "forwarder", "partner_id": "7"})
    assert db.execute("SELECT partner_id FROM vendor_mapping").fetchone()[0] == 7


def test_an_incomplete_connection_cannot_be_turned_on_and_is_deleted_by_name(client, db):
    token = signed_in(client)
    client.post("/connections/new", data={"csrf": token, "name": "Development", "url": "http://dev.local"})
    [dev] = store.connections(db)
    assert "Incomplete" in client.get("/settings").get_data(as_text=True)
    client.post(f"/connections/{dev['id']}/on", data={"csrf": token, "start_from": "2026-11-01"})
    assert not store.connection(db, dev["id"])["active"]

    client.post(f"/connections/{dev['id']}/delete", data={"csrf": token, "confirm_name": "dev"})
    assert store.connection(db, dev["id"])
    client.post(f"/connections/{dev['id']}/delete", data={"csrf": token, "confirm_name": "Development"})
    assert not store.connections(db)


def test_process_again_in_one_connection_leaves_the_others_done(client, db, tmp_path, ai):
    signed_in(client)
    store.set_setting(db, "openai_api_key", "sk-test")
    production, staging = connection(db), connection(db, "Staging", is_production=0)
    message_id = email(db, tmp_path)
    pipeline.process(db, odoo(), production)
    pipeline.process(db, odoo(), staging)

    client.post(f"/messages/{message_id}/retry", data={"csrf": csrf(client, f"/messages/{message_id}"),
                                                       "connection_id": staging})
    assert target(db, message_id, production)["state"] == "done"
    assert target(db, message_id, staging)["state"] == "new"
    assert row(db, "message", message_id)["state"] == "new"
