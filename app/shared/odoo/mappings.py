"""Vendor mappings (the setup pages' Vendor Mappings) and Odoo many2one values, as every job reads them."""

import re


def m2o_id(value):
    """The id of a many2one as XML-RPC returns it ([id, name] or False)."""
    return value[0] if value else False


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
