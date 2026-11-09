"""PayablesGraph: what Freight Payables reads beyond the shared Graph client."""

from app.payables.graph import PayablesGraph

BOX = {"access_token": "token", "token_expires_at": "2999-01-01 00:00:00"}


class Response:
    def __init__(self, status, body=None, content=b"", headers=None):
        self.status_code = status
        self._body = body or {}
        self.content = content
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        return self._body


class Http:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.urls.append((url, params))
        return self.responses.pop(0)


def graph(*responses):
    http = Http(*responses)
    return PayablesGraph(dict(BOX), lambda values: None, http=http, sleep=lambda seconds: None), http


def test_inline_attachments_kept():
    g, _http = graph(Response(200, {"value": [
        {"id": "a", "name": "invoice.pdf", "size": 10, "isInline": False},
        {"id": "b", "name": "image001.png", "size": 30000, "isInline": True},
        {"id": "c", "name": "empty.txt", "size": 0, "isInline": False}]}))
    assert [att["id"] for att in g.all_attachments("m1")] == ["a", "b"]


def test_attachment_bytes_reads_the_raw_value():
    g, http = graph(Response(200, content=b"From: a@b\r\n\r\nforwarded"))
    assert g.attachment_bytes("m1", "att1") == b"From: a@b\r\n\r\nforwarded"
    assert http.urls[0][0].endswith("/me/messages/m1/attachments/att1/$value")


def test_attachment_bytes_retries_busy_outlook():
    g, http = graph(Response(429, headers={"Retry-After": "1"}), Response(200, content=b"raw"))
    assert g.attachment_bytes("m1", "att1") == b"raw"
    assert len(http.urls) == 2


def test_unique_body():
    g, http = graph(Response(200, {"uniqueBody": {"content": "<p>new part</p>"}}))
    assert g.unique_body("m1") == "<p>new part</p>"
    assert http.urls[0][1] == {"$select": "uniqueBody"}
