"""Scraped bills and customs pages: Odoo's records as its forms show them, parent to child."""

import pytest

from fakes import FakeOdoo, mailbox, signed_in


class Odoo(FakeOdoo):
    url = "http://odoo.test"

    def _out(self, model, row_id, fields):
        out = super()._out(model, row_id, fields)
        for f in ("batch_id", "entry_id"):  # many2one, as Odoo returns it: [id, name]
            if out.get(f):
                out[f] = [out[f], self.rows("cbp7501.audit.batch" if f == "batch_id" else "cbp7501.entry")
                          [out[f]].get("name")]
        return out

    def call(self, model, method, *args, **kwargs):
        if method == "search_read":
            return self.search_read(model, args[0], kwargs["fields"])
        return super().call(model, method, *args, **kwargs)


@pytest.fixture
def odoo(monkeypatch):
    fake = Odoo()
    monkeypatch.setattr("app.odoo.client.Odoo.from_store", lambda db: fake)
    monkeypatch.setattr("app.web.booking_compare._fields_cache", {})
    return fake


def fed(db, purpose, odoo_id, subject):
    """An email of a ``purpose`` mailbox whose file went into Odoo record ``odoo_id``."""
    box = mailbox(db, name=purpose, purpose=purpose)
    with db:
        message_id = db.execute("INSERT INTO message (mailbox_id, dedupe_key, subject, state) VALUES (?, ?, ?, 'done')",
                                (box["id"], subject, subject)).lastrowid
        db.execute("INSERT INTO file (message_id, sha256, path, state, odoo_id) VALUES (?, ?, 'x', 'done', ?)",
                   (message_id, subject, odoo_id))


def test_scraped_bill_page_shows_the_form(client, db, odoo):  # noqa: F811
    bill = odoo.add("b2b.ocr.scraped.bill", name="TIMAC-260805", vendor_name="ETERNAL MACHINE TOOL", state="ready_for_po",
                    amount_total=10169.41, match_notes="No draft bill or purchase order of this vendor fits.")
    odoo.add("b2b.ocr.scraped.bill", name="NOT-OURS", state="waiting")
    fed(db, "bill_matching", bill, "Invoice TIMAC-260805")
    signed_in(client)

    listing = client.get("/scraped-bills").get_data(as_text=True)
    assert "TIMAC-260805" in listing and "NOT-OURS" not in listing and "from 1 email" in listing

    page = client.get(f"/scraped-bills/{bill}").get_data(as_text=True)
    for shown in ("ETERNAL MACHINE TOOL", "Vendor", "Invoice", "Match", "10,169.41",
                  "No draft bill or purchase order", "Invoice TIMAC-260805",
                  f"/web#model=b2b.ocr.scraped.bill&amp;id={bill}"):
        assert shown in page


def test_customs_goes_from_batch_to_entry_to_findings(client, db, odoo):  # noqa: F811
    batch = odoo.add("cbp7501.audit.batch", name="Customs Audit 2026-10-28 (email)", status="draft", entry_count=1)
    entry = odoo.add("cbp7501.entry", name="040-0496733-9", entry_number="040-0496733-9", batch_id=batch,
                     file_name="HLCUCA6260809377.pdf", total_duty=15094.06, status="review_required")
    odoo.add("cbp7501.finding", entry_id=entry, rule_id="CBILL-001", message="Select the customs vendor bill.",
             severity="review")
    odoo.add("cbp7501.audit.batch", name="Uploaded by hand", status="pass")
    fed(db, "customs_entry", entry, "RE: ISF FILING")
    signed_in(client)

    listing = client.get("/customs").get_data(as_text=True)
    assert "Customs Audit 2026-10-28 (email)" in listing and "Uploaded by hand" not in listing

    batch_page = client.get(f"/customs/batches/{batch}").get_data(as_text=True)
    assert "Customs Entries" in batch_page and f"/customs/entries/{entry}" in batch_page

    entry_page = client.get(f"/customs/entries/{entry}").get_data(as_text=True)
    for shown in ("040-0496733-9", "HLCUCA6260809377.pdf", "Duties &amp; Fees", "15,094.06", "Findings", "CBILL-001",
                  "Select the customs vendor bill.", "Customs Audit 2026-10-28 (email)", "RE: ISF FILING"):
        assert shown in entry_page


def test_record_deleted_in_odoo_says_so(client, db, odoo):  # noqa: F811
    fed(db, "bill_matching", 999, "Invoice gone")
    signed_in(client)
    assert "no longer in Odoo" in client.get("/scraped-bills/999").get_data(as_text=True)


def test_odoo_lists_ask_odoo_only_for_the_page_shown(client, db, monkeypatch):
    from app.web import odoo_forms
    signed_in(client)
    asked = []
    monkeypatch.setattr("app.web.odoo_forms.record_ids", lambda db, purpose: set(range(1, 201)))
    monkeypatch.setattr("app.web.odoo_forms.email_counts", lambda db, purpose: {})
    monkeypatch.setattr("app.web.odoo_records._odoo_or_error", lambda: (object(), None))
    monkeypatch.setattr(odoo_forms, "list_rows", lambda odoo, model, ids, columns: (
        asked.append(sorted(ids)) or ([("name", "Name")], [{"id": i, "raw": {}, "name": f"B{i}"} for i in sorted(ids, reverse=True)])))
    page = client.get("/scraped-bills?offset=80").get_data(as_text=True)
    assert asked == [list(range(41, 121))] and 'value="81-160"' in page and "/ 200" in page


def test_odoo_lists_group_every_record_and_read_the_open_group_only(client, db, odoo, monkeypatch):  # noqa: F811
    for n in range(3):
        fed(db, "bill_matching", odoo.add("b2b.ocr.scraped.bill", name=f"WAIT-{n}", state="waiting"), f"Invoice W{n}")
    fed(db, "bill_matching", odoo.add("b2b.ocr.scraped.bill", name="READY-1", state="ready_for_po"), "Invoice R1")
    fields_get = odoo.call
    monkeypatch.setattr(odoo, "call", lambda model, method, *args, **kwargs: {
        f: dict(info, type="selection" if f == "state" else "char")
        for f, info in fields_get(model, method, *args, **kwargs).items()} if method == "fields_get"
        else fields_get(model, method, *args, **kwargs))
    signed_in(client)
    grouped = client.get("/scraped-bills?groupby=state").get_data(as_text=True)
    assert '<span class="clip">waiting</span><span class="n">3</span>' in grouped   # every bill, not one page
    assert '<span class="clip">ready_for_po</span><span class="n">1</span>' in grouped
    assert "WAIT-0" not in grouped and 'class="o-pager"' not in grouped           # folded until opened
    opened = client.get("/scraped-bills?groupby=state&group=waiting").get_data(as_text=True)
    assert all(f"WAIT-{n}" in opened for n in range(3)) and "READY-1" not in opened
