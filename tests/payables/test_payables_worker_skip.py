"""The main worker never reads a Freight Payables mailbox: its mail must never reach the Odoo pipeline."""

from app import worker

from fakes import mailbox


def record_reads(monkeypatch, steps):
    class Sync:
        def __init__(self, db, graph, box):
            self.name = box["name"]

        def run(self):
            steps.append(f"read {self.name}")
    monkeypatch.setattr(worker, "MailboxSync", Sync)
    monkeypatch.setattr(worker, "process_files", lambda db: steps.append("process"))


def test_main_worker_skips_payables_mailboxes(db, monkeypatch):
    mailbox(db, name="Bookings")
    mailbox(db, name="Payables", purpose="freight_payables")
    steps = []
    record_reads(monkeypatch, steps)

    worker.run_once(db)

    assert steps == ["read Bookings", "process", "process"]


def test_existing_purposes_unchanged(db, monkeypatch):
    mailbox(db, name="Bookings")
    mailbox(db, name="Bills", purpose="bill_matching")
    mailbox(db, name="Customs", purpose="customs_entry")
    steps = []
    record_reads(monkeypatch, steps)

    worker.run_once(db)

    assert steps == ["read Bookings", "process", "read Bills", "process", "read Customs", "process", "process"]
