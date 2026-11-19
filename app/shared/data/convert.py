"""Cell values, to and from the types a record holds. A cell that cannot be read raises ValueError with what to fix, in
words a person can act on (it is shown against the row)."""

import re
from datetime import date, datetime

TRUE = {"1", "true", "yes", "y", "on", "t", "x"}
FALSE = {"0", "false", "no", "n", "off", "f"}


def is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def to_text(value):
    """A cell as text. A number Excel stored for what is really an identifier (a booking number 94659872) stays whole."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    text = str(value).strip()
    # An export prefixes a formula-looking text with an apostrophe (see tables.safe): that is not part of the value.
    return text[1:] if text[:1] == "'" and text[1:2] in ("=", "+", "-", "@") else text


def to_bool(value):
    if isinstance(value, bool):
        return value
    text = to_text(value).lower()
    if text in TRUE:
        return True
    if text in FALSE:
        return False
    raise ValueError("write Yes or No")


def to_int(value):
    if isinstance(value, bool):
        raise ValueError("write a whole number")
    try:
        number = float(to_text(value).replace(",", "").replace(" ", ""))
    except ValueError:
        raise ValueError("write a whole number") from None
    if not number.is_integer():
        raise ValueError("write a whole number")
    return int(number)


def to_float(value):
    if isinstance(value, bool):
        raise ValueError("write a number")
    try:
        return float(to_text(value).replace(",", "").replace(" ", ""))
    except ValueError:
        raise ValueError("write a number, such as 4900 or 4900.50") from None


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")


def to_date(value):
    """YYYY-MM-DD. A date typed as text must be written year first (day first and month first are ambiguous)."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = to_text(value)[:10]
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise ValueError("write the date as YYYY-MM-DD")


def to_datetime(value):
    """YYYY-MM-DD HH:MM:SS, as stored."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return f"{value.isoformat()} 00:00:00"
    text = to_text(value)
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2})(?::(\d{2}))?)?", text)
    if not match:
        raise ValueError("write the time as YYYY-MM-DD HH:MM:SS")
    return f"{match.group(1)} {match.group(2) or '00:00'}:{match.group(3) or '00'}"


def to_choice(value, options):
    """The key of a choice, given its key or its label (either case). options: ((key, label), ...)."""
    text = to_text(value)
    for key, label in options:
        if text == str(key) or text.lower() == str(label).lower() or text.lower() == str(key).lower():
            return key
    raise ValueError("choose one of: " + ", ".join(str(label) for _key, label in options))
