"""PayablesSync: Freight Payables stores its mail in its own tables, completely enough for the audit."""

import base64
import io
import zipfile
from datetime import datetime
from pathlib import Path

from app.core import store
from app.mail.ingest import MailboxSync
from app.payables.ingest import PayablesSync

from fakes import PDF, FakeGraph, attachment, mailbox, message

NOW = datetime(2026, 11, 1, 12, 0, 0)
PNG = b"\x89PNG\r\n\x1a\n"
BIG_PICTURE = PNG + b"\0" * 25_000
LOGO = PNG + b"\0" * 1_000
ITEM = "#microsoft.graph.itemAttachment"
MIME = b"From: carrier@hlag.com\r\nSubject: invoice\r\n\r\nplease pay"


def xlsx():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", b"x")
    return buffer.getvalue()


def zipped_pdf():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("invoice.pdf", PDF)
    return buffer.getvalue()


class PayablesGraph(FakeGraph):
    """FakeGraph plus what PayablesGraph adds: inline attachments, raw bytes, reply-only text."""

    def __init__(self, messages=(), files=None, raw=None, unique=None):
        super().__init__(messages, files)
        self.raw = raw or {}
        self.unique = unique or {}

    def all_attachments(self, message_id):
        return [{k: v for k, v in att.items() if k != "contentBytes"} for att in self.files.get(message_id, [])]

    def attachment_bytes(self, message_id, attachment_id):
        self.downloads += 1
        return self.raw[(message_id, attachment_id)]

    def unique_body(self, message_id):
        return self.unique.get(message_id)


def payables_box(db, **extra):
    return mailbox(db, name="Freight Payables", purpose="freight_payables", **extra)


def sync(db, graph, box, sync_class=PayablesSync):
    return sync_class(db, graph, store.mailbox(db, box["id"]), now=lambda: NOW).run()


def rows(db, table):
    return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


def test_keeps_excel_zip_eml_msg_and_big_inline_pictures(db):
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64
    graph = PayablesGraph([message(1)], {"graph-1": [
        attachment("a1", "SOA.xlsx", xlsx()),
        attachment("a2", "invoices.zip", zipped_pdf()),
        attachment("a3", "FW invoice.msg", ole),
        attachment("a4", "FW rate.eml", MIME),
        attachment("a5", "image001.png", BIG_PICTURE, isInline=True),
        attachment("a6", "image002.png", LOGO, isInline=True),
        attachment("a7", "notes.txt", b"plain text"),
    ]})

    assert sync(db, graph, payables_box(db)) == "success"

    links = rows(db, "payables_message_file")
    assert sorted(link["filename"] for link in links) == [
        "FW invoice.msg", "FW rate.eml", "SOA.xlsx", "image001.png", "invoices.zip"]
    assert [link["filename"] for link in links if link["is_inline"]] == ["image001.png"]
    [email] = rows(db, "payables_message")
    assert "image002.png: inline image under 20 KB (signature / logo)" in email["skipped_attachments"]
    assert "notes.txt" in email["skipped_attachments"]
    kinds = {row["kind"] for row in rows(db, "payables_file")}
    assert kinds == {"xlsx", "zip", "msg", "eml", "image"}
    for row in rows(db, "payables_file"):
        assert Path(row["path"]).parent.parent.name == "payables"


def test_item_attachment_saved_as_eml(db):
    graph = PayablesGraph([message(1)], {"graph-1": [
        {"id": "i1", "name": "FW: invoice", "size": len(MIME), "isInline": False, "@odata.type": ITEM}]},
        raw={("graph-1", "i1"): MIME})

    sync(db, graph, payables_box(db))

    [link] = rows(db, "payables_message_file")
    assert link["filename"] == "FW: invoice.eml"
    [saved] = rows(db, "payables_file")
    assert Path(saved["path"]).read_bytes() == MIME and saved["kind"] == "eml"


def test_skip_senders(db):
    graph = PayablesGraph([message(1)], {"graph-1": [attachment("a1")]})

    sync(db, graph, payables_box(db, skip_sender_domains="csd.hlag.com, forwarder.com"))

    [email] = rows(db, "payables_message")
    assert email["state"] == "skipped"
    assert graph.downloads == 0
    assert rows(db, "payables_file") == []


def test_unique_body_stored(db):
    graph = PayablesGraph([message(1)], {}, unique={"graph-1": "<p>new part</p>"})

    sync(db, graph, payables_box(db))

    [email] = rows(db, "payables_message")
    assert email["unique_body_html"] == "<p>new part</p>"
    assert email["body_html"] == "hello"
    assert email["received"] == "2026-10-26 08:00:00"
    assert email["dedupe_key"] == "freight_payables:<msg-1@forwarder.com>"


def test_same_file_in_two_threads_linked_to_both(db):
    graph = PayablesGraph([message(1, conversationId="thread-a"), message(2, conversationId="thread-b")],
                          {"graph-1": [attachment("a1", "invoice.pdf")],
                           "graph-2": [attachment("a2", "invoice copy.pdf")]})

    sync(db, graph, payables_box(db))

    [saved] = rows(db, "payables_file")
    links = rows(db, "payables_message_file")
    assert len(links) == 2 and {link["file_id"] for link in links} == {saved["id"]}
    assert sorted(link["filename"] for link in links) == ["invoice copy.pdf", "invoice.pdf"]


def test_known_keys_use_payables_tables(db):
    bookings = mailbox(db, name="Bookings")
    payables = payables_box(db)
    graph = PayablesGraph([message(1)], {"graph-1": [attachment("a1")]})

    sync(db, graph, bookings, sync_class=MailboxSync)
    sync(db, graph, payables)
    sync(db, graph, payables)

    assert len(rows(db, "message")) == 1
    assert len(rows(db, "payables_message")) == 1


def test_payables_mail_never_enters_odoo_tables(db):
    graph = PayablesGraph([message(1)], {"graph-1": [attachment("a1")]})

    sync(db, graph, payables_box(db))

    for table in ("message", "file", "message_target", "file_target", "odoo_change"):
        assert rows(db, table) == [], table
    assert len(rows(db, "payables_message")) == 1


def test_run_history_recorded(db):
    graph = PayablesGraph([message(1)], {"graph-1": [attachment("a1")]})

    sync(db, graph, payables_box(db))

    [run] = rows(db, "run")
    assert run["status"] == "success" and run["documents_created"] == 1
    assert store.mailbox(db, 1)["fetch_resume_from"] == "2026-10-26 08:00:00"


def test_inline_pictures_are_base64_decoded_like_files(db):
    graph = PayablesGraph([message(1)], {"graph-1": [attachment("a5", "image001.png", BIG_PICTURE, isInline=True)]})

    sync(db, graph, payables_box(db))

    [saved] = rows(db, "payables_file")
    assert Path(saved["path"]).read_bytes() == BIG_PICTURE
    assert saved["size"] == len(BIG_PICTURE)
    assert base64.b64encode(BIG_PICTURE)  # sanity: the fake sends contentBytes, as Graph does
