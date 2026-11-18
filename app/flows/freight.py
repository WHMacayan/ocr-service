"""Freight Booking: one document consolidated into its b2b.ocr.booking in Odoo.

Port of b2b_freight_ocr's booking rules. A value fills an empty field; a
different value is kept as an open Conflict for a person; a field a person
confirmed is never touched. Purchase history is written as suggestions, except the
team's own freight PO for the shipment, which is linked (PO Matched); drafting a PO
is a button in Odoo.
"""

import json
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta

from ..documents.text import (find_labelled_booking_number, normalize_identifier, parse_date, plausible_identifier,
                              valid_container_number)

_logger = logging.getLogger(__name__)

BOOKING = "b2b.ocr.booking"
CONTAINER = "b2b.ocr.booking.container"
INVOICE = "b2b.ocr.booking.invoice"
BL = "b2b.ocr.booking.bl"
CONFLICT = "b2b.ocr.booking.conflict"
CHANGE = "b2b.ocr.booking.container.change"

_SCHEDULE = ("booking_confirmation", "mbl_hbl", "shipping_notice")
# The factory's own papers. They start a booking and fill its shipment block, but never dispute a value
# already held: the carrier's and forwarder's documents are the source of the schedule.
FACTORY_DOCS = ("commercial_invoice", "packing_list")
# Which document types state which values. Every type prints other parties'
# details, so a value is taken only from a type whose job is to state it.
TRUSTED = {
    "schedule": _SCHEDULE,
    "factory_schedule": FACTORY_DOCS,
    "email_schedule": ("email_message",),
    "bl": _SCHEDULE + ("freight_invoice", "email_message"),
    "forwarder": _SCHEDULE + ("freight_invoice",),
    "factory": ("commercial_invoice", "packing_list", "mbl_hbl"),
    "containers": _SCHEDULE + FACTORY_DOCS + ("freight_invoice", "email_message"),
    # A packing list prints the order number where an invoice number would be: never an invoice.
    "invoices": ("commercial_invoice", "email_message"),
    "amounts": ("freight_invoice",),
    # Fills a booking that started without a number (from a factory invoice); never replaces one.
    "booking_number": _SCHEDULE + FACTORY_DOCS + ("freight_invoice",),
    "reference": FACTORY_DOCS,
}
# A booking still open for documents: no freight PO drafted or linked yet. A container is reused
# shipment after shipment, and a sailing carries many shipments: only open bookings are matched by them.
OPEN = [("purchase_order_id", "=", False), ("state", "not in", ["po_created", "po_matched"])]
# A newer booking confirmation replaces these: carriers reissue them as the schedule moves.
UPDATED_BY_CONFIRMATION = ("vessel", "voyage", "etd", "eta")
SCHEDULE_FIELDS = ("vessel", "voyage", "port_of_loading", "port_of_discharge", "etd", "eta")
_READ = (
    "booking_number", "forwarder_id", "forwarder_name", "factory_id", "factory_name", "master_bl",
    "master_bl_confirmed", "master_bl_assumed", "house_bl", "house_bl_confirmed", "house_bl_assumed",
    *SCHEDULE_FIELDS, *(f"{name}_source" for name in UPDATED_BY_CONFIRMATION),
    "freight_currency_id", "freight_cost", "additional_charges", "freight_mode",
    "freight_product_id", "mapping_freight_product_id", "purchase_order_id", "warehouse_id", "shipment_reference",
)


def m2o_id(value):
    """The id of a many2one as XML-RPC returns it ([id, name] or False)."""
    return value[0] if value else False


# ── the same value, written another way ──────────────────────────────

# Ocean carriers. Their own documents name the carrier where a forwarder would be: never a dispute.
CARRIER = re.compile(r"HAPAG|MAERSK|\bMSC\b|MEDITERRANEAN SHIPPING|CMA[\s-]*CGM|COSCO|OCEAN NETWORK EXPRESS|\bONE\b"
                     r"|EVERGREEN|YANG MING|\bHMM\b|HYUNDAI MERCHANT|\bZIM\b|WAN HAI|\bOOCL\b|ORIENT OVERSEAS"
                     r"|\bPIL\b|PACIFIC INTERNATIONAL LINES|\bSEALAND\b", re.IGNORECASE)
# Carrier codes that start bills and bookings: ONEYSELGE0071900 is booking SELGE0071900.
_SCAC = re.compile(r"^(ONEY|MEDU|MSCU|HLCU|HDMU|MAEU|COSU|CMDU|EGLV|OOLU|YMLU|YMJA|ZIMU|WHLC|EMIV|SMLM|KMTC|SUDU|APLU)")
# Words a company, port or vessel name carries that say nothing about which one it is.
_LEGAL = {"CO", "COMPANY", "CORP", "CORPORATION", "LTD", "LIMITED", "LLC", "INC", "PTE", "PVT", "SA", "AG", "GMBH",
          "GROUP", "THE", "AND", "OF", "INTERNATIONAL", "TNHH", "CONG", "TY", "CTY", "MTV", "JSC"}
_PLACES = {"US", "USA", "UNITED", "STATES", "AMERICA", "VN", "VIET", "NAM", "VIETNAM", "CN", "CHINA", "TH",
           "THAILAND", "MA", "MOROCCO", "MX", "MEXICO", "CA", "CANADA", "KR", "KOREA", "TW", "TAIWAN", "IN", "INDIA",
           "PK", "PAKISTAN", "SG", "SINGAPORE", "MY", "MALAYSIA", "ID", "INDONESIA", "COUNTRY"}
_US_STATES = {"AL", "AK", "AZ", "AR", "CO", "CT", "DE", "FL", "GA", "HI", "IA", "IL", "KS", "KY", "LA", "MD", "ME",
              "MI", "MN", "MO", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH", "OK", "OR", "PA",
              "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY"}
_OPERATORS = {"MAERSK", "HAPAG", "LLOYD", "MSC", "CMA", "CGM", "COSCO", "MV", "M", "V", "VESSEL"}


def _words(text):
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.findall(r"[A-Z0-9]+", plain.upper())


# One port under two names. Sentury Morocco's invoices print TANGIER for the container port Tanger Med;
# a bare UN/LOCODE stands for its port.
_PORT_ALIASES = {"TANGIER": "TANGERMED", "TANGER": "TANGERMED", "TANGIERMED": "TANGERMED", "MAPTM": "TANGERMED",
                 "USPEF": "EVERGLADES", "SAIGON": "HOCHIMINH"}
# Neighbouring ports stay apart on purpose (Los Angeles / Long Beach, Ho Chi Minh City / Vung Tau): the terminal and
# the trucking differ, so a document naming the other one is a disagreement for a person.
# The factories' invoice numbers (Sentury F26AR10THA0693 / F26ARS09THA180, General Rubber GRKA-P-2026436 written
# without dashes): read where a B/L goes, never a B/L.
_INVOICE_NUMBER = re.compile(r"^(F\d{2}ARS?\d{2}[A-Z]{2,3}\d{3,4}|GRKA[A-Z]{0,2}\d{7})")
# UN/LOCODEs printed alone where a port goes (an arrival notice's THLCH, USDAL): the city they stand for.
_LOCODES = {"THLCH": "LAEMCHABANG", "THBKK": "BANGKOK", "VNSGN": "HOCHIMINH", "VNVUT": "VUNGTAU",
            "VNCMT": "CAIMEP", "VNHPH": "HAIPHONG", "CNQDG": "QINGDAO", "CNTAO": "QINGDAO", "CNSHA": "SHANGHAI",
            "CNNGB": "NINGBO", "CNYTN": "YANTIAN", "CNSHK": "SHEKOU", "KHKOS": "SIHANOUKVILLE", "MAPTM": "TANGERMED",
            "USLAX": "LOSANGELES", "USLGB": "LONGBEACH", "USOAK": "OAKLAND", "USTIW": "TACOMA", "USSEA": "SEATTLE",
            "USSAV": "SAVANNAH", "USCHS": "CHARLESTON", "USNYC": "NEWYORK", "USEWR": "NEWARK", "USORF": "NORFOLK",
            "USBAL": "BALTIMORE", "USHOU": "HOUSTON", "USMOB": "MOBILE", "USTPA": "TAMPA", "USJAX": "JACKSONVILLE",
            "USMIA": "MIAMI", "USPEF": "EVERGLADES", "USHNL": "HONOLULU", "PRSJU": "SANJUAN", "USDAL": "DALLAS",
            "USMEM": "MEMPHIS", "USATL": "ATLANTA", "USCHI": "CHICAGO", "USCLE": "CLEVELAND", "USDET": "DETROIT",
            "USCMH": "COLUMBUS", "USCVG": "CINCINNATI", "USKCK": "KANSASCITY", "USSTL": "STLOUIS", "USELP": "ELPASO",
            "USDEN": "DENVER"}


