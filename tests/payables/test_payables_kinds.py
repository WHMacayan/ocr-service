"""Which attachments Freight Payables keeps, by their bytes, not their names."""

import io
import zipfile

import pytest

from app.payables.kinds import file_kind
from fakes import PDF

OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64


def zipped(*members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for member in members:
            archive.writestr(member, b"x")
    return buffer.getvalue()


@pytest.mark.parametrize("raw, name, kind", [
    (PDF, "invoice.pdf", "pdf"),
    (zipped("[Content_Types].xml", "xl/workbook.xml"), "soa.xlsx", "xlsx"),
    (zipped("[Content_Types].xml", "xl/workbook.xml"), "277046688_FREIGHT INVOICE.xls", "xlsx"),
    (zipped("invoices/a.pdf"), "invoices.zip", "zip"),
    (zipped("[Content_Types].xml", "word/document.xml"), "letter.docx", None),
    (OLE, "rates.xls", "xls"),
    (OLE, "FW invoice.msg", "msg"),
    (OLE, "letter.doc", None),
    (b"From: a@b.com\r\nSubject: invoice\r\n\r\nhi", "FW invoice.eml", "eml"),
    (b"plain text", "notes.txt", None),
])
def test_kind_recognises_each_type(raw, name, kind):
    assert file_kind(raw, name) == kind
