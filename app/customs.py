"""Customs entry summaries: upload each CBP 7501 or 3461 PDF to the customs audit
module (b2b_cbp_7501_audit_odoo) as an entry in the day's batch. Nothing more:
the entry is not parsed or audited here, a person does that in Odoo.

No AI: the form is recognised by what it prints, so the broker's invoices,
arrival notices and statements sent alongside are skipped.
"""

import base64
import json
import re

from .text import file_text

BATCH = "cbp7501.audit.batch"
ENTRY = "cbp7501.entry"

# (form, title printed on it, its OMB number or form footer)
FORMS = (("7501", "ENTRY SUMMARY", ("1651-0022", "CBP FORM 7501")),
         ("3461", "IMMEDIATE DELIVERY", ("1651-0024", "CBP FORM 3461")))
_ENTRY_NUMBER = re.compile(r"\b([A-Z0-9]{3}-\d{7}-\d)\b")


def form_of(text):
    """``"7501"`` or ``"3461"`` when the text is that CBP form, else None."""
    flat = " ".join((text or "").upper().split())
    for form, title, marks in FORMS:
        if title in flat and any(mark in flat for mark in marks):
            return form
    return None


def entry_number(text):
    """The entry number (filer code-number-check digit) the form prints first."""
    match = _ENTRY_NUMBER.search((text or "").upper())
    return match.group(1) if match else None


class CustomsUploader:
    def __init__(self, db, odoo, connection_id, max_pages):
        self.db = db
        self.odoo = odoo
        self.connection_id = connection_id
        self.max_pages = max_pages
        self._batches = {}

    def take(self, file, received):
        """``(state, note, odoo_id)`` for one saved file of a customs email."""
        with open(file["path"], "rb") as handle:
            raw = handle.read()
        text = file_text(raw, self.max_pages)
        form = form_of(text)
        if not form:
            return "skipped", "Not a CBP 7501 or 3461.", None
        number = entry_number(text)
        values = {"form": form, "entry_number": number}
        self.db.execute("UPDATE file SET doc_class = 'customs_entry', extracted = ? WHERE id = ?",
                        (json.dumps(values), file["id"]))
        # The broker resends the same entry as a new PDF (a paperless copy): one entry per number.
        if number and self.db.execute(
                "SELECT 1 FROM file f JOIN file_target t ON t.file_id = f.id AND t.connection_id = ? "
                "WHERE f.doc_class = 'customs_entry' AND t.state = 'done' AND f.id != ? "
                "AND json_extract(f.extracted, '$.entry_number') = ? LIMIT 1",
                (self.connection_id, file["id"], number)).fetchone():
            return "skipped", f"Entry {number} is already in the customs audit.", None
        entry_id = self.odoo.create(ENTRY, {"batch_id": self.batch((received or "")[:10]),
                                            "file_name": file["filename"],
                                            "pdf_file": base64.b64encode(raw).decode()})
        return "done", f"CBP {form} entry {number or '(number not read)'} uploaded.", entry_id

    def batch(self, day):
        """The day's batch of emailed entry summaries, created on first use."""
        name = f"Customs Audit {day} (email)"
        if name not in self._batches:
            found = self.odoo.call(BATCH, "search", [("name", "=", name)], limit=1)
            self._batches[name] = found[0] if found else self.odoo.create(BATCH, {"name": name})
        return self._batches[name]