# Inland rail ramps in the US and Canada: never a seaport. A document printing one where the port of
# discharge goes names the inland leg (60 port conflicts in 1103, Nov 2026: MEMPHIS against SAVANNAH).
INLAND_POINTS = {"MEMPHIS", "DALLAS", "DETROIT", "STPAUL", "SAINTPAUL", "CLEVELAND", "CHICAGO", "ATLANTA",
                 "COLUMBUS", "CINCINNATI", "CINCINATI", "KANSASCITY", "MINNEAPOLIS", "EDMONTON", "CALGARY",
                 "TORONTO", "ELPASO", "DENVER", "LOUISVILLE", "STLOUIS", "PITTSBURGH", "INDIANAPOLIS", "NASHVILLE",
                 "CHARLOTTE", "JOLIET", "OMAHA", "SALTLAKECITY", "PHOENIX", "HARRISBURG", "LAREDO", "SANANTONIO",
                 "WINNIPEG", "REGINA", "SASKATOON", "FORTWORTH", "BIRMINGHAM", "GREER", "HUNTSVILLE"}
# Destinations whose shipments go on by rail 90-100% of the time (Import Logsheet RAIL RAMP, Nov 2026):
# the shipment reference naming one (CC11899DETROIT1-OCT-31, PO# MEMPHIS19-26AUG-13) tells the inland leg.
INLAND_BY_REFERENCE = {"DALLAS", "MEMPHIS", "CLEVELAND", "ATLANTA", "DETROIT", "MINNEAPOLIS", "COLUMBUS",
                       "CINCINNATI", "CINCINATI", "ELPASO"}
_REFERENCE_CITY = re.compile(r"(?:\bC{1,2}\s?\d{4,6}\s?|\bPO\s*#\s*)([A-Z][A-Z .]{2,}?)\s?\d{1,2}\s?-", re.I)


def reference_city(text):
    """The destination city a shipment reference names: CC11899DETROIT1-OCT-31 and PO# MEMPHIS19-26AUG-13
    give DETROIT and MEMPHIS; '' when the text holds no reference."""
    match = _REFERENCE_CITY.search(text or "")
    return re.sub(r"[^A-Z]", "", match.group(1).upper()) if match else ""


def is_inland_point(text):
    """An inland rail ramp, also with one letter typed wrong, lost or added (ATLTANTA is ATLANTA)."""
    if not text:
        return False
    # The whole name as well: port_key drops place words, and SAINT PAUL, MN would be PAUL.
    keys = {port_key(text), "".join(w for w in _words(text) if w not in _US_STATES and w not in ("US", "USA"))}
    keys = {k.replace("RAMP", "") for k in keys} - {""}
    return any(k in INLAND_POINTS or (len(k) >= 6 and any(_one_edit(k, p) for p in INLAND_POINTS)) for k in keys)


def _one_edit(a, b):
    if abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 6:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


# The importer under the names its documents print, beside the Odoo company names (1111 knows only
# Wholesale Tire Distributors): never a factory or forwarder. 10 conflicts in 1111 named KATANA RACING the factory.
OWN_NAMES = ("KATANARACING", "WTDUS", "WHOLESALETIREDISTRIBUTORS")
# Customs brokers: never the ocean forwarder. Triumph Link bills customs entry fees (2,901 PO lines in 1111);
# the 41 shipments its papers named were paid to Hapag-Lloyd (the team's POs, Nov 2026).
CUSTOMS_BROKERS = ("TRIUMPHLINK",)


# North American seaports, where the company's cargo is discharged (by port_key).
DESTINATION_PORTS = {"LOSANGELES", "LONGBEACH", "OAKLAND", "TACOMA", "SEATTLE", "PORTLAND", "SAVANNAH", "CHARLESTON",
                     "NORFOLK", "NEWYORK", "NEWARK", "ELIZABETH", "BAYONNE", "BALTIMORE", "PHILADELPHIA", "WILMINGTON",
                     "BOSTON", "HOUSTON", "GALVESTON", "MOBILE", "NEWORLEANS", "TAMPA", "MIAMI", "JACKSONVILLE",
                     "EVERGLADES", "HONOLULU", "ANCHORAGE", "SANJUAN", "SANDIEGO", "PORTHUENEME", "GULFPORT", "CHESTER",
                     "CAMDEN", "VANCOUVER", "PRINCERUPERT", "MONTREAL", "HALIFAX", "SAINTJOHN"}
_NORTH_AMERICA = re.compile(r"\bU\.?S\.?A?\b|UNITED STATES|\bCANADA\b|PUERTO RICO|,\s*(?:" + "|".join(sorted(_US_STATES))
                            + r"|CA|BC|PR)\b")


def is_destination_port(text):
    """A port in the US, Canada or Puerto Rico: SAVANNAH, GA, USA and USLAX LOS ANGELES are; SINGAPORE,
    YANTIAN and MAPTM TANGER MED are not."""
    upper = (text or "").upper()
    if _NORTH_AMERICA.search(upper) or re.match(r"\s*(?:US|CA|PR)[A-Z]{3}\b", upper):
        return True
    return port_key(text) in DESTINATION_PORTS


# Customs brokers' papers (DO invoices, entry fees): never the ocean freight.
CUSTOMS_BROKER_DOMAINS = ("tlius.com",)


# A terminal inside a port (APM TERMINALS PIER 400 is in Los Angeles): it never disputes the port.
_TERMINAL = re.compile(r"TERMINAL|\bPIER\b|\bBERTH\b|\bWHARF\b", re.I)
# A container number (ONEU0312535) is never a bill of lading number.
_CONTAINER_NUMBER = re.compile(r"[A-Z]{3}[UJZ]\d{7}")


def is_container_number(text):
    return bool(_CONTAINER_NUMBER.fullmatch(normalize_identifier(text) or ""))


def swapped_date(a, b, days=10):
    """Whether date ``b`` is a date near ``a`` with its day and month read the other way round: 2026-08-10 for
    8 October beside 7 October (Tianshi's day-first cargo receipts moved an ETD two months back, Nov 2026)."""
    try:
        x, y = date.fromisoformat(str(a)[:10]), date.fromisoformat(str(b)[:10])
        if x == y or y.day > 12:
            return False
        return abs((y.replace(month=y.day, day=y.month) - x).days) <= days
    except (TypeError, ValueError):
        return False


def min_sea_days(port_of_loading):
    """The fewest days an ETA can follow the ETD. Tanger Med to the US sails in 7 to 13 days (the Booking Report's
    plans, Jun-Oct 2026); from Asia nothing arrives under 9. A nearer date is a stop on the way (Yantian, Singapore,
    Shanghai on a carrier's first leg: 24315154 had 5 days, 17 Nov 2026). At home (an inland leg) only the order counts."""
    if not port_of_loading or is_destination_port(port_of_loading):
        return 1
    if port_key(port_of_loading) == "TANGERMED" or "MOROCCO" in str(port_of_loading).upper():
        return 7
    return 9


def port_key(text):
    """A port as its city: 'USCHS CHARLESTON, SC' and 'CHARLESTON, SC, US' are CHARLESTON; HAI PHONG is HAIPHONG;
    'USPEF PORT' is EVERGLADES; TANGIER is TANGERMED."""
    words = _words(text)
    if len(words) == 1 and words[0] in _LOCODES:
        return _LOCODES[words[0]]  # a UN/LOCODE printed alone (THLCH, USDAL)
    code = ""
    if len(words) > 1 and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}", words[0]):
        code, words = words[0], words[1:]  # the UN/LOCODE printed before the name
    key = "".join(w for w in words if w not in _PLACES and w not in _US_STATES and len(w) > 1
                  and w not in ("PORT", "CITY", "OF"))
    return _PORT_ALIASES.get(key) or key or _PORT_ALIASES.get(code) or _LOCODES.get(code, code)


