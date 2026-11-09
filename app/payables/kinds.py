"""Which attachments Freight Payables keeps, judged by their bytes rather than their names
(a freight invoice named .xls can really be an .xlsx).

The booking flow keeps PDFs and images only; the audit also needs Excel statements, zipped
invoices, forwarded emails and screenshots pasted into the email.
"""

import io
import zipfile

from ..mail.ingest import is_supported_file

# Smaller inline pictures are signatures and logos (the scanner's MIN_IMAGE_BYTES).
MIN_INLINE_IMAGE_BYTES = 20_000
MAX_FILE_BYTES = 40 * 1024 * 1024

_ZIP = b"PK\x03\x04"
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # .xls, .msg, .doc, .ppt


def file_kind(raw, name):
    """pdf, image, xlsx, xls, zip, eml or msg; None for anything Freight Payables does not keep."""
    name = (name or "").lower()
    if is_supported_file(raw):
        return "pdf" if raw.startswith(b"%PDF-") else "image"
    if raw.startswith(_ZIP):
        try:
            members = zipfile.ZipFile(io.BytesIO(raw)).namelist()
        except zipfile.BadZipFile:
            return None
        if any(member.startswith("xl/") for member in members):
            return "xlsx"
        if any(member.startswith(("word/", "ppt/")) for member in members):
            return None
        return "zip"
    if raw.startswith(_OLE):
        if name.endswith(".msg"):
            return "msg"
        if name.endswith((".doc", ".ppt", ".pps")):
            return None
        return "xls"
    if name.endswith(".eml"):
        return "eml"
    return None
