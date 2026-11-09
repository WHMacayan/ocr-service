"""Freight Payables is a fourth mailbox purpose."""

from app.core import store
from app.web import forms
from fakes import signed_in


def test_purpose_listed():
    assert ("freight_payables", "Freight Payables") in forms.PURPOSES
    assert [key for key, _label in forms.PURPOSES][:3] == ["freight_booking", "bill_matching", "customs_entry"]


def test_payables_mailbox_saved(client, db):
    token = signed_in(client)
    client.post("/mailboxes/new", data={"csrf": token, "name": "Freight Payables", "active": "on",
                                        "purpose": "freight_payables", "company_id": "1",
                                        "start_from": "2026-11-03"})
    assert store.mailbox(db, 1)["purpose"] == "freight_payables"