def party_key(text):
    """A company by the words that name it: TIANSHI LOGISTICS (VIETNAM) CO.,LTD is TIANSHILOGISTICS."""
    return "".join(w for w in _words(text) if w not in _LEGAL and w not in _PLACES)


def vessel_key(text):
    """A vessel by its own name: MAERSK MEMPHIS and HAPAG-LLOYD MEMPHIS are MEMPHIS."""
    return "".join(w for w in _words(text) if w not in _OPERATORS)


def _contains(a, b):
    return bool(a and b) and min(len(a), len(b)) >= 4 and (a in b or b in a)


def same_value(name, old, new, mappings=()):
    """Whether two values of a booking field name the same thing, written another way."""
    if name in ("port_of_loading", "port_of_discharge"):
        return _contains(port_key(old), port_key(new))
    if name in ("forwarder_name", "factory_name"):
        role = name.split("_")[0]
        a, b = resolve_mapping(mappings, role, name=old), resolve_mapping(mappings, role, name=new)
        if a and b and a["partner_id"] == b["partner_id"]:
            return True  # two printed names of one Odoo vendor (an origin agent and the forwarder it works for)
        return _contains(party_key(old), party_key(new))
    if name == "vessel":
        return vessel_key(old) == vessel_key(new) != ""
    return False


# Characters a reading mixes up: one of these pairs is a misreading, two other digits are another number.
_LOOK_ALIKE = [set(pair) for pair in ("0O", "0D", "0Q", "1I", "1L", "5S", "8B", "2Z", "6G", "UV")]


def copy_po_details(odoo, booking_id, order_id):
    """The team's freight PO knows the shipment: its MBL, HBL and containers fill the booking's empty ones, and an
    Ocean Freight PO makes the mode ocean (booking 35254926: linked to P68349, yet no B/L and mode Unknown,
    17 Nov 2026). Returns what was copied."""
    po = odoo.call("purchase.order", "read", [order_id], ["name", "master_bl", "house_bl", "container_number"])[0]
    booking = odoo.call(BOOKING, "read", [booking_id], ["master_bl", "house_bl", "freight_mode", "invoice_ids"])[0]
    source = f"team PO {po['name']}"
    labels = odoo.call(BOOKING, "fields_get", attributes=["type"])
    updates, copied = {}, []
    for name in ("master_bl", "house_bl"):
        first = re.split(r"[,;/&\n]", po.get(name) or "")[0]
        value = normalize_identifier(first)
        if value and plausible_identifier(value) and not booking[name]:
            updates[name] = value
            if f"{name}_source" in labels:
                updates[f"{name}_source"] = source
            copied.append(f"{'MBL' if name == 'master_bl' else 'HBL'} {value}")
    if booking["freight_mode"] in (False, "unknown"):
        updates["freight_mode"] = "ocean"
        copied.append("mode Ocean")
    if updates:
        odoo.write(BOOKING, [booking_id], updates)
    numbers = [c for c in dict.fromkeys(normalize_identifier(c) for c in re.split(r"[,;/\s]+", po.get("container_number") or ""))
               if valid_container_number(c)]
    if numbers and not odoo.call(CONTAINER, "search_count", [("booking_id", "=", booking_id), ("state", "=", "active")]):
        invoice = booking["invoice_ids"][0] if len(booking["invoice_ids"]) == 1 else False
        for number in numbers:
            values = {"booking_id": booking_id, "container_no": number, "source": source}
            if invoice:
                values["invoice_id"] = invoice
            odoo.create(CONTAINER, values)
        copied.append(f"containers {', '.join(numbers)}")
    return copied


def reference_number(text):
    """The booking number a PO's Vendor Reference holds: 31978034-2 (one PO of a split) is 31978034; the
    carrier code is dropped, as on the booking."""
    first = re.split(r"[\s/]|-(?=\d{1,2}$)", (text or "").strip())[0] if text else ""
    return _SCAC.sub("", normalize_identifier(first))


def same_identifier(a, b):
    """Booking and bill numbers that are the same apart from the carrier code before them, or one
    character read wrong, lost or added (from the tenth character: short numbers must match exactly)."""
    a, b = _SCAC.sub("", normalize_identifier(a)), _SCAC.sub("", normalize_identifier(b))
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 9 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        # Only a look-alike character read wrong: EBKG18500445 and EBKG18500447 are two bookings in a row.
        diffs = [{x, y} for x, y in zip(a, b) if x != y]
        return len(diffs) == 1 and diffs[0] in _LOOK_ALIKE
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


# ── impossible values ────────────────────────────────────────────────

# A date further than this from the email that brought it is a misreading.
# A schedule document comes before the vessel sails or soon after it arrives, never months after the departure.
_DATE_WINDOW = {"etd": (timedelta(days=100), timedelta(days=200)), "eta": (timedelta(days=90), timedelta(days=270))}


def plausible_date(name, iso, received):
    """The date as it can be for a document received then, else None. A date with its day and month
    read the wrong way round (2026-06-11 for 11 June when 6 November fits) is turned back."""
    if not iso:
        return None
    try:
        day = date.fromisoformat(iso)
        sent = datetime.fromisoformat(str(received).replace("T", " ").rstrip("Z")).date()
    except (TypeError, ValueError):
        return iso
    before, after = _DATE_WINDOW[name]
    fits = lambda d: sent - before <= d <= sent + after
    if fits(day):
        return iso
    try:
        swapped = day.replace(month=day.day, day=day.month)
    except ValueError:
        return None
    return swapped.isoformat() if fits(swapped) else None


_NOT_A_VESSEL = re.compile(r"DEMURRAGE|DETENTION|CHARGE|INVOICE|TERMINAL|\bPORT\b|\bTBA\b|\bTBN\b|\bN/?A\b", re.I)


def plausible_vessel(text):
    """Not a vessel: a port with its UN/LOCODE (SGSIN SINGAPORE: the code's city part starts the name
    after it, unlike a vessel such as MAPLE LEAF), a charge, a terminal."""
    words = _words(text)
    if not words or _NOT_A_VESSEL.search(text or ""):
        return False
    locode = len(words) > 1 and re.fullmatch(r"[A-Z]{2}[A-Z]{3}", words[0]) and words[0][:2] in _PLACES
    return not (locode and words[1][0] == words[0][2])


def resolve_mapping(mappings, role, sender_domain="", name=""):
    """The vendor mapping row for a sender domain, else for a printed name."""
    rows = [m for m in mappings if m["active"] and m["role"] == role]
    domain = (sender_domain or "").strip().lower()
    if domain:
        for row in rows:
            if (row["sender_domain"] or "").strip().lower() == domain:
                return row
        for row in rows:  # a subdomain is the same sender: De Well writes from qin.dewell.com and vn.dewell.com
            mapped = (row["sender_domain"] or "").strip().lower()
            if mapped and domain.endswith("." + mapped):
                return row
    printed = " ".join((name or "").upper().split())
    if printed:
        # The longest fragment wins: "SENTURY TIRE MOROCCO" over "SENTURY TIRE".
        for row in sorted(rows, key=lambda r: -len((r["document_name"] or "").strip())):
            fragment = " ".join((row["document_name"] or "").upper().split())
            # Lookarounds, not \b: a name may start or end with punctuation, e.g. "SENTURY TIRE (THAILAND)".
            if fragment and re.search(rf"(?<!\w){re.escape(fragment)}(?!\w)", printed):
                return row
    return None


class _History:
    """What a booking's documents said before. Without the pipeline's records nothing is known:
    the schedule then moves only with a newer booking confirmation, as before."""

    def received(self, booking_id, source):
        """When the document named ``source`` on this booking was received; None when unknown."""
        return None

    def pre_carriers(self, booking_id):
        """The feeder vessels and voyages this booking's documents printed, as identifiers."""
        return set()


