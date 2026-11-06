"""One search over everything the page shows: emails (with their files and what was read from
them), mailboxes, vendor mappings, subject formats, settings, the activity log and the pages.

Query syntax: every word must match (in any field); "quoted words" match as a phrase;
state:failed, from:maersk.com, mailbox:bookings narrow emails; in:emails (or mailboxes,
vendors, formats, settings, activity, pages) keeps one kind of result.
"""

import re
from collections import namedtuple

from flask import g, render_template, request

from . import bp
from .base import STATE_LABELS
from ..core.settings import SETTINGS

# What the Odoo connections section of the Settings page is found by.
CONNECTIONS_TEXT = "odoo connections address url database login user api key production staging development test"

Query = namedtuple("Query", "terms filters")
Item = namedtuple("Item", "title sub url badge")

FILTERS = {"state", "from", "mailbox", "in"}
GROUPS = {"emails": "Emails", "mailboxes": "Mailboxes", "vendors": "Vendor mappings", "formats": "Subject formats",
          "settings": "Settings", "activity": "Activity", "pages": "Pages"}
GROUP_ALIASES = {"email": "emails", "mail": "emails", "mailbox": "mailboxes", "vendor": "vendors",
                 "vendor-mappings": "vendors", "mappings": "vendors", "format": "formats", "subject-formats": "formats",
                 "setting": "settings", "log": "activity", "page": "pages"}
STATE_WORDS = {"queued": "new", "new": "new", "review": "review", "needs review": "review", "done": "done",
               "processed": "done", "failed": "failed", "skipped": "skipped"}
PAGES = [("Emails", "/messages"), ("Activity", "/activity"), ("Mailboxes", "/mailboxes/"),
         ("Vendor mappings", "/vendor-mappings/"), ("Subject formats", "/subject-formats/"), ("Settings", "/settings"),
         ("Change password", "/password"), ("Add mailbox", "/mailboxes/new"),
         ("Add vendor mapping", "/vendor-mappings/new"), ("Add subject format", "/subject-formats/new")]


def parse(text):
    """Words, "phrases" and key:value filters from the search box."""
    terms, filters = [], {}
    for token in re.findall(r'\w+:"[^"]*"|"[^"]*"|\S+', text or ""):
        key, sep, value = token.partition(":")
        if sep and key.lower() in FILTERS and value:
            filters[key.lower()] = value.strip('"')
        elif token.strip('"'):
            terms.append(token.strip('"'))
    return Query(terms, filters)


def _like(term):
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _match(columns, terms):
    """SQL requiring every term to appear in at least one of the columns, and its parameters."""
    clauses, params = [], []
    for term in terms:
        clauses.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in columns) + ")")
        params += [_like(term)] * len(columns)
    return " AND ".join(clauses) or "1", params


def _wanted(query, group):
    only = query.filters.get("in")
    return not only or GROUP_ALIASES.get(only.lower(), only.lower()) == group


def email_where(query):
    """WHERE clause and parameters for emails (aliases m = message, b = mailbox)."""
    files = ("EXISTS (SELECT 1 FROM file f WHERE f.message_id = m.id AND ("
             "f.filename LIKE ? ESCAPE '\\' OR f.doc_class LIKE ? ESCAPE '\\' OR f.extracted LIKE ? ESCAPE '\\' "
             "OR f.note LIKE ? ESCAPE '\\'))")
    odoo = ("EXISTS (SELECT 1 FROM odoo_change c WHERE c.message_id = m.id AND "
            "(c.label LIKE ? ESCAPE '\\' OR c.detail LIKE ? ESCAPE '\\'))")
    clauses, params = [], []
    for term in query.terms:
        cols = ["m.subject", "m.sender", "m.body_text", "m.note", "m.skipped_attachments", "b.name"]
        clauses.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in cols) + f" OR {files} OR {odoo})")
        params += [_like(term)] * (len(cols) + 6)
    state = query.filters.get("state")
    if state:
        clauses.append("m.state = ?")
        params.append(STATE_WORDS.get(state.lower().replace("-", " "), state.lower()))
    if query.filters.get("from"):
        clauses.append("m.sender LIKE ? ESCAPE '\\'")
        params.append(_like(query.filters["from"]))
    if query.filters.get("mailbox"):
        clauses.append("b.name LIKE ? ESCAPE '\\'")
        params.append(_like(query.filters["mailbox"]))
    return " AND ".join(clauses) or "1", params


