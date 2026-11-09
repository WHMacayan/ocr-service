"""The Freight Payables worker: its own loop, its own mailboxes, no Odoo."""

from app.payables import worker as payables_worker

from fakes import mailbox


def stub_sync(steps):
    class Sync:
        def __init__(self, db, graph, box):
            self.name = box["name"]

        def run(self):
            steps.append(f"read {self.name}")
    return Sync


def test_payables_worker_reads_only_payables(db):
    mailbox(db, name="Bookings")
    mailbox(db, name="Bills", purpose="bill_matching")
    mailbox(db, name="Payables", purpose="freight_payables")
    steps = []

    payables_worker.run_payables_once(db, sync_class=stub_sync(steps))

    assert steps == ["read Payables"]


def test_payables_worker_needs_no_odoo_connection(db):
    mailbox(db, name="Payables", purpose="freight_payables")
    assert db.execute("SELECT COUNT(*) FROM odoo_connection").fetchone()[0] == 0
    steps = []

    payables_worker.run_payables_once(db, sync_class=stub_sync(steps))

    assert steps == ["read Payables"]


def test_one_failing_mailbox_does_not_stop_the_others(db):
    mailbox(db, name="Payables A", purpose="freight_payables")
    mailbox(db, name="Payables B", purpose="freight_payables")
    steps = []

    class Sync:
        def __init__(self, db, graph, box):
            self.name = box["name"]

        def run(self):
            if self.name == "Payables A":
                raise RuntimeError("Outlook down")
            steps.append(f"read {self.name}")

    payables_worker.run_payables_once(db, sync_class=Sync)

    assert steps == ["read Payables B"]


def test_default_reader_is_the_payables_one():
    from app.payables.graph import PayablesGraph
    from app.payables.ingest import PayablesSync
    assert payables_worker._reader() == (PayablesSync, PayablesGraph)


def test_commands_registered(client):
    from app import create_app
    commands = create_app().cli.list_commands(None)
    assert {"payables-worker", "payables-run-once"} <= set(commands)


def test_payables_waits_for_a_busy_database(db):
    # The booking pipeline keeps the write lock while it waits for the AI; 30 s is not enough.
    connection = payables_worker.connect()
    try:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 120_000
    finally:
        connection.close()
