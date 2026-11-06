"""Reading text and shipment identifiers. Port of b2b_freight_ocr/lib/text.py."""

import io
import logging
import re
from datetime import date, datetime

import pdfplumber
from lxml import etree, html as lxml_html

_logger = logging.getLogger(__name__)

# Copied from b2b_customization/tools/shipment_reference.py: placeholders such
# as N/A that stand where a reference has not been issued.
_PLACEHOLDER_RE = re.compile("|".join((
    r"^N/?A$", r"^NA$", r"^NO\s+MBL\s+YET$", r"^NO\s+MBL$", r"^NOT\s+YET\s+AVAILABLE$",
    r"^NOT\s+AVAILABLE$", r"^DDP$")), re.IGNORECASE)
_CONTAINER_RE = re.compile(r"^[A-Z]{4}\d{7}$")
_CONTAINER_IN_TEXT_RE = re.compile(r"\b([A-Z]{4})[\s-]?(\d{7})\b", re.IGNORECASE)
_MASTER_BL_RE = re.compile(
    r"(?:MASTER\s*B/?L|MBL|MBOL)\s*(?:NO\.?|NUMBER|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9-]{5,19})\b", re.I)
_HOUSE_BL_RE = re.compile(
    r"(?:HOUSE\s*B/?L|HBL|HBOL)\s*(?:NO\.?|NUMBER|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9-]{5,19})\b", re.I)
# A carrier's single ocean bill printed as "BL/SWB No(s).: HLCU..."; PDF table
# borders come through as "|".
_SWB_RE = re.compile(
    r"BL\s*/\s*SWB\s*(?:\(NO\)S?|NO\(S\)|NOS?|NUMBER|#)\.?[\s:|\-]*([A-Z0-9][A-Z0-9-]{5,19})\b", re.I)
_BOOKING_LABEL_RE = re.compile(
    r"\b(?:BC|BKG|BK|BOOKING(?:\s+(?:NO|NUMBER|CONFIRMATION))?|HL)[\s:#.-]{1,20}([A-Z]{0,6}\d{6,12})\b",
    re.I)
# "MBOL COSU6506044390" names a bill of lading, not a booking.
_BL_CONTEXT_RE = re.compile(
    r"(?:M?B/?L|HB/?L|MBOL|HBOL|BILL\s+OF\s+LADING|WAYBILL|AWB)\s*(?:NO\.?|NUMBER|#)?\s*[:\-]?\s*$", re.I)
_QUOTED_REPLY_RE = re.compile(
    r"^[\s*_>]*(?:From\s*:|发件人|寄件者|-{3,}\s*Original"
    r"|On [^\n]{3,160}(?:\n[^\n]{0,160})?wrote:|Sent from|获取 Outlook)", re.I | re.M)
_CID_RE = re.compile(r"\(cid:\d+\)")


def normalize_identifier(value):
    """Upper-case alphanumerics only; placeholders such as N/A read as ''."""
    text = str(value or "").strip()
    if not text or _PLACEHOLDER_RE.fullmatch(text.upper()):
        return ""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def split_list(value):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def is_container_number(value):
    return bool(_CONTAINER_RE.fullmatch(normalize_identifier(value)))


def find_container_numbers(text):
    found = []
    for prefix, digits in _CONTAINER_IN_TEXT_RE.findall(text or ""):
        number = f"{prefix}{digits}".upper()
        if number not in found:
            found.append(number)
    return found


def _first_bl(pattern, text):
    for match in pattern.finditer(text or ""):
        value = normalize_identifier(match.group(1))
        # Every B/L number has digits, and a container number is not a B/L.
        if re.search(r"\d", value) and not is_container_number(value):
            return value
    return ""


def find_master_bl(text):
    return _first_bl(_MASTER_BL_RE, text) or _first_bl(_SWB_RE, text)


def find_house_bl(text):
    return _first_bl(_HOUSE_BL_RE, text)


def find_labelled_booking_number(text):
    for match in _BOOKING_LABEL_RE.finditer(str(text or "")):
        value = normalize_identifier(match.group(1))
        if value and not is_container_number(value):
            return value
    return ""


def booking_number_in_subject(subject, pattern):
    """A labelled booking number, else the first match of the mailbox's
    booking number pattern that is neither a container nor a B/L."""
    labelled = find_labelled_booking_number(subject)
    if labelled or not pattern:
        return labelled
    upper = str(subject or "").upper()
    for match in re.finditer(pattern, upper):
        value = normalize_identifier(match.group(0))
        if value and not is_container_number(value) and not _BL_CONTEXT_RE.search(upper[:match.start()]):
            return value
    return ""


