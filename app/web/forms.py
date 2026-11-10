"""The setup forms (mailboxes, vendor mappings, subject formats): their fields, and reading, checking
and describing what was posted."""

import re
from collections import namedtuple

from flask import abort, g

from ..core import store
from ..odoo import mapping_check
from ..odoo.client import Odoo


# kind: text | textarea | pattern | int | float | bool | date | secret | select | odoo
# options: (value, label) pairs for select; the name of an ODOO_OPTIONS loader for odoo.
# help: a short hint under the field. tip: what the field is for, behind its info icon.
Field = namedtuple("Field", "name label kind options help name_column group tip",
                   defaults=("text", None, "", None, "", ""))
Form = namedtuple("Form", "table title singular columns fields groups")

PURPOSES = [("freight_booking", "Freight Booking"), ("bill_matching", "Bill Matching"),
            ("customs_entry", "Customs Entry Summaries"), ("freight_payables", "Freight Payables")]
SUBJECT_GROUPS = {"booking_number", "master_bl", "house_bl", "invoice_number", "etd", "eta"}


FORMS = {
    "mailboxes": Form(
        "mailbox", "Mailboxes", "mailbox", ["name", "purpose", "status", "last_sync"], [
            Field("name", "Name", group="Mailbox",
                  tip="A name for this mailbox, shown in the lists and the activity log."),
            Field("purpose", "Purpose", "select", PURPOSES, group="Mailbox",
                  tip="Freight Booking: shipping documents become booking suggestions in Odoo. Bill Matching: "
                      "supplier invoices are matched to a draft bill or purchase order. Freight Payables: "
                      "freight invoices and the freight audit as Excel workbooks; never written to Odoo."),
            Field("company_id", "Company", "odoo", "companies", group="Mailbox",
                  tip="The Odoo company the bookings or bills from this mailbox belong to."),
            Field("active", "Active", "bool", help="Inactive mailboxes are never read", group="Mailbox",
                  tip="Switch off to stop reading this mailbox. Its emails and history are kept."),
            Field("tenant_id", "Tenant ID", group="Outlook app",
                  tip="Your Microsoft 365 organisation's ID. Azure portal > App registrations > your app > "
                      "Overview > Directory (tenant) ID."),
            Field("client_id", "Client ID", group="Outlook app",
                  tip="The ID of the Azure app the service signs in through. Azure portal > App registrations > "
                      "your app > Overview > Application (client) ID."),
            Field("client_secret", "Client secret", "secret", group="Outlook app",
                  tip="A password for that Azure app, made under Certificates & secrets. Needed to connect "
                      "Outlook and to stay connected. When it expires in Azure, make a new one and paste it here."),
            Field("start_from", "Read mail received from", "date",
                  help="Reading starts on this date (UTC); earlier mail is never read", group="Reading",
                  tip="Set this to the day you switch over from the old Odoo module, so mail it already "
                      "imported is not imported again. Reading starts here however far back it is; "
                      "Look back is then not used."),
            Field("folder", "Folder", help="Leave empty to read every folder", group="Reading",
                  tip="The Outlook folder to read, for example Inbox or a subfolder name."),
            Field("fetch_window_days", "Look back (days)", "int", group="Reading",
                  help="Only when Read mail received from is empty",
                  tip="How far back a read looks when the mailbox has no start date. With a start date, "
                      "reading starts on that date instead."),
            Field("batch_size", "New emails per run", "int", group="Reading",
                  tip="The most new emails saved in one read. The rest is picked up on the next read."),
            Field("auto_sync", "Read automatically", "bool", group="Reading",
                  tip="Read this mailbox on a schedule. Off means it is only read when you press Read now."),
            Field("sync_interval", "Every (minutes)", "int", group="Reading",
                  tip="Minutes between automatic reads of this mailbox."),
            Field("group_address", "Distribution group",
                  help="Only mail sent to this group (in To or Cc). Enter its exact name, e.g. WTD - PH, "
                       "or its address, e.g. wtd@company.com. Leave empty to read all mail.",
                  group="Filters",
                  tip="Use this when the mailbox also receives mail that is not for this process: only mail "
                      "addressed to the distribution group is read."),
            Field("sender_filter", "From", help="Only mail from this address", group="Filters",
                  tip="Read mail from this one sender address only. Leave empty to read every sender."),
            Field("sender_domains", "From domains", help="Comma-separated, e.g. tlius.com", group="Filters",
                  tip="Read mail only from senders at these domains, whoever at the domain sends it. "
                      "Leave empty to read every sender."),
            Field("subject_keyword", "Subject or preview contains", group="Filters",
                  tip="Read only mail whose subject or first lines contain this word or phrase."),
            Field("skip_sender_domains", "Skip senders",
                  help="Comma-separated domains; their emails are kept without files", group="Filters",
                  tip="For senders whose attachments are never shipping documents, such as newsletters. "
                      "Their emails are listed, but the files are not downloaded or read."),
            Field("booking_number_pattern", "Booking number pattern", "pattern",
                  help="Regular expression for booking numbers in this mailbox", group="Filters",
                  tip="How a booking number looks in this mailbox's subject lines, used when no subject "
                      "format finds it. For example [A-Z]{2}\\d{6}."),
        ], {
            "Mailbox": "What this mailbox is for and which Odoo company it feeds.",
            "Outlook app": "The Azure app registration used to sign in. Save these, then connect Outlook.",
            "Reading": "Which mail is read and how often.",
            "Filters": "Mail that does not match is left in the mailbox.",
        }),
    "vendor-mappings": Form(
        "vendor_mapping", "Vendor mappings", "vendor mapping",
        ["partner_name", "role", "sender_domain", "document_name", "freight_mode", "active"], [
            Field("role", "Role", "select", [("forwarder", "Forwarder"), ("factory", "Factory")],
                  group="Vendor",
                  tip="Forwarder: arranges the transport and sends booking confirmations and freight invoices. "
                      "Factory: ships the goods and sends commercial invoices and packing lists."),
            Field("partner_id", "Odoo partner", "odoo", "partners", name_column="partner_name",
                  group="Vendor",
                  tip="The Odoo partner this vendor is. Bookings from this vendor are linked to it."),
            Field("active", "Active", "bool", group="Vendor",
                  tip="Switch off to stop using this mapping without deleting it."),
            Field("sender_domain", "Sender domain", help="For example maersk.com", group="Recognised by",
                  tip="Email from this domain is recognised as this vendor. This is the surest match."),
            Field("document_name", "Name on documents", help="As printed on their documents",
                  group="Recognised by",
                  tip="The vendor's name as printed on documents. Used when the sender doesn't identify the "
                      "vendor; such a match is flagged on the booking for a person to check."),
            Field("freight_mode", "Freight mode", "select", [("", "None"), ("ocean", "Ocean"), ("inland", "Inland")],
                  group="Freight cost",
                  tip="Ocean or inland freight. Set on bookings from this forwarder."),
            Field("freight_product_id", "Freight product", "odoo", "products", group="Freight cost",
                  tip="The Odoo product this forwarder's freight is suggested on."),
            Field("cost_per_container", "Cost per container", "float", group="Freight cost",
                  tip="This forwarder's agreed cost per container, suggested on the booking when no freight "
                      "invoice amount is known yet."),
        ], {
            "Vendor": "The Odoo partner this vendor is.",
            "Recognised by": "How an email or document is matched to this vendor.",
            "Freight cost": "Used when this forwarder's freight is booked.",
        }),
    "subject-formats": Form(
        "subject_format", "Subject formats", "subject format",
        ["name", "sequence", "sender_domains", "subject_pattern", "confirms_booking", "active"], [
            Field("name", "Name", group="Format",
                  tip="A name for this format, for example the forwarder that uses it."),
            Field("sequence", "Order", "int", help="Lower numbers are tried first", group="Format",
                  tip="Formats are tried from the lowest number up; the first one that matches the subject "
                      "is used."),
            Field("active", "Active", "bool", group="Format",
                  tip="Switch off to stop using this format without deleting it."),
            Field("sender_domains", "Sender domains", help="Comma-separated; leave empty for every sender",
                  group="Match",
                  tip="This format is only tried for mail from these domains."),
            Field("subject_pattern", "Subject pattern", "pattern",
                  help="Regular expression with named groups: " + ", ".join(sorted(SUBJECT_GROUPS)),
                  group="Match",
                  tip="Reads numbers and dates straight from the subject line, before any AI. Name each part, "
                      "for example BKG (?P<booking_number>\\d+), so the service knows what it found."),
            Field("confirms_booking", "This subject confirms the booking", "bool", group="Match",
                  tip="Mail matching this format, from a mapped forwarder, is the booking confirmation: its "
                      "values are taken as confirmed on the booking."),
        ], {
            "Format": "Formats are tried in order; the first that matches is used.",
            "Match": "Which senders use this format and how their subject lines read.",
        }),
}

