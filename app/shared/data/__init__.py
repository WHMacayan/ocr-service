"""Import and export of records, as Odoo's: the fields to export are picked, a file is mapped column by column to the
fields of a record, tested, then applied to create and update records.

A page's records are a DataSet. Each job describes its own (app/<job>/data.py, listed in its Job's ``datasets``); the
shared pages (mailboxes, vendor mappings, subject formats, emails, activity) are described here (sqlite.py). The engine
below (files, mapping, planning) names no job.

    convert.py  cell values to and from the types a record holds
    tables.py   Excel and CSV files
    sqlite.py   data sets over the service's own tables (setup lists, emails, activity)
    odoo.py     data sets over Odoo models (bookings, scraped bills, customs)
"""

import re
from dataclasses import dataclass, field
from importlib import import_module

from app.shared.odoo.client import Odoo


@dataclass(frozen=True)
class Column:
    """A field a record has, as it is exported and imported."""
    name: str               # the key in a record's values
    label: str              # the file's column header
    kind: str = "char"      # char text int float bool date datetime select many2one list
    importable: bool = True
    default: bool = False   # picked when the export opens for the first time
    options: tuple = ()     # select: ((key, label), ...)
    relation: str = ""      # many2one: the Odoo model
    required: bool = False
    note: str = ""          # a hint shown beside the field


@dataclass
class RowPlan:
    """What one row of an import would do, or did."""
    number: int                                   # the row's line in the file (the header is line 1)
    action: str = "error"                         # create | update | unchanged | error
    record_id: int = 0
    label: str = ""                               # which record, in words
    values: dict = field(default_factory=dict)    # what would be written
    changes: list = field(default_factory=list)   # [(column label, old, new)] for an update
    errors: list = field(default_factory=list)
    done: bool = False


@dataclass
class Plan:
    rows: list
    problems: list = field(default_factory=list)  # reasons that stop the whole import (not tied to one row)

    def count(self, action):
        return sum(1 for r in self.rows if r.action == action)

    @property
    def errors(self):
        return sum(1 for r in self.rows if r.action == "error")


class Context:
    """What a data set works with: the service's database, and Odoo through the connection it is told to use (the
    one the pages read when none is named)."""

    def __init__(self, db, connection=None):
        self.db = db
        self.connection = connection
        self._odoo = None
        self.cache = {}  # what a request reads once: an Odoo model's fields, the names it looked up

    def odoo(self):
        if self._odoo is None:
            self._odoo = Odoo.from_connection(self.db, self.connection) if self.connection else Odoo.from_store(self.db)
        return self._odoo


class DataSet:
    """One list's records. A subclass says how to read them, and, when ``importable``, how to match, check and write."""

    key = ""
    label = ""
    singular = ""
    endpoint = ""           # the page that lists them (a Flask endpoint), for the breadcrumb
    endpoint_args = {}
    importable = False
    creatable = False       # an import may add records, not only update
    max_rows = 5000         # the most rows one import takes
    writes_to = "service"   # service | odoo: where an import writes

    def columns(self, ctx):
        raise NotImplementedError

    def default_names(self, ctx):
        return [c.name for c in self.columns(ctx) if c.default]

    def rows(self, ctx, names, filters):
        """[{column name: value as a file shows it}] of the records (those the page's filters keep), with ``names``."""
        raise NotImplementedError

    def plan(self, ctx, rows):
        """Plan of an import: ``rows`` is [(line number, {column name: cell})]. Nothing is written."""
        raise NotImplementedError

    def apply(self, ctx, plan):
        """Write the rows of ``plan`` that have no error; each row's ``done`` says whether it was written."""
        raise NotImplementedError

    def target(self, ctx):
        """Where an import writes, in words."""
        return "the OCR service's own settings"


# ── the registry ─────────────────────────────────────────────────────

def all_datasets():
    """Every data set, the shared ones first, then each job's (its Job's ``datasets`` module)."""
    from app.shared.data import sqlite
    from app.shared.jobs import all_jobs
    found = list(sqlite.datasets())
    for job in all_jobs():
        if job.datasets:
            found += list(import_module(job.datasets).DATASETS)
    return found


def dataset(key):
    return next((d for d in all_datasets() if d.key == key), None)


# ── mapping a file's columns to a data set's ─────────────────────────

def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def auto_map(headers, columns):
    """For each header, the name of the column it most likely is, else "" (not imported): the same label or name, however
    it is cased or spaced. A column is taken once."""
    importable = [c for c in columns if c.importable]
    by_text = {}
    for c in importable:
        by_text.setdefault(_norm(c.label), c.name)
        by_text.setdefault(_norm(c.name), c.name)
    taken, mapped = set(), []
    for header in headers:
        name = by_text.get(_norm(header), "")
        if name in taken:
            name = ""
        if name:
            taken.add(name)
        mapped.append(name)
    return mapped


def mapped_rows(rows, mapping):
    """[(line number, {column name: cell})] from the file's rows and its column mapping (a list parallel to the headers)."""
    out = []
    for index, row in enumerate(rows, 2):
        picked = {name: row[i] for i, name in enumerate(mapping) if name and i < len(row)}
        out.append((index, picked))
    return out
