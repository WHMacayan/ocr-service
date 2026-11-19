"""Side by side: the AI reads a run's files several at once, and each worker handles only its mailboxes."""

import threading
import time

from app.shared.core import store
from app.shared.documents import extract
from app.shared import pipeline
from app.shared import worker

from fakes import CONFIRMATION, connection, email, odoo


def files_of(db, tmp_path, n):
    """n freight emails, each with its own file."""
    for i in range(n):
        email(db, tmp_path, received=f"2026-10-0{i + 1} 08:00:00", subject=f"Booking AB12345{i}")
    return db.execute("SELECT COUNT(*) FROM file").fetchone()[0]


def test_the_ai_reads_several_files_at_once_before_anything_is_written(db, tmp_path, monkeypatch):
    store.set_setting(db, "openai_api_key", "k")
    store.set_setting(db, "ai_parallel", "4")
    count = files_of(db, tmp_path, 4)
    lock, now, most = threading.Lock(), [0], [0]

    def read_file(raw, purpose, settings):
        with lock:
            now[0] += 1
            most[0] = max(most[0], now[0])
        time.sleep(0.2)
        with lock:
            now[0] -= 1
        return "booking_confirmation", dict(CONFIRMATION)
    monkeypatch.setattr(extract, "read_file", read_file)
    started = time.monotonic()
    pipeline.process(db, odoo(), connection(db))
    assert most[0] == 4 and time.monotonic() - started < 0.2 * count     # side by side, not one after another
    assert db.execute("SELECT COUNT(*) FROM file WHERE extracted IS NOT NULL").fetchone()[0] == count


def test_a_file_not_read_ahead_is_read_in_its_turn(db, tmp_path, monkeypatch):
    store.set_setting(db, "openai_api_key", "k")
    files_of(db, tmp_path, 1)
    calls = []

    def read_file(raw, purpose, settings):
        calls.append(threading.current_thread() is threading.main_thread())
        if len(calls) == 1:
            raise TimeoutError("the AI took too long")
        return "booking_confirmation", dict(CONFIRMATION)
    monkeypatch.setattr(extract, "read_file", read_file)
    pipeline.process(db, odoo(), connection(db))
    assert calls == [False, True]                                         # ahead failed; read again in its turn
    assert db.execute("SELECT doc_class FROM file").fetchone()[0] == "booking_confirmation"


def test_one_at_a_time_reads_in_turn_as_before(db, tmp_path, ai):
    store.set_setting(db, "openai_api_key", "k")
    store.set_setting(db, "ai_parallel", "1")
    files_of(db, tmp_path, 2)
    pipeline.process(db, odoo(), connection(db))
    assert ai.calls == 2


def test_a_worker_handles_only_its_own_mailboxes(db, tmp_path, ai, monkeypatch):
    store.set_setting(db, "openai_api_key", "k")
    freight = email(db, tmp_path, purpose="freight_booking")
    bills = email(db, tmp_path, purpose="bill_matching", sender="ar@factory.cn", received="2026-10-02 08:00:00")
    ai.answer = ("supplier_invoice", {"is_invoice": True, "invoice_number": "INV-1", "vendor_name": "Good Factory"})
    pipeline.process(db, odoo(), connection(db), purposes=("bill_matching",))
    done = {row[0] for row in db.execute("SELECT message_id FROM message_target")}
    assert done == {bills} and freight not in done

    monkeypatch.setenv("WORKER_PURPOSES", "freight_booking, customs_entry")
    assert worker.worker_purposes() == ("freight_booking", "customs_entry")
    monkeypatch.delenv("WORKER_PURPOSES")
    assert worker.worker_purposes() is None                              # unset: every mailbox, one worker
