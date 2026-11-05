from app import worker

from fakes import mailbox


def test_files_are_processed_after_each_mailbox_read(db, monkeypatch):
    mailbox(db, name="Bookings")
    mailbox(db, name="Bills", purpose="bill_matching")
    steps = []

    class Sync:
        def __init__(self, db, graph, box):
            self.name = box["name"]

        def run(self):
            steps.append(f"read {self.name}")
    monkeypatch.setattr(worker, "MailboxSync", Sync)
    monkeypatch.setattr(worker, "process_files", lambda db: steps.append("process"))

    worker.run_once(db)

    assert steps == ["read Bookings", "process", "read Bills", "process", "process"]
