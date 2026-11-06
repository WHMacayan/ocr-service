from app import search
from app.core import store

from fakes import connection, mailbox
from test_store_web import client, signed_in  # noqa: F401  (fixtures)


def seed(db):
    box = mailbox(db, name="Freight Bookings")
    with db:
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key, subject, sender, received, state, note) VALUES "
                   "(1, ?, 'a', 'BKG 241188 confirmation', 'ops@quartz.com', '2026-11-01 08:00:00', 'done', NULL),"
                   "(2, ?, 'b', 'Arrival notice', 'notice@one-line.com', '2026-11-02 08:00:00', 'failed', 'Odoo down'),"
                   "(3, ?, 'c', 'Invoice 50%_off', 'billing@apex.com', '2026-11-03 08:00:00', 'review', NULL)",
                   (box["id"],) * 3)
        db.execute("INSERT INTO file (message_id, filename, sha256, path, extracted) VALUES "
                   "(2, 'an.pdf', 'x', '/x', '{\"containers\": [\"MSCU1234567\"]}')")
        db.execute("INSERT INTO vendor_mapping (role, partner_name, sender_domain, document_name) "
                   "VALUES ('forwarder', 'Quartz Logistics (NY) Inc.', 'quartz.com', 'QUARTZ LOGISTICS')")
    return box


def titles(groups, key):
    return [i.title for k, _l, items, _m in groups if k == key for i in items]


def test_parse_words_phrases_and_filters():
    q = search.parse('quartz "booking confirmation" state:failed from:apex.com http://x')
    assert q.terms == ["quartz", "booking confirmation", "http://x"]
    assert q.filters == {"state": "failed", "from": "apex.com"}


def test_finds_emails_by_what_was_read_from_their_files(db):
    seed(db)
    assert titles(search.run(db, "MSCU1234567"), "emails") == ["Arrival notice"]


def test_every_word_must_match_across_fields_and_groups(db):
    seed(db)
    groups = search.run(db, "quartz")
    assert titles(groups, "emails") == ["BKG 241188 confirmation"]
    assert titles(groups, "vendors") == ["Quartz Logistics (NY) Inc."]
    assert titles(search.run(db, "quartz arrival"), "emails") == []


def test_filters_and_in(db):
    seed(db)
    assert titles(search.run(db, "state:needs-review"), "emails") == ["Invoice 50%_off"]
    assert titles(search.run(db, "from:one-line"), "emails") == ["Arrival notice"]
    groups = search.run(db, "quartz in:vendors")
    assert [k for k, *_ in groups] == ["vendors"]


def test_like_wildcards_are_literal(db):
    seed(db)
    assert titles(search.run(db, "50%_off"), "emails") == ["Invoice 50%_off"]
    assert titles(search.run(db, "%"), "emails") == ["Invoice 50%_off"]


def test_settings_and_pages_are_found(db):
    groups = search.run(db, "api key")
    assert "Odoo connections" in titles(groups, "settings") and "OpenAI API key" in titles(groups, "settings")
    connection(db, "Staging (1103)", database="1103")
    assert titles(search.run(db, "1103"), "settings") == ["Staging (1103)"]
    assert titles(search.run(db, "password"), "pages") == ["Change password"]


def test_quick_search_json_and_highlighted_page(client, db):
    seed(db)
    signed_in(client)
    data = client.get("/search?format=json&q=arrival").get_json()
    assert data["groups"][0]["label"] == "Emails"
    assert data["groups"][0]["items"][0]["url"] == "/messages/2"
    page = client.get("/search?q=arrival").get_data(as_text=True)
    assert "<mark>Arrival</mark> notice" in page


def test_highlight_escapes_html(client, db):
    box = mailbox(db)
    with db:
        db.execute("INSERT INTO message (mailbox_id, dedupe_key, subject, sender, state) "
                   "VALUES (?, 'z', '<script>alert(1)</script> BKG', 'x@y.com', 'new')", (box["id"],))
    signed_in(client)
    page = client.get("/search?q=BKG").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_emails_page_searches_all_emails(client, db):
    seed(db)
    signed_in(client)
    page = client.get("/messages?q=apex").get_data(as_text=True)
    assert "Invoice 50%_off" in page and "Arrival notice" not in page
    assert "1 email match" in page
