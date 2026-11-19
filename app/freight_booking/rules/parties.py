"""Carriers, customs brokers and the importer's own names: who a paper's printed party is."""

import re

from app.freight_booking.rules.words import _LEGAL, _PLACES, _words


# ── the same value, written another way ──────────────────────────────

# Ocean carriers. Their own documents name the carrier where a forwarder would be: never a dispute.
CARRIER = re.compile(r"HAPAG|MAERSK|\bMSC\b|MEDITERRANEAN SHIPPING|CMA[\s-]*CGM|COSCO|OCEAN NETWORK EXPRESS|\bONE\b"
                     r"|EVERGREEN|YANG MING|\bHMM\b|HYUNDAI MERCHANT|\bZIM\b|WAN HAI|\bOOCL\b|ORIENT OVERSEAS"
                     r"|\bPIL\b|PACIFIC INTERNATIONAL LINES|\bSEALAND\b", re.IGNORECASE)


# The importer under the names its documents print, beside the Odoo company names (1111 knows only
# Wholesale Tire Distributors): never a factory or forwarder. 10 conflicts in 1111 named KATANA RACING the factory.
OWN_NAMES = ("KATANARACING", "WTDUS", "WHOLESALETIREDISTRIBUTORS")


# Customs brokers: never the ocean forwarder. Triumph Link bills customs entry fees (2,901 PO lines in 1111);
# the 41 shipments its papers named were paid to Hapag-Lloyd (the team's POs, Nov 2026).
CUSTOMS_BROKERS = ("TRIUMPHLINK",)


# Customs brokers' papers (DO invoices, entry fees): never the ocean freight.
CUSTOMS_BROKER_DOMAINS = ("tlius.com",)


def party_key(text):
    """A company by the words that name it: TIANSHI LOGISTICS (VIETNAM) CO.,LTD is TIANSHILOGISTICS."""
    return "".join(w for w in _words(text) if w not in _LEGAL and w not in _PLACES)
