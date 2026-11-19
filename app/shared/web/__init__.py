"""The pages: one blueprint, one file per shared area of the menu; each job adds its own pages from its folder
(app/<job>/pages.py, imported by create_app).

base.py runs around every request; ui.py and forms.py hold what the pages share. Importing the
area files registers their routes on the blueprint."""

from flask import Blueprint

bp = Blueprint("web", __name__)

from app.shared.web import (activity, auth, base, connections, data_pages, data_transfer, emails, odoo_records,  # noqa: E402,F401
                            search, settings, setup, ui)
