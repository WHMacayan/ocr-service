"""Freight Payables: the scanner's freight invoices and freight audit, as Excel workbooks.

Its own mailbox purpose, worker and tables (payables_*). Nothing here is ever written to Odoo,
and the main worker never reads a Freight Payables mailbox.
"""

from app.shared.jobs import Job

PURPOSE = "freight_payables"


def _commands(cli):
    from app.payables.commands import register
    register(cli)


JOB = Job(
    purpose=PURPOSE, label="Freight Payables", own_worker=True, commands=_commands, pages="app.payables.pages",
    datasets="app.payables.data",
    # Parked: hidden from the menu, its pages and worker kept. To show it again:
    # menu=(MenuItem("Freight Payables", "web.payables_emails", "/payables"),),
)
