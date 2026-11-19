"""Ports as their city: UN/LOCODEs, aliases, inland rail ramps, North American destination ports, and the fewest days a crossing takes."""

import re

from app.freight_booking.rules.words import _PLACES, _US_STATES, _words


# One port under two names. Sentury Morocco's invoices print TANGIER for the container port Tanger Med;
# a bare UN/LOCODE stands for its port.
_PORT_ALIASES = {"TANGIER": "TANGERMED", "TANGER": "TANGERMED", "TANGIERMED": "TANGERMED", "MAPTM": "TANGERMED",
                 "USPEF": "EVERGLADES", "SAIGON": "HOCHIMINH"}


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


# A terminal inside a port (APM TERMINALS PIER 400 is in Los Angeles): it never disputes the port.
_TERMINAL = re.compile(r"TERMINAL|\bPIER\b|\bBERTH\b|\bWHARF\b", re.I)


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