def parse_date(value):
    """``YYYY-MM-DD`` or ''; any other layout has more than one reading."""
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def parse_month_date(text, reference):
    """A day and month name ("18/OCT", "OCT 9") as ``YYYY-MM-DD``, taking the
    year nearest ``reference``; '' otherwise."""
    value = (text or "").strip().upper()
    match = re.match(r"^(\d{1,2})\s*[/ .-]?\s*([A-Z]{3,9})\.?$", value)
    if match:
        day, month_name = match.groups()
    else:
        match = re.match(r"^([A-Z]{3,9})\.?\s*[/ .-]?\s*(\d{1,2})$", value)
        if not match:
            return ""
        month_name, day = match.groups()
    months = {date(2000, m, 1).strftime("%b").upper(): m for m in range(1, 13)}
    month = months.get(month_name[:3])
    if not month or not reference:
        return ""
    candidates = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(date(year, month, int(day)))
        except ValueError:
            pass
    return min(candidates, key=lambda d: abs((d - reference).days)).isoformat() if candidates else ""


def date_appears_in(text, iso_date):
    """Whether ``text`` prints the date in a common layout (16Oct26, 10/16/2026...)."""
    iso = parse_date(iso_date)
    if not iso:
        return False
    day = datetime.strptime(iso, "%Y-%m-%d")
    haystack = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    yyyy, yy = day.strftime("%Y"), day.strftime("%y")
    forms = {yyyy + day.strftime("%m%d")}
    for d in (day.strftime("%d"), str(day.day)):
        for m in (day.strftime("%m"), str(day.month)):
            forms |= {d + m + yyyy, m + d + yyyy, d + m + yy, m + d + yy}
        for name in (day.strftime("%b").upper(), day.strftime("%B").upper()):
            forms |= {d + name + yyyy, d + name + yy, name + d + yyyy, name + d + yy}
    return any(form in haystack for form in forms)


def appears_in(text, value):
    """Whether an identifier is printed in ``text``, ignoring separators."""
    key = normalize_identifier(value)
    return bool(key) and key in normalize_identifier(text)


def newest_message_text(body_html):
    """The email's newest message only: quoted history repeats other mail."""
    if not body_html:
        return ""
    try:
        root = lxml_html.fromstring(body_html)
        for quoted in root.xpath("//blockquote | //div[contains(@class, 'gmail_quote')]"):
            if quoted.getparent() is not None:
                quoted.drop_tree()
        for br in root.xpath("//br"):
            br.tail = "\n" + (br.tail or "")
        for block in root.xpath("//p | //div | //tr | //li"):
            block.tail = "\n" + (block.tail or "")
        text = root.text_content()
    except (etree.ParserError, ValueError):
        text = body_html
    text = "\n".join(line.strip() for line in text.splitlines())
    match = _QUOTED_REPLY_RE.search(text)
    return re.sub(r"\n{3,}", "\n\n", text[:match.start()] if match else text).strip()


def file_text(raw, max_pages):
    """Text of a PDF or image: the text layer first, OCR when there is none.

    Raises RuntimeError when OCR is needed but tesseract/poppler is missing,
    so the file is retried rather than judged unreadable.
    """
    text = ""
    if raw.startswith(b"%PDF-"):
        try:
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages[:max_pages]).strip()
        except Exception as exc:  # a damaged PDF is a document problem
            _logger.warning("PDF text failed: %s", exc)
    # pdfplumber writes "(cid:N)" for glyphs of fonts it cannot map.
    if text and sum(map(len, _CID_RE.findall(text))) / len(text) < 0.3:
        return text
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
        from PIL import Image
        pages = (convert_from_bytes(raw, last_page=max_pages) if raw.startswith(b"%PDF-")
                 else [Image.open(io.BytesIO(raw))])
        return "\n".join(pytesseract.image_to_string(page) for page in pages).strip()
    except ImportError as exc:
        raise RuntimeError(f"OCR libraries are not installed: {exc}") from exc
    except Exception as exc:
        if type(exc).__name__ in ("TesseractNotFoundError", "PDFInfoNotInstalledError"):
            raise RuntimeError(f"OCR programs are not installed: {exc}") from exc
        _logger.warning("OCR failed: %s", exc)
        return ""
