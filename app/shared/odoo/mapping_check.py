"""The check before an Odoo connection is switched on: every Odoo record the mailboxes and vendor
mappings point at must exist there, under the same name as in the connection the pages read
(store.reference_connection). The databases are copies, so the ids should match; this catches the
day they stop matching, before a booking lands on the wrong partner.
"""

# (model, what it is to a person, query for the ids in use)
MAPPED = [
    ("res.company", "Mailbox company", "SELECT DISTINCT company_id FROM mailbox WHERE company_id IS NOT NULL"),
    ("res.partner", "Vendor partner", "SELECT DISTINCT partner_id FROM vendor_mapping WHERE partner_id IS NOT NULL"),
    ("product.product", "Freight product",
     "SELECT DISTINCT freight_product_id FROM vendor_mapping WHERE freight_product_id IS NOT NULL"),
]
# The models the odoo fields of the Mailboxes and Vendor mappings forms point at.
OPTION_MODELS = {"companies": "res.company", "partners": "res.partner", "products": "product.product"}


def mapped_ids(db):
    """{model: ids} of every mapped record, active mappings or not: one switched on later must be safe too."""
    return {model: sorted(row[0] for row in db.execute(query)) for model, _label, query in MAPPED}


def names(odoo, model, ids):
    """{id: display name} of the records that exist, archived ones included."""
    if not ids:
        return {}
    return {row["id"]: row["display_name"] for row in odoo.search_read(
        model, [("id", "in", list(ids))], ["display_name"], context={"active_test": False})}


def mismatches(odoo, name, wanted, reference=None, reference_name=None, stored=None):
    """One line per record in ``wanted`` ({model: ids}) that is missing through ``odoo`` or named
    differently than through ``reference``. Without a reference, partners are compared with the
    names stored on the vendor mappings (``stored``: {partner id: name}); the rest only need to exist."""
    labels = {model: label for model, label, _query in MAPPED}
    lines = []
    for model, ids in wanted.items():
        if not ids:
            continue
        found = names(odoo, model, ids)
        expected = names(reference, model, ids) if reference is not None else (
            stored or {} if model == "res.partner" else {})
        for record_id in ids:
            label = f"{labels.get(model, model)} #{record_id}"
            if record_id not in found:
                lines.append(f"{label}: not found in {name}.")
            elif expected.get(record_id) and expected[record_id] != found[record_id]:
                where = reference_name or "the vendor mapping"
                lines.append(f"{label}: \"{expected[record_id]}\" in {where}, \"{found[record_id]}\" in {name}.")
    return lines


def check(db, odoo, name, reference=None, reference_name=None):
    """mismatches() for everything the mailboxes and vendor mappings point at."""
    stored = {row[0]: row[1] for row in db.execute(
        "SELECT partner_id, partner_name FROM vendor_mapping WHERE partner_id IS NOT NULL AND partner_name IS NOT NULL")}
    return mismatches(odoo, name, mapped_ids(db), reference, reference_name, stored)
