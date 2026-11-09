"""The emails: the list beside an email, its conversation, paging, processing again."""


import re

from fakes import mailbox, signed_in


def test_retry_puts_the_email_back_and_keeps_the_extraction(client, db):
    token = signed_in(client)
    box = mailbox(db)
    with db:
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key, state, attempts, note) "
                   "VALUES (1, ?, 'k', 'failed', 5, 'AI down')", (box["id"],))
        db.execute("INSERT INTO file (message_id, sha256, path, state, extracted) "
                   "VALUES (1, 'abc', '/x', 'failed', '{}')")
    client.post("/messages/1/retry", data={"csrf": token})

    message = db.execute("SELECT state, attempts, note FROM message").fetchone()
    assert tuple(message) == ("new", 0, None)
    assert tuple(db.execute("SELECT state, extracted FROM file").fetchone()) == ("new", "{}")


def test_an_open_email_shows_beside_its_filtered_list(client, db):
    token = signed_in(client)
    box = mailbox(db)
    with db:
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key, subject, state, received) VALUES "
                   "(1, ?, 'a', 'BKG 1', 'failed', '2026-11-05 10:00:00'), (2, ?, 'b', 'BKG 2', 'done', '2026-11-05 11:00:00')",
                   (box["id"],) * 2)
    page = client.get("/messages/1?state=failed").get_data(as_text=True)
    assert 'href="/messages/1?state=failed" aria-current="true"' in page  # the list keeps the filter
    assert "BKG 2" not in page.split('id="detail"')[0]                     # filtered out of the list
    assert 'data-list-key="emails failed "' in page and 'id="email-detail"' in page
    resp = client.post("/messages/1/retry?state=failed", data={"csrf": token})
    assert resp.headers["Location"] == "/messages/1?state=failed"


def test_an_open_email_shows_its_whole_conversation(client, db):
    signed_in(client)
    bills, bookings = mailbox(db, name="Bills"), mailbox(db, name="Bookings")
    with db:
        db.executemany(
            "INSERT INTO message (id, mailbox_id, dedupe_key, subject, sender, received, state, conversation_id, "
            "internet_message_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'conv', ?)",
            [(1, bookings["id"], "a", "SO 1 new booking", "ops@a.com", "2026-11-05 08:00:00", "done", "<m1>"),
             (2, bills["id"], "b", "RE: SO 1 new booking", "me@b.com", "2026-11-05 09:00:00", "done", "<m2>"),
             (3, bookings["id"], "c", "RE: SO 1 new booking", "me@b.com", "2026-11-05 09:00:00", "review", "<m2>")])
        db.execute("INSERT INTO message (id, mailbox_id, dedupe_key, subject, conversation_id, internet_message_id) "
                   "VALUES (4, ?, 'd', 'Unrelated', 'other', '<m4>')", (bills["id"],))
    page = client.get("/messages/2").get_data(as_text=True)
    thread = page.split('class="thread"')[1].split("</ol>")[0]
    assert thread.index("ops@a.com") < thread.index("me@b.com")          # oldest first
    assert thread.count('class="copy-chip') == 3                         # the reply was read by both mailboxes
    assert 'href="/messages/2" aria-current="true"' in thread and "Unrelated" not in thread
    assert "2 in thread" in client.get("/messages").get_data(as_text=True)


def test_the_email_list_hands_out_the_next_page(client, db, monkeypatch):
    signed_in(client)
    monkeypatch.setattr("app.web.emails.EMAILS_PAGE", 2)
    box = mailbox(db)
    with db:
        for n in range(5):
            db.execute("INSERT INTO message (mailbox_id, dedupe_key, subject, received) VALUES (?, ?, ?, ?)",
                       (box["id"], f"k{n}", f"Mail {n}", f"2026-11-0{n + 1} 08:00:00"))
    first = client.get("/messages").get_data(as_text=True)
    assert "Mail 4" in first and "Mail 3" in first and "Mail 2" not in first
    assert 'data-more="/messages?offset=2"' in first
    second = client.get("/messages?offset=2").get_data(as_text=True)
    assert "Mail 2" in second and "Mail 1" in second and "Mail 4" not in second.split('id="detail"')[0]


def test_the_email_list_groups_every_email_and_opens_a_group(client, db, monkeypatch):
    signed_in(client)
    monkeypatch.setattr("app.web.ui.GROUP_PAGE", 2)
    box = mailbox(db)
    with db:
        for n, state in enumerate(("failed", "done", "failed", "done", "failed")):
            db.execute("INSERT INTO message (mailbox_id, dedupe_key, subject, state, received) VALUES (?, ?, ?, ?, ?)",
                       (box["id"], f"k{n}", f"Mail {n}", state, f"2026-11-0{n + 1} 08:00:00"))
    grouped = client.get("/messages?groupby=state").get_data(as_text=True).split('id="detail"')[0]
    assert grouped.index('<span class="clip">Done</span><span class="n">2</span>')         < grouped.index('<span class="clip">Failed</span><span class="n">3</span>')   # in the order an email goes
    assert "Mail " not in grouped and re.search(r'id="mail-list"\s+data-prefetch', grouped)  # folded, not refreshed
    assert re.search(r'data-list-key="emails +by state"', grouped)
    opened = client.get("/messages?groupby=state&group=failed").get_data(as_text=True).split('id="detail"')[0]
    assert "Mail 4" in opened and "Mail 2" in opened and "Mail 0" not in opened and "Mail 1" not in opened
    assert 'href="/messages/5?groupby=state"' in opened                             # opening an email keeps the grouping
    assert "<b>1-2</b> / 3" in opened
    assert 'href="/messages?groupby=state&amp;group=failed&amp;offset=2" data-group-page aria-label="Next' in opened
    more = client.get("/messages?groupby=state&group=failed&offset=2").get_data(as_text=True).split('id="detail"')[0]
    assert "Mail 0" in more and "Mail 4" not in more