REQUIRED = {"mailbox": ["name", "purpose", "company_id"], "vendor_mapping": ["role"],
            "subject_format": ["name", "subject_pattern"]}

ODOO_OPTIONS = {
    "companies": ("res.company", []),
    "partners": ("res.partner", [("supplier_rank", ">", 0)]),
    "products": ("product.product", [("purchase_ok", "=", True), ("type", "=", "service")]),
}


def sections(fields, descriptions):
    """Fields grouped in their order of appearance: [(title, description, fields)]."""
    order = []
    for field in fields:
        if field.group not in order:
            order.append(field.group)
    return [(name, descriptions.get(name, ""), [f for f in fields if f.group == name]) for name in order]


def secret_hints(fields, values):
    """For each saved secret, its last four characters; the secret itself never reaches the page."""
    return {f.name: (values.get(f.name) or "")[-4:] for f in fields
            if f.kind == "secret" and values.get(f.name)}


def _shown(value):
    return "(empty)" if value in (None, "") else str(value)


def describe_changes(fields, before, after):
    """'Label: old → new' per changed field. Secrets and prompts only say that they changed."""
    lines = []
    for f in fields:
        if f.name not in after:
            continue
        old, new = before.get(f.name), after[f.name]
        if _shown(old) == _shown(new):
            continue
        if f.kind in ("secret", "textarea"):
            lines.append(f"{f.label}: changed")
        elif f.kind == "bool":
            lines.append(f"{f.label}: {'on' if new else 'off'}")
        else:
            lines.append(f"{f.label}: {_shown(old)} → {_shown(new)}")
    return lines