class Booker:
    """Consolidates documents for one company. ``newest_confirmation(doc,
    booking_id)`` tells whether ``doc`` is the latest booking confirmation the
    booking has received; the pipeline answers it from its own records, and
    ``history`` (when given) what its documents said before: see _History."""

    def __init__(self, odoo, company_id, mappings, newest_confirmation, history=None):
        self.odoo = odoo
        self.company_id = company_id
        self.mappings = mappings
        self.newest_confirmation = newest_confirmation
        self.history = history or _History()
        self._fields = {}
        self._own_names = None

    def fields(self, model):
        """The fields ``model`` has in this Odoo: a module not yet upgraded lacks the newer ones."""
        if model not in self._fields:
            self._fields[model] = self.odoo.call(model, "fields_get", attributes=["string", "required"])
        return self._fields[model]

    def has_field(self, model, name):
        return name in self.fields(model)

    def labels(self):
        return {name: info["string"] for name, info in self.fields(BOOKING).items()}

    def number_optional(self):
        """Whether a booking may exist without a booking number (b2b_ocr_documents from Nov 2026)."""
        return not self.fields(BOOKING).get("booking_number", {}).get("required")

    def own_names(self):
        if self._own_names is None:
            self._own_names = [normalize_identifier(c["name"])
                               for c in self.odoo.search_read("res.company", [], ["name"])] + list(OWN_NAMES)
        return self._own_names

    def _search(self, domain):
        return self.odoo.call(BOOKING, "search", [("company_id", "=", self.company_id)] + domain)

    # ------------------------------------------------------------------
    # Finding the booking of a document
    # ------------------------------------------------------------------

    def take(self, doc):
        """``(state, note, booking_id)`` for ``doc``: ``done`` when it was
        consolidated, ``waiting`` when no single booking fits yet."""
        values = doc["values"]
        # Values read before the reading checked them (stored files read again) are checked here too.
        wrong = [k for k in ("booking_number", "master_bl", "house_bl", "bl_number")
                 if values.get(k) and not plausible_identifier(values[k])]
        if wrong:
            values = {k: v for k, v in values.items() if k not in wrong}
            doc = dict(doc, values=values)
        if values.get("containers") and not all(valid_container_number(c) for c in values["containers"]):
            values = dict(values, containers=[c for c in values["containers"] if valid_container_number(c)])
            doc = dict(doc, values=values)
        factory_doc = doc["doc_class"] in FACTORY_DOCS
        if factory_doc and values.get("bl_number") and not values.get("booking_number"):
            # Sentury prints the booking number as "B/L#": 96 of 96 invoices checked against the Import
            # Logsheet (12 Nov 2026) equal its booking number, or the carrier's bill that is that number.
            values = dict(values, booking_number=values["bl_number"])
            doc = dict(doc, values=values)
        keys = {name: normalize_identifier(values.get(name))
                for name in ("booking_number", "master_bl", "house_bl", "bl_number")}
        ids = set()
        if keys["booking_number"]:
            ids |= set(self._search([("booking_number", "=", keys["booking_number"])]))
        for name in ("master_bl", "house_bl", "bl_number"):
            if keys[name]:
                # A B/L# printed without its type (a factory invoice) may be either bill.
                fields = ("master_bl", "house_bl") if name == "bl_number" else (name,)
                clauses = [(f, "=", keys[name]) for f in fields] + [("bl_ids.bl_number", "=", keys[name])]
                ids |= set(self._search(["|"] * (len(clauses) - 1) + clauses))
        if not ids:
            ids = self._nearly(keys.values())
        if not ids and values.get("containers"):
            ids |= set(self._search([("container_ids.container_no", "in", values["containers"])] + OPEN))
        if not ids and factory_doc and values.get("containers") and values.get("vessel"):
            # A late invoice of a shipment whose other invoices have their POs: the booking takes it and
            # drafts only its PO. A reused container alone is not enough there: the vessel must agree too.
            ids |= {b["id"] for b in self.odoo.search_read(
                BOOKING, [("company_id", "=", self.company_id), ("container_ids.container_no", "in", values["containers"]),
                          ("purchase_order_id", "=", False), ("state", "=", "po_created")], ["vessel"])
                if vessel_key(b["vessel"]) == vessel_key(values["vessel"])}
        if not ids and factory_doc and values.get("invoices"):
            # The same factory invoice again: a revision, or the packing list sent on its own.
            found = {m2o_id(line["booking_id"]) for line in self.odoo.search_read(
                INVOICE, [("invoice_number", "in", values["invoices"])], ["booking_id"])}
            ids |= set(self._search([("id", "in", sorted(found))])) if found else set()
        if not ids:
            ids = self._same_sailing(values, factory_doc)
        if len(ids) > 1:
            names = [self.name(b) for b in self.odoo.call(BOOKING, "read", sorted(ids), ["booking_number"])]
            return "waiting", f"Its identifiers point to bookings {', '.join(names)}.", None
        if not ids and self._may_create(doc, keys["booking_number"]):
            booking_id = self.odoo.create(BOOKING, {"booking_number": keys["booking_number"] or False,
                                                    "company_id": self.company_id})
            if doc["doc_class"] == "email_message":
                self.odoo.write(BOOKING, [booking_id], {"has_uncertain_match": True})
            ids = {booking_id}
        if not ids:
            if factory_doc and values.get("invoices") and not keys["booking_number"] and not self.number_optional():
                return "waiting", ("It prints no booking number, and this Odoo needs one to start a booking "
                                   "(update the Freight Booking module)."), None
            if any(keys.values()) or values.get("containers"):
                return "waiting", "No booking has its booking number, B/L or containers yet.", None
            return "waiting", "It names no booking number, B/L or container.", None
        booking_id = ids.pop()
        self.consolidate(booking_id, doc)
        return "done", None, booking_id

    @staticmethod
    def name(booking):
        return booking["booking_number"] or f"#{booking['id']} (no booking number yet)"

    def _same_sailing(self, values, factory_doc):
        """The one open booking on the same vessel, voyage and ports. A factory paper looks among bookings of
        the same factory (or none yet); a forwarder's document only among bookings still without a booking
        number, which a factory invoice started. Several fit, or a value is missing: none."""
        vessel, voyage = values.get("vessel"), normalize_identifier(values.get("voyage"))
        pol, pod = values.get("port_of_loading"), values.get("port_of_discharge")
        words = [w for w in _words(vessel) if w not in _OPERATORS]
        if not (words and voyage and pol and pod):
            return set()
        domain = [("company_id", "=", self.company_id), ("vessel", "ilike", max(words, key=len))]
        # A factory invoice may come after the shipment's other invoices have their POs (state po_created):
        # only a whole-shipment PO linked to the booking closes it.
        domain += [("purchase_order_id", "=", False)] if factory_doc else OPEN + [("booking_number", "=", False)]
        ids = set()
        for booking in self.odoo.search_read(BOOKING, domain, ["vessel", "voyage", "port_of_loading",
                                                               "port_of_discharge", "factory_name"]):
            if vessel_key(booking["vessel"]) != vessel_key(vessel) or normalize_identifier(booking["voyage"]) != voyage:
                continue
            if not (same_value("port_of_loading", booking["port_of_loading"], pol)
                    and same_value("port_of_discharge", booking["port_of_discharge"], pod)):
                continue
            if factory_doc and booking["factory_name"] and values.get("factory_name") and not same_value(
                    "factory_name", booking["factory_name"], values["factory_name"], self.mappings):
                continue
            ids.add(booking["id"])
        return ids if len(ids) == 1 else set()

    def _nearly(self, keys):
        """The one booking whose number or bill is one of these written another way (see
        same_identifier); none when no booking, or several, fit."""
        ids = set()
        for key in {k for k in keys if k}:
            core = _SCAC.sub("", key)
            if len(core) < 9:
                continue
            tail = core[-6:]  # the database narrows by the tail, the comparison decides
            for booking in self.odoo.search_read(BOOKING, [("company_id", "=", self.company_id), "|", "|", "|",
                                                           ("booking_number", "ilike", tail), ("master_bl", "ilike", tail),
                                                           ("house_bl", "ilike", tail), ("bl_ids.bl_number", "ilike", tail)],
                                                 ["booking_number", "master_bl", "house_bl"]):
                if any(same_identifier(key, booking[f]) for f in ("booking_number", "master_bl", "house_bl")):
                    ids.add(booking["id"])
            if len(core) >= 10:  # one character lost: the tail is not where it was
                for booking in self.odoo.search_read(BOOKING, [("company_id", "=", self.company_id), "|", "|",
                                                               ("booking_number", "ilike", core[:8]), ("master_bl", "ilike", core[:8]),
                                                               ("house_bl", "ilike", core[:8])],
                                                     ["booking_number", "master_bl", "house_bl"]):
                    if any(same_identifier(key, booking[f]) for f in ("booking_number", "master_bl", "house_bl")):
                        ids.add(booking["id"])
        return ids if len(ids) == 1 else set()

    def _may_create(self, doc, number):
        """A booking confirmation creates its booking; so does a factory invoice naming its invoice and
        the shipment (containers or vessel), with or without a booking number; another document only
        from a booking number its subject labels; an email only in a Subject Format that confirms
        bookings, from a mapped forwarder."""
        values = doc["values"]
        if doc["doc_class"] == "commercial_invoice" and values.get("invoices") \
                and (values.get("containers") or values.get("vessel")):
            return bool(number) or self.number_optional()
        if not number:
            return False
        if doc["doc_class"] == "booking_confirmation":
            return True
        if doc["doc_class"] == "email_message":
            domain = (doc["sender"] or "").rpartition("@")[2]
            return bool(doc["values"].get("confirms_booking")
                        and resolve_mapping(self.mappings, "forwarder", sender_domain=domain))
        return find_labelled_booking_number(doc["subject"]) == number

    # ------------------------------------------------------------------
    # Consolidation
    # ------------------------------------------------------------------

    def consolidate(self, booking_id, doc):
        fields = list(_READ) + [f for f in ("rail_ramp", "rail_ramp_source", "port_of_discharge_source")
                                if self.has_field(BOOKING, f)]
        booking = self.odoo.call(BOOKING, "read", [booking_id], fields)[0]
        run = _Consolidation(self, booking, doc)
        run.apply()


