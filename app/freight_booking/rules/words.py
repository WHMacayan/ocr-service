"""Words a company, port or vessel name carries that say nothing about which one it is."""

import re
import unicodedata



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