def mapping_problems(form, values, options):
    """Lines saying why an edited mailbox or vendor mapping would point at the wrong record in a
    connection that is on: each Odoo id must exist, under one name, in every connection that is on."""
    wanted = {}
    for field in form.fields:
        if field.kind == "odoo" and values.get(field.name):
            wanted.setdefault(mapping_check.OPTION_MODELS[field.options], set()).add(values[field.name])
    on = [c for c in store.connections(g.db, on=True) if store.is_complete(c)]
    if not wanted or not on:
        return []
    problems, reference_odoo = [], None
    for index, connection in enumerate(on):
        try:
            odoo = Odoo.from_connection(g.db, connection)
        except Exception as exc:
            problems.append(f"{connection['name']} could not be reached to check the Odoo records. "
                            f"Try again, or turn it off first: {exc}")
            continue
        if index == 0:
            reference_odoo = odoo
        problems += mapping_check.mismatches(
            odoo, connection["name"], {m: sorted(ids) for m, ids in wanted.items()},
            None if index == 0 else reference_odoo, on[0]["name"])
    return problems


def _form(key):
    if key not in FORMS:
        abort(404)
    return FORMS[key]


def odoo_options(fields):
    """Dropdown choices loaded from Odoo, {} when Odoo cannot be reached."""
    loaders = {f.options for f in fields if f.kind == "odoo"}
    if not loaders:
        return {}
    try:
        odoo = Odoo.from_store(g.db)
        # display_name is not stored, so Odoo cannot sort on it; name is.
        return {name: [(row["id"], row["display_name"]) for row in
                       odoo.search_read(ODOO_OPTIONS[name][0], ODOO_OPTIONS[name][1], ["display_name"],
                                        order="name")]
                for name in loaders}
    except Exception as exc:
        # An Odoo fault carries the whole server traceback; its last line says what went wrong.
        g.odoo_error = (str(getattr(exc, "faultString", exc)).strip().splitlines() or ["unknown error"])[-1][:200]
        return {}


def parse_form(fields, form, options=None):
    """Form values by column, or an error. An empty secret or whole number keeps the stored one."""
    values = {}
    for field in fields:
        raw = form.get(field.name, "").strip()
        try:
            if field.kind == "bool":
                values[field.name] = 1 if field.name in form else 0
            elif field.kind in ("secret", "int"):
                if raw:
                    values[field.name] = int(raw) if field.kind == "int" else raw
            elif field.kind == "odoo":
                values[field.name] = int(raw) if raw else None
            elif field.kind == "float":
                values[field.name] = float(raw) if raw else None
            elif field.kind == "date":
                values[field.name] = f"{raw} 00:00:00" if raw else None
            else:
                values[field.name] = raw or None
        except ValueError:
            return None, f"{field.label}: enter a number."
        if field.name_column:
            names = dict((options or {}).get(field.options, []))
            values[field.name_column] = names.get(values[field.name])
    return values, None


def validate(form, values):
    labels = {f.name: f.label for f in form.fields}
    for column in REQUIRED.get(form.table, []):
        if not values.get(column):
            return f"{labels[column]} is required."
    for column in ("booking_number_pattern", "subject_pattern"):
        if values.get(column):
            try:
                pattern = re.compile(values[column])
            except re.error as exc:
                return f"{labels[column]} is not a valid regular expression: {exc}."
            if column == "subject_pattern":
                unknown = set(pattern.groupindex) - SUBJECT_GROUPS
                if unknown:
                    return (f"Unknown group(s) {', '.join(sorted(unknown))}; "
                            f"use only {', '.join(sorted(SUBJECT_GROUPS))}.")
                if not pattern.groupindex:
                    return "The subject pattern needs at least one named group, e.g. (?P<booking_number>\\w+)."
    return None


def column_defaults(table):
    """The schema's defaults, shown on a new record's form."""
    values = {}
    for column in g.db.execute(f"PRAGMA table_info({table})"):
        default = column["dflt_value"]
        if default is None or default == "CURRENT_TIMESTAMP":
            continue
        values[column["name"]] = default.strip("'") if default.startswith("'") else int(default)
    return values


def record_label(values, record_id):
    return values.get("name") or values.get("partner_name") or values.get("sender_domain") or f"#{record_id}"
