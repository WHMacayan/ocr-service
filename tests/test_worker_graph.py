from datetime import datetime

import requests

from app.shared.core import store
from app.shared.mail.graph import Graph
from app.shared.worker import is_due

from fakes import mailbox

NOW = datetime(2026, 11, 1, 12, 0, 0)


def due(**values):
    return is_due(dict({"sync_requested": 0, "auto_sync": 1, "last_sync": None, "sync_interval": 15},
                       **values), NOW)


def test_due_when_requested_or_interval_passed():
    assert due(sync_requested=1, auto_sync=0)
    assert not due(auto_sync=0)
    assert due(last_sync=None)
    assert due(last_sync="2026-11-01 11:45:00")
    assert not due(last_sync="2026-11-01 11:50:00")


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, gets):
        self.gets = list(gets)
        self.posts = 0

    def post(self, url, data, timeout):
        self.posts += 1
        return Response(200, {"access_token": "new-access", "refresh_token": "rotated",
                              "expires_in": 3600})

    def get(self, url, headers, params, timeout):
        result = self.gets.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def graph_with(db, gets):
    box = mailbox(db)
    http = FakeHttp(gets)
    waits = []
    graph = Graph(box, lambda values: store.update_mailbox(db, box["id"], values),
                  http=http, sleep=waits.append)
    return graph, http, waits


def test_rotated_token_is_saved_encrypted(db):
    graph, http, _waits = graph_with(db, [Response(200, {"value": []})])
    graph.messages_page("https://graph/messages")

    assert http.posts == 1
    stored = store.mailbox(db, 1)
    assert stored["refresh_token"] == "rotated"
    assert stored["access_token"] == "new-access"
    raw = db.execute("SELECT refresh_token FROM mailbox WHERE id = 1").fetchone()[0]
    assert raw != "rotated"


def test_outlook_busy_waits_as_asked_then_retries(db):
    graph, _http, waits = graph_with(db, [
        Response(429, headers={"Retry-After": "7"}),
        requests.exceptions.ConnectionError("dropped"),
        Response(200, {"value": [{"id": "m1"}], "@odata.nextLink": "next"}),
    ])
    messages, next_link = graph.messages_page("https://graph/messages")

    assert waits == [7, 5]
    assert messages == [{"id": "m1"}] and next_link == "next"


def test_email_outcomes_are_logged_once(db):
    from app.shared.worker import log_outcomes
    box = mailbox(db)
    with db:
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key, subject, state, note) "
                   "VALUES (1, ?, 'a', 'BKG 1', 'done', NULL), (2, ?, 'b', 'Invoice 9', 'review', 'Waiting for booking'),"
                   " (3, ?, 'c', 'Same', 'review', 'Unchanged')", (box["id"],) * 3)
    log_outcomes(db, {1: ("new", None), 2: ("new", None), 3: ("review", "Unchanged")})

    rows = [tuple(r) for r in db.execute("SELECT summary, level, link FROM activity ORDER BY id")]
    assert rows == [("Processed: BKG 1", "info", "/messages/1"),
                    ("Needs review: Invoice 9", "warning", "/messages/2")]


# ── review F06 and F07 ──

def test_attachments_on_later_pages_are_listed(db):
    graph, http, _waits = graph_with(db, [
        Response(200, {"value": [{"id": "a1", "size": 5}], "@odata.nextLink": "https://graph/next"}),
        Response(200, {"value": [{"id": "a2", "size": 5}, {"id": "a3", "size": 0}]}),
    ])
    assert [a["id"] for a in graph.attachments("m1")] == ["a1", "a2"]
    assert http.gets == []  # both pages were requested


def test_an_attachment_list_that_never_ends_is_an_error_not_a_short_list(db, monkeypatch):
    from app.shared.mail import graph as graph_module
    from app.shared.mail.graph import GraphError
    monkeypatch.setattr(graph_module, "MAX_ATTACHMENT_PAGES", 3)
    page = Response(200, {"value": [{"id": "a", "size": 1}], "@odata.nextLink": "https://graph/next"})
    graph, _http, _waits = graph_with(db, [page] * 5)
    import pytest
    with pytest.raises(GraphError, match="longer than 3 pages"):
        graph.attachments("m1")


def test_a_mailbox_that_stays_throttled_gives_up(db):
    import pytest
    from app.shared.mail.graph import BUSY_RETRIES, GraphError
    graph, _http, waits = graph_with(db, [Response(429, headers={"Retry-After": "30"})] * 100)
    with pytest.raises(GraphError, match="stayed busy"):
        graph.messages_page("https://graph/messages")
    assert len(waits) <= BUSY_RETRIES and sum(waits) <= 600


def test_the_wait_is_validated(db):
    from app.shared.mail.graph import DEFAULT_WAIT_SECONDS, MAX_WAIT_SECONDS, retry_after
    cases = {"7": 7, "0": DEFAULT_WAIT_SECONDS, "-5": DEFAULT_WAIT_SECONDS, "99999": MAX_WAIT_SECONDS,
             "Wed, 21 Oct 2026 07:28:00 GMT": DEFAULT_WAIT_SECONDS, "": DEFAULT_WAIT_SECONDS, "nan": DEFAULT_WAIT_SECONDS,
             "inf": DEFAULT_WAIT_SECONDS}
    for header, expected in cases.items():
        assert retry_after(Response(429, headers={"Retry-After": header})) == expected, header
    assert retry_after(Response(429)) == DEFAULT_WAIT_SECONDS


def test_the_payables_client_also_follows_attachment_pages_and_gives_up_when_busy(db):
    import pytest
    from app.payables.graph import PayablesGraph
    from app.shared.mail.graph import GraphError
    box = mailbox(db)
    http = FakeHttp([Response(200, {"value": [{"id": "a1", "size": 5}], "@odata.nextLink": "https://graph/next"}),
                     Response(200, {"value": [{"id": "a2", "size": 5, "isInline": True}]})])
    graph = PayablesGraph(box, lambda values: None, http=http, sleep=lambda s: None)
    assert [a["id"] for a in graph.all_attachments("m1")] == ["a1", "a2"]
    class Busy(FakeHttp):
        def get(self, url, headers, timeout, params=None):
            return Response(503)
    busy = Busy([])
    graph = PayablesGraph(box, lambda values: None, http=busy, sleep=lambda s: None)
    with pytest.raises(GraphError, match="stayed busy"):
        graph.attachment_bytes("m1", "a1")