class _Consolidation:
    def __init__(self, booker, booking, doc):
        self.booker = booker
        self.odoo = booker.odoo
        self.booking = booking
        self.id = booking["id"]
        self.doc = doc
        self.values = doc["values"]
        self.source = doc["source"]
        self.updates = {}
        self.notes = []
        self.replaced = set()  # parties whose name this document replaced: their partner is found again
        self.history_order = None  # the team's freight PO _PurchaseHistory found, with its vendor

    def current(self, name):
        return self.updates[name] if name in self.updates else self.booking.get(name)

    def apply(self):
        kind = self.doc["doc_class"]
        trusted = {group for group, kinds in TRUSTED.items() if kind in kinds}
        if "schedule" in trusted or "factory_schedule" in trusted:
            pod = self.values.get("port_of_discharge")
            first_stop = bool(pod) and not is_destination_port(pod) and not is_inland_point(pod)
            for name in SCHEDULE_FIELDS:
                if name == "eta" and first_stop:
                    continue  # the ETA at that hub (YANTIAN on Hapag's first leg, 37643170), not in North America
                if name == "etd" and kind == "shipping_notice" and self.current("etd"):
                    continue  # an arrival notice's dates are the arrival: Hapag's moved 14546118's ETD to it
                self.set(name, self.values.get(name))
        if "booking_number" in trusted:
            self.fill_booking_number(normalize_identifier(self.values.get("booking_number")))
        if "reference" in trusted and self.values.get("shipment_reference") and not self.current("shipment_reference"):
            self.updates["shipment_reference"] = self.values["shipment_reference"]
        if self.values.get("rail_ramp") and trusted & {"schedule", "factory_schedule", "email_schedule"}:
            self.set_rail_ramp(self.values["rail_ramp"])
        reference = self.values.get("shipment_reference") or self.doc.get("subject") or ""
        if reference_city(reference) in INLAND_BY_REFERENCE:
            self.set_rail_ramp(reference_city(reference), source=f"shipment reference {reference[:80]}")
        if "email_schedule" in trusted:
            for name in ("etd", "eta"):
                self.set(name, self.values.get(name))
        if "bl" in trusted:
            invoices = {normalize_identifier(i) for i in self.values.get("invoices") or []}
            stem = normalize_identifier(re.sub(r"\.[A-Z0-9]{2,4}$", "", (self.doc.get("source") or "").upper().strip()))
            numbers = {name: normalize_identifier(self.values.get(name)) for name in ("master_bl", "house_bl")}
            if numbers["master_bl"] and stem == numbers["master_bl"] + "H" and not numbers["house_bl"]:
                # Kerry names its house bill "<number>H.pdf": the number read as the MBL is the house bill.
                numbers = {"master_bl": "", "house_bl": numbers["master_bl"]}
            for name, bl_type in (("master_bl", "master"), ("house_bl", "house")):
                number = numbers[name]
                if is_container_number(number):
                    number = ""  # a container number read where the bill of lading number goes
                if number in invoices or _INVOICE_NUMBER.match(number):
                    number = ""  # a factory invoice number (F26AR10THA0693, GRKA-P-2026436) is never a B/L
                self.set(name, number)
                self.add_bl(bl_type, number)
        for role in ("forwarder", "factory"):
            if role in trusted:
                self.set_party(role, self.values.get(f"{role}_name"))
        invoice_ids = {}
        if "invoices" in trusted and self.values.get("invoices"):
            invoice_ids = self.add_invoices(self.values["invoices"])
        if "containers" in trusted and self.values.get("containers"):
            # A factory invoice's containers are that invoice's: its freight PO carries them.
            own = (list(invoice_ids.values())[0] if kind == "commercial_invoice" and len(invoice_ids) == 1
                   else None)
            self.add_containers(self.values["containers"], own)
        if "amounts" in trusted:
            self.set_amounts()
        self.mirror_bls()
        self.check_eta()
        if self.current("freight_mode") == "unknown" and (
                self.current("master_bl") or self.current("house_bl") or self.values.get("containers")):
            self.updates["freight_mode"] = "ocean"
        if not self.booking["purchase_order_id"] and (
                self.current("master_bl") or self.current("house_bl") or self.current("booking_number")):
            self.updates.update(_PurchaseHistory(self).suggestions())
        if self.updates:
            self.odoo.write(BOOKING, [self.id], self.updates)
        self.link_existing_po()
        for note in self.notes:
            self.odoo.call(BOOKING, "message_post", [self.id], body=note)

    def check_eta(self):
        """An ETA too soon after the ETD is a stop on the way, or a date the sailing has since moved past (14546118
        kept 21 Sep after it sailed on 2 Oct): it is dropped, so the arrival is taken from the next paper."""
        etd, eta = self.current("etd"), self.current("eta")
        if not etd or not eta or self.booking.get("eta_confirmed"):
            return
        try:
            days = (date.fromisoformat(str(eta)[:10]) - date.fromisoformat(str(etd)[:10])).days
        except ValueError:
            return
        least = min_sea_days(self.current("port_of_loading"))
        if days >= least:
            return
        self.updates["eta"] = False
        if "eta_source" in self.booker.labels():
            self.updates["eta_source"] = False
        self.notes.append(f"ETA {str(eta)[:10]} dropped: {days} day(s) after the ETD {str(etd)[:10]} is not the arrival "
                          f"(a crossing from {self.current('port_of_loading') or 'there'} takes {least} days or more).")

    def link_existing_po(self):
        """The team already raised this shipment's freight PO (same B/L, forwarder and container count, found by
        _PurchaseHistory): link it, so the booking shows PO Matched and no second PO is drafted (user, 17 Nov 2026).
        The module's Link Existing PO also copies the PO's vendor and rate per container onto the booking."""
        order = self.updates.get("history_purchase_order_id")
        if not order or self.booking.get("purchase_order_id"):
            return
        if self.booker.has_field(INVOICE, "purchase_order_id") and self.odoo.call(
                INVOICE, "search_count", [("booking_id", "=", self.id), ("purchase_order_id", "!=", False)]):
            return  # per-invoice POs exist already: linking another would bill the shipment twice
        try:
            self.odoo.call(BOOKING, "action_link_existing_po", [self.id])
        except Exception as exc:  # an older module without the action, or Odoo refusing it
            _logger.warning("Booking %s: existing freight PO %s not linked: %s", self.id, order, exc)
            return
        self.notes.append("PO already existing: the team's freight PO for this shipment (same B/L, forwarder and "
                          "containers) was linked by the OCR service; no new PO is needed.")
        vendor = (self.history_order or {}).get("partner_id")
        forwarder = self.current("forwarder_id")
        forwarder = forwarder[0] if isinstance(forwarder, list) else forwarder
        if vendor and forwarder and m2o_id(vendor) != forwarder:
            # The PO's vendor is the one paid: it replaces the forwarder the papers named.
            mapped = [m["partner_name"] for m in self.booker.mappings
                      if m["partner_id"] == forwarder and "partner_name" in m.keys() and m["partner_name"]]
            was = mapped[0] if mapped else self.current("forwarder_name")
            self.odoo.write(BOOKING, [self.id], {"forwarder_id": m2o_id(vendor), "forwarder_name": vendor[1]})
            self.notes.append(f"Forwarder set to {vendor[1]}, the vendor of the team's PO {self.history_order['name']} "
                              f"(the papers named {was}).")
        copied = copy_po_details(self.odoo, self.id, order)
        if copied:
            self.notes.append(f"From the team's PO: {', '.join(copied)}.")

    def take_value(self, name, value):
        self.updates[name] = value
        if f"{name}_source" in self.booker.labels():  # only some fields name their document
            self.updates[f"{name}_source"] = self.source

    def set(self, name, value):
        """Fill an empty field; keep the first value and open a conflict for a different one, unless it is
        the same value written another way, the feeder vessel beside the ocean one, a newer document moving
        the schedule (an older one never disputes it), or a carrier naming itself beside the forwarder."""
        if self.booking.get(f"{name}_confirmed"):
            return
        if name in ("etd", "eta"):
            value = plausible_date(name, parse_date(value), self.doc["received"]) or ""
            if name == "eta" and value:
                etd = (plausible_date("etd", parse_date(self.values.get("etd")), self.doc["received"])
                       or self.current("etd"))
                if etd and value <= str(etd)[:10]:
                    return  # arriving the day it sails or before: another date read as the ETA
        else:
            value = str(value or "").strip()
            if name == "vessel" and value and not plausible_vessel(value):
                return
        if not value:
            return
        old = self.current(name) or ""
        if name == "port_of_discharge" and is_inland_point(value):
            if not is_inland_point(old):
                self.set_rail_ramp(value)  # the inland ramp, printed where the seaport goes
                return
        elif name == "port_of_discharge" and is_inland_point(old):
            # The booking held the inland ramp as its port: the seaport takes the place, the ramp moves.
            self.set_rail_ramp(old, source=self.booking.get("port_of_discharge_source") or "the port of discharge")
            self.take_value(name, value)
            self.notes.append(f"Port of Discharge set to the seaport {value}; {old} is the rail ramp ({self.source}).")
            return
        if not old or self.booking.get(f"{name}_assumed"):
            self.take_value(name, value)
            if f"{name}_assumed" in self.booking:
                self.updates[f"{name}_assumed"] = False
                self.booking[f"{name}_assumed"] = False
            return
        if str(self.booking.get(f"{name}_source") or "").startswith("Booking Report") \
                and self.doc["doc_class"] != "email_message":
            # The team's plan filled it while no paper had: the paper replaces the plan.
            self.take_value(name, value)
            self.notes.append(f"{self.booker.labels().get(name, name)} set to {value} by {self.source} "
                              f"(was {old} from the {self.booking.get(f'{name}_source')}).")
            return
        if name in ("etd", "eta") and swapped_date(old, value):
            return  # the same date with day and month the other way round
        if name == "port_of_discharge" and is_destination_port(value) != is_destination_port(old):
            # The cargo comes to North America: a foreign port held as the port of discharge is a transshipment hub
            # (SINGAPORE, YANTIAN on a Hapag confirmation's first leg) or the port of loading (TANGER MED).
            if is_destination_port(value):
                self.take_value(name, value)
                self.notes.append(f"Port of Discharge set to {value}; {old} is not a destination port ({self.source}).")
            return
        if name in ("master_bl", "house_bl", "booking_number") and same_identifier(old, value):
            return  # one look-alike character read differently (HLCUBKK2609ATNKS, ATNK5): the first reading stays
        terminal_new, terminal_old = bool(_TERMINAL.search(value)), bool(_TERMINAL.search(old))
        if name in ("port_of_loading", "port_of_discharge") and terminal_new != terminal_old:
            if terminal_new:
                return  # a terminal in the port never disputes the port
            self.take_value(name, value)  # the port replaces the terminal that held its place
            return
        a, b = normalize_identifier(old), normalize_identifier(value)
        # An email, or the factory's own paper, fills empty fields but never disputes a value.
        email = self.doc["doc_class"] == "email_message" or self.doc["doc_class"] in FACTORY_DOCS
        if a == b or (b and b in a):
            return
        if name == "house_bl" and not email and a == normalize_identifier(self.current("master_bl")):
            # The HBL only repeated the MBL (no house bill known yet): the forwarder's own house bill takes it.
            self.take_value(name, value)
            self.notes.append(f"House B/L set to {value} (it only repeated the MBL {old}) by {self.source}.")
            return
        if a and a in b and not email:
            self.take_value(name, value)  # the same value, stated more fully
            return
        if same_value(name, old, value, self.booker.mappings):
            return
        label = self.booker.labels().get(name, name)
        if name in ("vessel", "voyage"):
            feeders = self.booker.history.pre_carriers(self.id)
            if b in feeders:
                return  # a bill of lading prints the first (feeder) vessel; the booking keeps the ocean one
            if a in feeders and not email:
                self.take_value(name, value)
                self.notes.append(f"{label} set to the ocean vessel {value} (was the feeder {old}) by {self.source}.")
                return
        if name == "forwarder_name":
            # A mapped forwarder's own papers (Kerry's notices from kln.com) also print a staff handle (BRIGHT #241)
            # or the carrier's agent (MARINE (HKG) LTD.): an unmapped name there never disputes; the forwarder's
            # own mapped name replaces such a name.
            sender = resolve_mapping(self.booker.mappings, "forwarder",
                                     sender_domain=(self.doc["sender"] or "").rpartition("@")[2])
            if sender:
                printed = resolve_mapping(self.booker.mappings, "forwarder", name=value)
                if not printed:
                    return
                if printed["partner_id"] == sender["partner_id"] \
                        and not resolve_mapping(self.booker.mappings, "forwarder", name=old):
                    self.take_value(name, value)
                    return
            if CARRIER.search(value) and not CARRIER.search(old):
                return  # the carrier's own document names the carrier; the forwarder stays
            # Only a mapped forwarder replaces a carrier: an unmapped name may be a shipper misread as forwarder.
            if CARRIER.search(old) and not CARRIER.search(value) and not email \
                    and resolve_mapping(self.booker.mappings, "forwarder", name=value):
                self.take_value(name, value)
                self.replaced.add("forwarder")
                self.notes.append(f"Forwarder set to {value} (was the carrier {old}) by {self.source}.")
                return
        if name in UPDATED_BY_CONFIRMATION and not email:
            then = self.booker.history.received(self.id, self.booking.get(f"{name}_source"))
            now = self.doc.get("received")
            if then and now and str(now) != str(then):
                if str(now) > str(then):  # the shipment was re-planned: the later document stands
                    self.take_value(name, value)
                    self.notes.append(f"{label} updated from {old} to {value} by {self.source}, "
                                      f"a later document than {self.booking.get(f'{name}_source')}.")
                return  # an older document never disputes a newer one
        if name in UPDATED_BY_CONFIRMATION and self.doc["doc_class"] == "booking_confirmation" \
                and self.booker.newest_confirmation(self.doc, self.id):
            self.take_value(name, value)
            self.notes.append(f"{label} updated from {old} to {value} by {self.source}.")
            return
        if not email:  # an email never disputes a document
            self.conflict(name, old, value)

    def conflict(self, name, old, new):
        """One conflict per field and value, open or settled: reading the same
        file again, or a value a person already decided, adds nothing."""
        if self.odoo.call(CONFLICT, "search_count", [("booking_id", "=", self.id), ("field_name", "=", name),
                                                     ("new_value", "=", str(new))]):
            return
        self.odoo.create(CONFLICT, {"booking_id": self.id, "field_name": name,
                                    "field_label": self.booker.labels().get(name, name),
                                    "existing_value": str(old), "new_value": str(new), "source": self.source})

    def set_party(self, role, name):
        if not name:
            return
        if any(own and own in normalize_identifier(name) for own in self.booker.own_names()):
            return  # the importer printed as consignee or booking party
        if role == "forwarder" and any(broker in normalize_identifier(name) for broker in CUSTOMS_BROKERS):
            return  # the customs broker's own papers (DO invoice, ISF) name it, not the ocean forwarder
        domain = (self.doc["sender"] or "").rpartition("@")[2]
        by_domain = resolve_mapping(self.booker.mappings, role, sender_domain=domain)
        if role == "forwarder" and by_domain and CARRIER.search(name) and not CARRIER.search(by_domain["document_name"] or ""):
            # A forwarder sends the carrier's own confirmation (De Well forwards Hapag-Lloyd's, 37643170): the carrier
            # printed there is not the payable vendor, the sender is (check of the Ready for PO bookings, 17 Nov 2026).
            name = (by_domain["partner_name"] if "partner_name" in by_domain.keys() else None) or by_domain["document_name"]
        self.set(f"{role}_name", name)
        if self.current(f"{role}_id") and role not in self.replaced:
            return
        by_printed = resolve_mapping(self.booker.mappings, role, name=name)
        # One sender domain can serve several factories (Sentury Thailand and Morocco, Jinfei and
        # Vossen): when the printed name names another mapped vendor, the document wins.
        mapping = by_printed if by_printed and by_domain and by_printed is not by_domain else (by_domain or by_printed)
        by_name = mapping is not by_domain
        if not mapping:
            return
        self.updates[f"{role}_id"] = mapping["partner_id"]
        if role == "forwarder":
            if by_name:
                self.updates["has_uncertain_match"] = True
            if mapping["freight_product_id"]:
                self.updates["mapping_freight_product_id"] = mapping["freight_product_id"]
            if mapping["cost_per_container"]:
                self.updates["mapping_cost_per_container"] = mapping["cost_per_container"]
            if mapping["freight_mode"]:
                self.updates["freight_mode"] = mapping["freight_mode"]

    def add_bl(self, bl_type, number):
        if number and not self.odoo.call(BL, "search_count", [("booking_id", "=", self.id), ("bl_type", "=", bl_type),
                                                              ("bl_number", "=", number)]):
            self.odoo.create(BL, {"booking_id": self.id, "bl_type": bl_type, "bl_number": number,
                                  "source": self.source})

    def mirror_bls(self):
        """A shipment with a single bill: the other B/L is assumed equal until a
        document states it or a person confirms it."""
        for name, other in (("master_bl", "house_bl"), ("house_bl", "master_bl")):
            if self.current(other) and not self.current(name):
                self.updates.update({name: self.current(other), f"{name}_assumed": True,
                                     f"{name}_source": self.updates.get(f"{other}_source") or self.source})

    def set_rail_ramp(self, value, source=None):
        """The inland rail ramp or place of delivery beyond the seaport. A ramp a document prints replaces one
        only inferred from the shipment reference; any other ramp held is kept."""
        if not value or not self.booker.has_field(BOOKING, "rail_ramp"):
            return
        held, held_from = self.current("rail_ramp"), str(self.current("rail_ramp_source") or "")
        if held and not (source is None and held_from.startswith("shipment reference")):
            return
        self.updates["rail_ramp"] = value
        if self.booker.has_field(BOOKING, "rail_ramp_source"):
            self.updates["rail_ramp_source"] = source or self.source

    def fill_booking_number(self, number):
        """A booking a factory invoice started without a number takes the first one a trusted document
        states. A number already held is never replaced; a number another booking holds is left to a person."""
        if not number or self.current("booking_number"):
            return
        others = self.booker._search([("booking_number", "=", number), ("id", "!=", self.id)])
        if others:
            self.notes.append(f"{self.source} states booking number {number}, which booking #{others[0]} "
                              f"already holds: the two may be one shipment.")
            return
        self.updates["booking_number"] = number
        self.notes.append(f"Booking number {number} set by {self.source}.")

    def add_containers(self, containers, invoice_id=None):
        """New containers of a booking that has none are added. A factory invoice's containers are added
        tagged with their invoice, and so are those of a document listing every container the factory
        invoices already gave plus more (the shipment's other invoices). Any other new container is a
        replacement or an addition a person decides on (a container change)."""
        tagging = self.booker.has_field(CONTAINER, "invoice_id")
        invoice_id = invoice_id if tagging else None
        lines = self.odoo.search_read(CONTAINER, [("booking_id", "=", self.id)],
                                      ["container_no", "state"] + (["invoice_id"] if tagging else []))
        known = {line["container_no"]: line for line in lines}
        if invoice_id:  # the factory invoice's own containers belong to it, whoever gave or tagged them first
            # (the fill tags a booking's containers to its only invoice; a later invoice takes back its own)
            others = [known[c]["id"] for c in containers if c in known and known[c]["state"] == "active"
                      and m2o_id(known[c].get("invoice_id")) != invoice_id]
            if others:
                self.odoo.write(CONTAINER, others, {"invoice_id": invoice_id})
        new = [c for c in containers if c not in known]
        if not new:
            return
        active = [line for line in lines if line["state"] == "active"]
        superset = tagging and active and all(line.get("invoice_id") for line in active) \
            and {line["container_no"] for line in active} <= set(containers)
        if not active or invoice_id or superset:
            for number in new:
                values = {"booking_id": self.id, "container_no": number, "source": self.source}
                if invoice_id:
                    values["invoice_id"] = invoice_id
                self.odoo.create(CONTAINER, values)
            return
        active = [line["id"] for line in active]
        # A replacement or an addition: a person decides in Odoo.
        if self.odoo.call(CHANGE, "search_count", [("booking_id", "=", self.id), ("source", "=", self.source),
                                                   ("state", "=", "open")]):
            return
        self.odoo.create(CHANGE, {
            "booking_id": self.id, "source": self.source, "existing_container_ids": [(6, 0, active)],
            "new_container_lines": json.dumps([{"container_no": number} for number in new])})

    def add_invoices(self, numbers):
        """``{number: invoice line id}`` for the factory invoices ``numbers``, the new ones added with the
        seller as printed (the key is seller + invoice number)."""
        lines = {line["invoice_number"]: line["id"] for line in self.odoo.search_read(
            INVOICE, [("booking_id", "=", self.id)], ["invoice_number"])}
        seller = self.values.get("factory_name") if self.doc["doc_class"] == "commercial_invoice" else None
        out = {}
        for number in numbers:
            if number not in lines:
                values = {"booking_id": self.id, "invoice_number": number, "source": self.source}
                if seller and self.booker.has_field(INVOICE, "factory_name"):
                    values["factory_name"] = seller
                lines[number] = self.odoo.create(INVOICE, values)
            out[number] = lines[number]
        return out

    def set_amounts(self):
        amounts = {name: self.values[name] for name in ("freight_cost", "additional_charges") if self.values.get(name)}
        sender = (self.doc["sender"] or "").rpartition("@")[2].lower()
        if not amounts or sender in CUSTOMS_BROKER_DOMAINS:
            return  # a customs broker's DO invoice bills its own fees ($110 entry), not the ocean freight
        currency = self.odoo.search_read("res.currency", [("name", "=", (self.values.get("currency") or "").upper())],
                                         ["name", "rounding"], context={"active_test": False}, limit=1)
        if not currency:
            return  # an amount in an unknown currency is not an amount
        currency = currency[0]
        held = self.booking["freight_currency_id"]
        if held and held[0] != currency["id"]:
            self.notes.append(f"{self.source} states freight in {currency['name']}, the booking holds {held[1]}: "
                              f"its amounts were not taken.")
            return
        self.updates["freight_currency_id"] = currency["id"]
        # The freight PO is priced from the agreed rate per container, never from these amounts (freight PO study,
        # 16 Nov 2026): another amount is noted on the booking, never a conflict for a person.
        for name, amount in amounts.items():
            old = self.booking[name]
            if not old:
                self.updates[name] = amount
            elif abs(old - amount) > currency["rounding"]:
                self.notes.append(f"{self.source} states {self.booker.labels().get(name, name)} {amount:,.2f}; "
                                  f"the booking keeps {old:,.2f}.")