def run(db, text, limit=6, state_labels=None):
    """[(group key, label, [Item], more)] for every group with a match."""
    query = parse(text)
    if not query.terms and not query.filters:
        return []
    state_labels = state_labels or {}
    groups = []

    def add(key, items, total):
        if items:
            groups.append((key, GROUPS[key], items, max(total - len(items), 0)))

    email_filters = {k: v for k, v in query.filters.items() if k != "in"}
    if _wanted(query, "emails"):
        where, params = email_where(query)
        sql = f"FROM message m JOIN mailbox b ON b.id = m.mailbox_id WHERE {where}"
        total = db.execute(f"SELECT COUNT(*) {sql}", params).fetchone()[0]
        rows = db.execute(f"SELECT m.id, m.subject, m.sender, m.received, m.state, b.name AS mailbox {sql} "
                          "ORDER BY m.received DESC, m.id DESC LIMIT ?", (*params, limit)).fetchall()
        add("emails", [Item(r["subject"] or "No subject", f"{r['sender']}, {r['received'] or ''} in {r['mailbox']}",
                            f"/messages/{r['id']}", (r["state"], state_labels.get(r["state"], r["state"])))
                       for r in rows], total)

    if email_filters and not query.terms:
        return groups  # email filters alone mean: show emails

    def table(key, sql_from, columns, title, sub, url, order="id"):
        if not _wanted(query, key):
            return
        where, params = _match(columns, query.terms)
        total = db.execute(f"SELECT COUNT(*) FROM {sql_from} WHERE {where}", params).fetchone()[0]
        rows = db.execute(f"SELECT * FROM {sql_from} WHERE {where} ORDER BY {order} LIMIT ?",
                          (*params, limit)).fetchall()
        add(key, [Item(title(r), sub(r), url(r), None) for r in rows], total)

    table("mailboxes", "mailbox",
          ["name", "folder", "group_address", "sender_filter", "subject_keyword", "skip_sender_domains", "purpose"],
          lambda r: r["name"], lambda r: f"{r['purpose'].replace('_', ' ').title()}, {r['folder'] or 'all folders'}",
          lambda r: f"/mailboxes/{r['id']}")
    table("vendors", "vendor_mapping", ["partner_name", "sender_domain", "document_name", "role", "freight_mode"],
          lambda r: r["partner_name"] or r["document_name"] or f"#{r['id']}",
          lambda r: ", ".join(x for x in (r["role"].title(), r["sender_domain"], r["document_name"]) if x),
          lambda r: f"/vendor-mappings/{r['id']}")
    table("formats", "subject_format", ["name", "sender_domains", "subject_pattern"],
          lambda r: r["name"], lambda r: r["subject_pattern"], lambda r: f"/subject-formats/{r['id']}", "sequence, id")
    table("activity", "activity", ["summary", "detail", "actor"],
          lambda r: r["summary"], lambda r: f"{r['at']} UTC", lambda r: r["link"] or "/activity", "id DESC")

    if _wanted(query, "settings"):
        words = [t.lower() for t in query.terms]
        hits = [Item(s.label, f"{s.group} settings", f"/settings#{s.key}", None) for s in SETTINGS
                if all(w in f"{s.label} {s.help} {s.tip} {s.group} {s.key}".lower() for w in words)]
        if all(w in CONNECTIONS_TEXT for w in words):
            hits.insert(0, Item("Odoo connections", "Settings", "/settings", None))
        hits += [Item(r["name"], f"Odoo connection, {'on' if r['active'] else 'off'}", f"/connections/{r['id']}", None)
                 for r in db.execute("SELECT id, name, url, database, login, active FROM odoo_connection ORDER BY name")
                 if all(w in f"{r['name']} {r['url']} {r['database']} {r['login']} odoo connection".lower() for w in words)]
        add("settings", hits[:limit], len(hits))
    if _wanted(query, "pages"):
        hits = [p for p in PAGES if all(t.lower() in p[0].lower() for t in query.terms)]
        add("pages", [Item(title, "Go to page", url, None) for title, url in hits[:limit]], len(hits))
    return groups


@bp.route("/search")
def search_page():
    """Everything matching the search box: a page, or JSON for the quick """
    q = (request.args.get("q") or "").strip()
    if request.args.get("format") == "json":
        groups = run(g.db, q, limit=6, state_labels=STATE_LABELS)
        return {"query": q, "terms": parse(q).terms,
                "groups": [{"key": key, "label": label, "more": more,
                            "items": [item._asdict() for item in items]} for key, label, items, more in groups]}
    groups = run(g.db, q, limit=50, state_labels=STATE_LABELS)
    return render_template("search/search.html", title="Search", q=q, groups=groups, terms=parse(q).terms)
