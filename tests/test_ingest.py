from datetime import datetime
from pathlib import Path

from app.core import store
from app.mail.ingest import MailboxSync, dedupe_key, window_filter

from fakes import PDF, FakeGraph, attachment, mailbox, message

NOW = datetime(2026, 11, 1, 12, 0, 0)


def sync(db, graph, box):
    return MailboxSync(db, graph, store.mailbox(db, box["id"]), now=lambda: NOW).run()


def rows(db, table):
    return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")]


def test_new_email_saved_with_its_file(db):
    graph = FakeGraph([message(1)], {"graph-1": [attachment("a1")]})

    assert sync(db, graph, mailbox(db)) == "success"

    [email] = rows(db, "message")
    [document] = rows(db, "file")
    assert email["dedupe_key"] == "<msg-1@forwarder.com>"
    assert email["received"] == "2026-10-26 08:00:00"
    assert email["state"] == "new" and email["body_html"] == "hello"
    assert document["filename"] == "bl.pdf" and document["message_id"] == email["id"]
    assert Path(document["path"]).read_bytes() == PDF
    box = store.mailbox(db, 1)
    assert box["status"] == "success"
    assert box["fetch_resume_from"] == "2026-10-26 08:00:00"
    [run] = rows(db, "run")
    assert run["status"] == "success" and run["documents_created"] == 1


def test_running_twice_creates_no_duplicates(db):
    graph = FakeGraph([message(1)], {"graph-1": [attachment("a1")]})
    box = mailbox(db)
    sync(db, graph, box)
    sync(db, graph, box)

    assert len(rows(db, "message")) == 1
    assert len(rows(db, "file")) == 1
    assert rows(db, "run")[1]["emails_skipped"] == 1


def test_file_already_imported_is_not_saved_again(db):
    graph = FakeGraph([message(1), message(2)],
                      {"graph-1": [attachment("a1")], "graph-2": [attachment("a2")]})
    sync(db, graph, mailbox(db))

    assert len(rows(db, "message")) == 2
    assert len(rows(db, "file")) == 1
    assert "already imported" in rows(db, "message")[1]["skipped_attachments"]
    assert rows(db, "run")[0]["duplicates"] == 1


def test_file_imported_for_bookings_is_saved_again_for_bill_matching(db):
    graph = FakeGraph([message(1)], {"graph-1": [attachment("a1")]})
    sync(db, graph, mailbox(db))
    bills = mailbox(db, name="Bills", purpose="bill_matching")
    sync(db, graph, bills)
    sync(db, FakeGraph([message(2)], {"graph-2": [attachment("a2")]}), bills)

    files = rows(db, "file")
    assert len(files) == 2 and files[0]["path"] == files[1]["path"]
    assert "already imported" in rows(db, "message")[2]["skipped_attachments"]


def test_unsupported_file_is_noted_not_saved(db):
    graph = FakeGraph([message(1)], {"graph-1": [attachment("a1", "notes.txt", b"plain text file")]})
    sync(db, graph, mailbox(db))

    [email] = rows(db, "message")
    assert not rows(db, "file")
    assert "notes.txt: not a PDF or image" in email["skipped_attachments"]


def test_skip_sender_keeps_email_without_downloading(db):
    graph = FakeGraph([message(1)], {"graph-1": [attachment("a1")]})
    sync(db, graph, mailbox(db, skip_sender_domains="forwarder.com"))

    [email] = rows(db, "message")
    assert graph.downloads == 0
    assert email["state"] == "skipped"
    assert "Skip Senders" in email["skipped_attachments"]


def test_group_filter_leaves_out_other_mail_and_explains(db):
    graph = FakeGraph([message(1)])
    sync(db, graph, mailbox(db, group_address="Other Group"))

    assert not rows(db, "message")
    assert "WTD - PH <wtd@company.com> x1" in store.mailbox(db, 1)["group_miss_note"]


def test_quiet_run_after_group_mail_was_stored_raises_no_note(db):
    box = mailbox(db, group_address="WTD - PH")
    sync(db, FakeGraph([message(1)]), box)
    direct = message(2, toRecipients=[{"emailAddress": {"name": "PH Accounting Team",
                                                        "address": "phaccounting@avatco.com"}}])
    sync(db, FakeGraph([message(1), direct]), box)

    assert len(rows(db, "message")) == 1
    assert store.mailbox(db, 1)["group_miss_note"] is None


def test_failed_message_is_retried_next_run_and_others_saved(db):
    graph = FakeGraph([message(1, "2026-10-20T08:00:00Z"), message(2, "2026-10-26T08:00:00Z")],
                      {"graph-2": [attachment("a2")]}, fail_attachments_for={"graph-1"})

    assert sync(db, graph, mailbox(db)) == "error"

    box = store.mailbox(db, 1)
    assert [e["dedupe_key"] for e in rows(db, "message")] == ["<msg-2@forwarder.com>"]
    assert box["fetch_resume_from"] == "2026-10-20 08:00:00"
    assert box["last_error"] == "Graph down"


def test_mailbox_unreadable_marks_run_and_mailbox_failed(db):
    class BrokenGraph(FakeGraph):
        def messages_page(self, url, params=None):
            raise RuntimeError("Token refresh failed")

    assert sync(db, BrokenGraph(), mailbox(db)) == "error"
    assert store.mailbox(db, 1)["last_error"] == "Token refresh failed"
    assert rows(db, "run")[0]["status"] == "error"


def test_bill_matching_keys_carry_the_purpose():
    assert dedupe_key(message(1), "bill_matching") == "bill_matching:<msg-1@forwarder.com>"
    assert dedupe_key(message(1), "freight_booking") == "<msg-1@forwarder.com>"


def test_window_starts_on_monday_or_start_date_or_resume_point():
    box = {"fetch_window_days": 14}
    # 1 Nov 2026 minus 14 days = 18 Oct (Sunday) -> Monday 12 Oct.
    assert window_filter(box, NOW) == "receivedDateTime ge 2026-10-12T00:00:00Z"
    assert window_filter(dict(box, start_from="2026-10-28 00:00:00"), NOW) == \
        "receivedDateTime ge 2026-10-28T00:00:00Z"
    resumed = dict(box, start_from="2026-10-28 00:00:00", fetch_resume_from="2026-10-30 09:15:00")
    assert window_filter(resumed, NOW) == "receivedDateTime ge 2026-10-30T09:15:00Z"
