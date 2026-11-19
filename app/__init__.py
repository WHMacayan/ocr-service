"""OCR service: reads Outlook, extracts the documents, writes suggestions into Odoo.

    flask --app app run        the Settings page (Docker runs it under gunicorn)
    flask --app app reset-password   forget the admin password
    flask --app app worker     run continuously
    flask --app app run-once   one pass: due mailboxes, then the new files

Each job adds its own commands (flask --app app --help lists them): Freight Booking clean-up, import-booking-report
and fill-bookings; Freight Payables payables-worker and payables-run-once.
"""

import hashlib
import logging
import secrets
from importlib import import_module
from datetime import timedelta
from pathlib import Path

import click
from flask import Flask
from jinja2 import ChoiceLoader, FileSystemLoader

from app.shared.core import store
from app.shared.web import bp
from app.shared.jobs import all_jobs
from app.shared.worker import run_forever, run_once


def session_cookie_name(secret):
    """ocr_session_ and a short fingerprint of the install's secret key: one cookie per install."""
    return f"ocr_session_{hashlib.sha256(secret.encode()).hexdigest()[:8]}"


def create_app():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # The shared pages' templates and static files live under shared/web/; each job keeps its own templates in its
    # folder (freight_booking/templates/freight_booking/...), found after the shared ones.
    app = Flask(__name__, template_folder="shared/web/templates", static_folder="shared/web/static")
    here = Path(__file__).parent
    app.jinja_loader = ChoiceLoader([app.jinja_loader] + [FileSystemLoader(str(path)) for path in sorted(here.glob("*/templates"))])
    db = store.connect()
    with db:
        db.execute("INSERT OR IGNORE INTO setting (key, value) VALUES ('web_secret', ?)",
                   (secrets.token_hex(32),))
    app.secret_key = store.get_setting(db, "web_secret")
    db.close()
    app.config.update(
        # A name of this install's own: browsers share cookies across ports, so another local app, or
        # another copy of this one, would otherwise replace the login cookie and sign you out.
        SESSION_COOKIE_NAME=session_cookie_name(app.secret_key),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Signed in for 30 days from the last visit: each visit renews it, so a browser in use stays signed in.
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        # Uploads: the Booking Report and setup workbooks, a backup file (the Import / Export page).
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
    )
    for job in all_jobs():  # each job's pages, from its folder, before the blueprint is registered
        if job.pages:
            import_module(job.pages)
    app.register_blueprint(bp)

    @app.cli.command("reset-password")
    def reset_password_command():
        """Forget the admin password: the page asks for a new one on its next visit."""
        db = store.connect()
        store.set_setting(db, "admin_password_hash", None)
        store.set_setting(db, "session_version", None)  # signs out every open browser
        store.log_activity(db, "security", "Admin password cleared from the command line", actor="command line",
                           level="warning")
        db.close()
        print("Admin password cleared. Open the page to choose a new one.")

    @app.cli.command("worker")
    def worker_command():
        """Read due mailboxes and process files forever."""
        run_forever()

    @app.cli.command("run-once")
    def run_once_command():
        """One pass over the due mailboxes and the new files."""
        db = store.connect()
        try:
            run_once(db)
        finally:
            db.close()

    for job in all_jobs():  # each job adds its own commands (freight_booking/commands.py, payables/commands.py)
        if job.commands:
            job.commands(app.cli)

    return app
