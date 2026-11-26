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
# Every run of spaces and separators between a label and its number is bounded (a long gap is not a label's value)
# and there is only one such run: patterns with several unbounded runs side by side take cubic time on a label
# followed by thousands of spaces, which an email body can hold (review v003, slow patterns).
_BL_GAP = r"(?:[ \t]{0,20}(?:NO\.?|NUMBER))?[\s:#\-]{0,40}"
_MASTER_BL_RE = re.compile(
    r"(?:MASTER\s{0,10}B/?L|MBL|MBOL)" + _BL_GAP + r"([A-Z0-9][A-Z0-9-]{5,19})\b", re.I)
_HOUSE_BL_RE = re.compile(
    r"(?:HOUSE\s{0,10}B/?L|HBL|HBOL)" + _BL_GAP + r"([A-Z0-9][A-Z0-9-]{5,19})\b", re.I)
# A carrier's single ocean bill printed as "BL/SWB No(s).: HLCU..."; PDF table
# borders come through as "|".
_SWB_RE = re.compile(
    r"BL\s{0,5}/\s{0,5}SWB[ \t]{0,20}(?:\(NO\)S?|NO\(S\)|NOS?|NUMBER|#)\.?[\s:|\-]{0,40}([A-Z0-9][A-Z0-9-]{5,19})\b", re.I)
_BOOKING_LABEL_RE = re.compile(
    r"\b(?:BC|BKG|BK|BOOKING(?:\s+(?:NO|NUMBER|CONFIRMATION))?|HL)[\s:#.-]{1,20}([A-Z]{0,6}\d{6,12})\b",
    re.I)
# "MBOL COSU6506044390" names a bill of lading, not a booking.
_BL_CONTEXT_RE = re.compile(
    r"(?:M?B/?L|HB/?L|MBOL|HBOL|BILL\s{1,5}OF\s{1,5}LADING|WAYBILL|AWB)" + _BL_GAP + r"$", re.I)
# The prefix stays on its line: letting it cross newlines made a long run of blank lines take quadratic time.
_QUOTED_REPLY_RE = re.compile(
    r"^[ \t*_>]*(?:From\s*:|发件人|寄件者|-{3,}\s*Original"
    r"|On [^\n]{3,160}(?:\n[^\n]{0,160})?wrote:|Sent from|获取 Outlook)", re.I | re.M)
_CID_RE = re.compile(r"\(cid:\d+\)")


def normalize_identifier(value):
    """Upper-case alphanumerics only; placeholders such as N/A read as ''."""
    text = str(value or "").strip()
    if not text or _PLACEHOLDER_RE.fullmatch(text.upper()):
        return ""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


_MONTH_RE = re.compile(r"-(?:JAN|FEB|MAR|APR|MAY|JUNE?|JULY?|AUG|SEPT?|OCT|NOV|DEC)-", re.I)


def plausible_identifier(value):
    """Whether a value read as a booking or B/L number can be one. Read wrongly in the freight mailbox
    (Nov 2026), each tying dozens of shipments together: a forwarder's NVOCC licence (IT1841NVO on every
    Tianshi FCR), a phone number, an email, a carrier code alone (HDMU), a customer reference
    (LAX19-SEPT-31ARS), a vessel and voyage (KOTA RAJIN V.0361S). Real numbers have 4 digits at least
    (Hapag HLCUBKK2608ARQMO has 4)."""
    text = str(value or "").strip()
    if not text or "@" in text or text.startswith("+") or _MONTH_RE.search(text):
        return False
    if len(re.findall(r"\d", text)) < 4 or normalize_identifier(text).endswith("NVO"):
        return False
    return len(re.findall(r"(?<![A-Z0-9])[A-Z]{2,}(?![A-Z0-9.])", text.upper())) < 2


def split_list(value):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def is_container_number(value):
    return bool(_CONTAINER_RE.fullmatch(normalize_identifier(value)))


_ISO6346 = {}
_v = 10
for _ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _v += _v % 11 == 0  # the letter values skip multiples of 11
    _ISO6346[_ch] = _v
    _v += 1


def valid_container_number(value):
    """A container number whose last digit is its ISO 6346 check digit. Rejects misreadings and template
    numbers (HLXU1234567); 6,579 of the 6,590 containers on the team's freight POs pass (Odoo 1111, Nov 2026)."""
    number = normalize_identifier(value)
    if not _CONTAINER_RE.fullmatch(number):
        return False
    total = sum((_ISO6346[c] if c.isalpha() else int(c)) << i for i, c in enumerate(number[:10]))
    return total % 11 % 10 == int(number[10])


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


# What OCR says about a file that is simply not a readable document: judged unreadable, not retried. Any other
# OCR failure (memory, a timeout, a program that crashed) may pass, so the file is retried instead of being
# skipped for good as having no text (review F09).
_DOCUMENT_PROBLEMS = {"PDFPageCountError", "PDFSyntaxError", "UnidentifiedImageError", "DecompressionBombError",
                      "DecompressionBombWarning"}


def _readable(text):
    """A page whose text layer says something: some text, and not mostly "(cid:N)" for glyphs it could not map."""
    text = (text or "").strip()
    return bool(text) and sum(map(len, _CID_RE.findall(text))) / len(text) < 0.3


def _pdf_pages(raw, max_pages):
    """The text layer of each page read (up to ``max_pages``), or None when the PDF cannot be opened."""
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            return [page.extract_text() or "" for page in pdf.pages[:max_pages]]
    except Exception as exc:  # a damaged PDF is a document problem
        _logger.warning("PDF text failed: %s", exc)
        return None


def _ocr(raw, pdf_pages=None):
    """OCR text of an image, or of the given 1-based pages of a PDF (all up to ``max`` when None)."""
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
        from PIL import Image
        if raw.startswith(b"%PDF-"):
            images = [image for number in pdf_pages for image in convert_from_bytes(raw, first_page=number,
                                                                                    last_page=number)]
        else:
            images = [Image.open(io.BytesIO(raw))]
        return [pytesseract.image_to_string(image).strip() for image in images]
    except ImportError as exc:
        raise RuntimeError(f"OCR libraries are not installed: {exc}") from exc
    except Exception as exc:
        if type(exc).__name__ in ("TesseractNotFoundError", "PDFInfoNotInstalledError"):
            raise RuntimeError(f"OCR programs are not installed: {exc}") from exc
        if type(exc).__name__ in _DOCUMENT_PROBLEMS:
            _logger.warning("OCR cannot read the file: %s", exc)
            return [""] * (len(pdf_pages) if pdf_pages is not None else 1)
        raise RuntimeError(f"OCR failed, the file will be tried again: {exc}") from exc


def file_text(raw, max_pages):
    """Text of a PDF or image, page by page: a page's text layer when it has one, OCR for a page that has none
    (a scan inside a PDF whose other pages are text, review F09).

    Raises RuntimeError when OCR is needed but tesseract/poppler is missing or failed in a way that may pass,
    so the file is retried rather than judged unreadable.
    """
    if not raw.startswith(b"%PDF-"):
        return "\n".join(_ocr(raw)).strip()
    pages = _pdf_pages(raw, max_pages)
    if pages is None:  # not openable as text: OCR what poppler can render
        return "\n".join(_ocr(raw, list(range(1, max_pages + 1)))).strip()
    missing = [number for number, text in enumerate(pages, 1) if not _readable(text)]
    if missing:
        for number, text in zip(missing, _ocr(raw, missing)):
            pages[number - 1] = text
    return "\n".join(pages).strip()
