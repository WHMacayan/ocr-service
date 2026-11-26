"""What one file states, as a job's Reading asks the AI (each job keeps its own instructions and fields).

A file's text is read once and the AI asked once; only the identifiers and dates the text really prints are kept.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from app.shared.documents import ai
from app.shared.documents.text import (appears_in, date_appears_in, file_text, normalize_identifier,
                                      plausible_identifier, valid_container_number)


@dataclass(frozen=True)
class Reading:
    """What the AI reads a job's attachments for: the setting holding its instructions, the fields it asks for (a JSON
    schema), and how its answer names the document (``classify(values)`` -> doc_class). Each job keeps its own
    (freight_booking/reading.py, scraped_bills/reading.py)."""
    prompt_setting: str
    schema: dict
    classify: Callable


def nullable(kind):
    return {"type": [kind, "null"]}


def _reading(reading):
    """A job's Reading; a mailbox purpose names its job's."""
    if isinstance(reading, str):
        from app.shared.jobs import job_for
        return job_for(reading).reading
    return reading


# The text read again when a full reading's answer overflowed the model's output limit.
SHORT_READ_CHARS = 12000


def reading_version(reading, settings):
    """A short fingerprint of what a reading depends on: the model, the instructions and the fields asked for.
    A stored reading with another fingerprint was made by older instructions and is read again (17 Nov 2026:
    77 certificates of origin read as factory invoices by the earlier prompt stayed in use). ``reading``: a job's
    Reading, or its mailbox purpose."""
    reading = _reading(reading)
    basis = json.dumps([settings.get("openai_model"), settings.get(reading.prompt_setting), reading.schema],
                       sort_keys=True)
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def reading_limits(settings):
    """How much of a file a reading looked at, ``pages/characters``: stored with the reading (file.read_limits)."""
    return f"{int(settings['max_pages'])}/{int(settings['max_chars'])}"


def limits_raised(used, settings):
    """Whether the limits now allow more of a file than the ones its stored reading was made with (review F18): a
    reading cut short by a lower limit is read again once the limit is raised. Lowering a limit does not
    throw readings away, and a reading from before the limits were recorded (``used`` empty) stands."""
    if not used:
        return False
    try:
        pages, chars = (int(part) for part in used.split("/"))
    except ValueError:
        return False
    return int(settings["max_pages"]) > pages or int(settings["max_chars"]) > chars


def read_file(raw, reading, settings):
    """``(doc_class, values)`` of a saved file; ``(None, {})`` when it has no text. ``reading``: a job's Reading, or
    its mailbox purpose.

    ``settings`` maps setting keys to values (see defaults.DEFAULT_SETTINGS).
    """
    reading = _reading(reading)
    text = file_text(raw, int(settings["max_pages"]))
    if not text.strip():
        return None, {}
    ask = lambda chars: ai.extract(  # noqa: E731
        settings["openai_api_key"], settings["openai_model"], settings[reading.prompt_setting],
        text[:chars], reading.schema)
    try:
        parsed = ask(int(settings["max_chars"]))
    except ai.AnswerTooLong:
        # A document listing hundreds of lines (a statement) overflows the answer: its first pages tell what it is.
        parsed = ask(SHORT_READ_CHARS)
    values = checked(parsed, text)
    return reading.classify(values), values


def checked(parsed, text):
    """Identifiers and dates the document does not print are dropped: model
    output is a reading, not a fact."""
    values = {key: value for key, value in parsed.items() if value not in (None, "", [])}
    for key in ("booking_number", "master_bl", "house_bl", "bl_number", "shipment_reference", "invoice_number",
                "vendor_vat"):
        if key in values and not appears_in(text, values[key]):
            values.pop(key)
    for key in ("booking_number", "master_bl", "house_bl", "bl_number"):
        if key in values and not plausible_identifier(values[key]):
            values.pop(key)
    for key in ("containers", "invoices", "po_numbers"):
        if key in values:
            values[key] = [normalize_identifier(v) for v in values[key] if appears_in(text, v)
                           and (key != "containers" or valid_container_number(v))]
    for key in ("etd", "eta", "invoice_date"):
        if key in values and not date_appears_in(text, values[key]):
            values.pop(key)
    return values
