"""Freight Payables' data set: the emails its scanner stored. Export only."""

from app.shared.data import Column
from app.shared.data.sqlite import QueryDataSet


def _query(ctx, filters):
    from app.payables.pages import EMAIL_SQL, _where
    where, args = _where((filters.get("q") or "").strip())
    return f"{EMAIL_SQL}{where} ORDER BY m.received DESC, m.id DESC", args


EMAILS = QueryDataSet(
    "payables-emails", "Freight Payables emails", "email",
    (Column("id", "ID", "int", importable=False, default=True), Column("mailbox", "Mailbox", importable=False, default=True),
     Column("subject", "Subject", importable=False, default=True), Column("sender", "Sender", importable=False, default=True),
     Column("received", "Received (UTC)", "datetime", importable=False, default=True),
     Column("state", "State", importable=False), Column("files", "Files", "int", importable=False, default=True),
     Column("created_at", "Saved by the service", "datetime", importable=False)),
    _query, "web.payables_emails", show=lambda row: dict(row, mailbox=row["mailbox_name"]))

DATASETS = (EMAILS,)
