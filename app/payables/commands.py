"""Freight Payables' commands: its own worker, continuously or one pass."""

from app.payables import worker as payables_worker


def register(cli):
    @cli.command("payables-worker")
    def payables_worker_command():
        """Read the due Freight Payables mailboxes forever (no Odoo)."""
        payables_worker.run_payables_forever()

    @cli.command("payables-run-once")
    def payables_run_once_command():
        """One pass over the due Freight Payables mailboxes."""
        db = payables_worker.connect()
        try:
            payables_worker.run_payables_once(db)
        finally:
            db.close()