class _PurchaseHistory:
    """What Odoo's purchases already say about this shipment, as suggestions."""

    def __init__(self, run):
        self.run = run
        self.odoo = run.odoo

    _FIELDS = ["name", "partner_id", "partner_ref", "master_bl", "house_bl", "invoice_ids", "picking_type_id",
               "date_order"]

    def by_containers(self, held, open_orders):
        """POs listing every container this booking holds, raised around its sailing. A container is reused shipment
        after shipment, so only a PO dated from 30 days before the ETD to 150 days after it counts."""
        try:
            sailed = date.fromisoformat(str(self.run.current("etd") or "")[:10])
        except ValueError:
            return []
        if not held:
            return []
        clauses = [("container_number", "ilike", c) for c in held]
        out = []
        for o in self.odoo.search_read("purchase.order", open_orders + ["|"] * (len(clauses) - 1) + clauses,
                                       self._FIELDS + ["container_number"]):
            listed = {normalize_identifier(c) for c in re.split(r"[,;/&\s]+", o["container_number"] or "")}
            try:
                day = date.fromisoformat(str(o["date_order"] or "")[:10])
            except ValueError:
                continue
            if set(held) <= listed and sailed - timedelta(days=30) <= day <= sailed + timedelta(days=150):
                out.append(o)
        return out

    def suggestions(self):
        keys = {k for k in (self.run.current("master_bl"), self.run.current("house_bl")) if k}
        number = _SCAC.sub("", normalize_identifier(self.run.current("booking_number")))
        po_fields = self.odoo.call("purchase.order", "fields_get", attributes=["type"])
        held = [c["container_no"] for c in self.odoo.search_read(
            CONTAINER, [("booking_id", "=", self.run.id), ("state", "=", "active")], ["container_no"])]
        if (not keys and len(number) < 6 and not held) or "master_bl" not in po_fields:
            return {}
        open_orders = [("state", "!=", "cancel"), ("company_id", "=", self.run.booker.company_id)]
        orders = []
        if keys:
            # The carrier code may stand before the number on either side: WHLC035GX69537 is 035GX69537 (P70039).
            bare = {_SCAC.sub("", normalize_identifier(k)) for k in keys}
            clauses = [c for k in keys for c in (("master_bl", "ilike", k[-6:]), ("house_bl", "ilike", k[-6:]))]
            orders = self.odoo.search_read("purchase.order", open_orders + ["|"] * (len(clauses) - 1) + clauses,
                                           self._FIELDS)
            orders = [o for o in orders if bare & {_SCAC.sub("", normalize_identifier(p)) for p in re.split(
                r"[,;/&\n]|\bAND\b", f"{o['master_bl'] or ''},{o['house_bl'] or ''}", flags=re.I)}]
        by_container = self.by_containers(held, open_orders) if "container_number" in po_fields else []
        # The team's Vendor Reference is the booking number on most freight POs (Hapag 94659872; De Well
        # 31978034-2 for a split PO): it finds the PO first, the B/L when it holds something else (user, 17 Nov 2026).
        by_reference = []
        if len(number) >= 6:
            by_reference = [o for o in self.odoo.search_read("purchase.order", open_orders + [("partner_ref", "ilike", number)],
                                                              self._FIELDS)
                            if reference_number(o["partner_ref"]) == number]
        if not orders and not by_reference and not by_container:
            return {}
        lines = self.odoo.search_read("purchase.order.line",
                                      [("order_id", "in", sorted({o["id"] for o in orders + by_reference + by_container}))],
                                      ["order_id", "product_id", "product_qty"])
        product_ids = sorted({m2o_id(line["product_id"]) for line in lines if line["product_id"]})
        storable = {p["id"] for p in self.odoo.call("product.product", "read", product_ids, ["is_storable"])
                    if p["is_storable"]}
        freight_products = {m["freight_product_id"] for m in self.run.booker.mappings if m["freight_product_id"]}
        freight_products |= {m2o_id(self.run.booking["freight_product_id"]),
                             m2o_id(self.run.booking["mapping_freight_product_id"]),
                             self.run.updates.get("mapping_freight_product_id")} - {False, None}
        goods = [o for o in orders if any(m2o_id(line["order_id"]) == o["id"] and m2o_id(line["product_id"]) in storable
                                          for line in lines)]
        values = {}
        if orders:  # the goods POs are found by B/L only
            bill_ids = [i for o in goods for i in o["invoice_ids"]]
            bills = self.odoo.search_read("account.move", [("id", "in", bill_ids), ("state", "=", "posted"),
                                                           ("move_type", "=", "in_invoice")], ["ref"]) if bill_ids else []
            refs = sorted({normalize_identifier(r) for r in [o["partner_ref"] for o in goods] + [b["ref"] for b in bills]}
                          - {""})
            values["invoice_references"] = ",".join(refs) or False

        forwarder = self.run.current("forwarder_id")
        forwarder = forwarder[0] if isinstance(forwarder, list) else forwarder
        containers = self.odoo.call(CONTAINER, "search_count", [("booking_id", "=", self.run.id),
                                                                 ("state", "=", "active")])
        def ocean(o, whole=True):
            return any(m2o_id(line["order_id"]) == o["id"] and m2o_id(line["product_id"]) in freight_products
                       and (not whole or not containers or line["product_qty"] == containers) for line in lines)

        def same_vendor(o):
            return not forwarder or m2o_id(o["partner_id"]) == forwarder

        found = {o["id"] for o in orders + by_container}
        # The PO named after the booking is its PO; under another vendor too when a B/L or container agrees, the
        # vendor on the papers being the one wrong (K-APEX papers, Quartz PO P78483 for BKKM68473200).
        freight = ([o for o in by_reference if ocean(o) and (same_vendor(o) or o["id"] in found)]
                   or [o for o in orders if same_vendor(o) and ocean(o)]
                   # A PO raised for two bookings holds both shipments' containers (P78536: 22492448 and 37643170).
                   or [o for o in by_container if same_vendor(o) and ocean(o, whole=False)])
        if freight:
            order = max(freight, key=lambda o: o["date_order"])
            values["history_purchase_order_id"] = order["id"]
            self.run.history_order = order
            if not forwarder:
                values["history_forwarder_id"] = m2o_id(order["partner_id"])
        factories = {m2o_id(o["partner_id"]) for o in goods}
        if len(factories) == 1 and not self.run.current("factory_id"):
            values["history_factory_id"] = factories.pop()
        types = {m2o_id(o["picking_type_id"]) for o in goods} - {False}
        if len(types) == 1 and not self.run.booking["warehouse_id"]:
            warehouse = self.odoo.call("stock.picking.type", "read", list(types), ["warehouse_id"])[0]["warehouse_id"]
            if warehouse:
                values["history_warehouse_id"] = warehouse[0]
        return values


