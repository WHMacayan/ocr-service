"""The pages: one blueprint, one file per area of the menu.

base.py runs around every request; ui.py and forms.py hold what the pages share. Importing the
area files registers their routes on the blueprint."""

from flask import Blueprint

bp = Blueprint("web", __name__)

from . import (activity, auth, base, bookings, connections, emails, odoo_records, payables,  # noqa: E402,F401
               search, settings, setup, ui)