def link_team_pos(odoo, mappings):
    """The team uploads its freight POs from the logsheet after the papers arrive (user, 17 Nov 2026): every booking
    still without a PO is looked up again, and the team's PO, once it exists, is linked as PO already existing so no
    second PO is drafted. Run on the hourly pass. Returns how many bookings were linked."""
    bookers, linked = {}, 0
    fields = list(_READ) + ["company_id"]
    for booking in odoo.search_read(BOOKING, [("purchase_order_id", "=", False),
                                              ("state", "not in", ["po_created", "po_matched"])], fields):
        if not (booking["booking_number"] or booking["master_bl"] or booking["house_bl"]):
            continue
        company = booking["company_id"][0] if isinstance(booking["company_id"], list) else booking["company_id"]
        booker = bookers.setdefault(company, Booker(odoo, company, mappings, lambda _doc, _booking: False))
        run = _Consolidation(booker, booking, {"doc_class": "team_po_check", "values": {}, "source": "the team's POs",
                                               "subject": "", "sender": "", "received": None, "file_id": None})
        values = _PurchaseHistory(run).suggestions()
        if not values.get("history_purchase_order_id"):
            continue
        odoo.write(BOOKING, [booking["id"]], values)
        run.updates.update(values)
        run.link_existing_po()
        for note in run.notes:
            odoo.call(BOOKING, "message_post", [booking["id"]], body=note)
        linked += 1
    return linked
