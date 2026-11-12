"""OCR service: reads Outlook, extracts the documents, writes suggestions into Odoo.

    flask --app app run        the Settings page (Docker runs it under gunicorn)
    flask --app app reset-password   forget the admin password
    flask --app app worker     run continuously
    flask --app app run-once   one pass: due mailboxes, then the new files
    flask --app app clean-up [--apply]   settle existing conflicts, re-read ready bills, freight from bills
    flask --app app payables-worker     the Freight Payables mailboxes, continuously
    flask --app app payables-run-once   one pass over the due Freight Payables mailboxes
"""

import hashlib
import logging
import secrets
from datetime import timedelta

import click
from flask import Flask

from .core import store
from .web import bp
from .worker import run_forever, run_once
from .payables import worker as payables_worker


def session_cookie_name(secret):
    """ocr_session_ and a short fingerprint of the install's secret key: one cookie per install."""
    return f"ocr_session_{hashlib.sha256(secret.encode()).hexdigest()[:8]}"


def create_app():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # The pages' templates and static files live beside them, under web/.
    app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
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
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
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

    @app.cli.command("clean-up")
    @click.option("--apply", is_flag=True, help="Write the changes; without it, only report them.")
    def clean_up_command(apply):
        """After the booking rules changed: close the conflicts they settle (see flows/cleanup.py)."""
        from .flows import cleanup
        from .odoo.client import Odoo
        passes = {"conflicts": lambda db, odoo, cid: cleanup.close_false_conflicts(db, odoo, cid, apply)}
        db = store.connect()
        try:
            for connection in store.connections(db, on=True):
                odoo = Odoo.from_connection(db, connection)
                for name in passes:
                    outcome = passes[name](db, odoo, connection["id"])
                    print(f"{connection['name']} · {name}{'' if apply else ' (report only)'}:")
                    for reason, count in outcome.most_common():
                        print(f"  {count:5}  {reason}")
        finally:
            db.close()

    @app.cli.command("import-booking-report")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    @click.option("--apply", is_flag=True, help="Write the rates; without it, only report them.")
    @click.option("--out", type=click.Path(dir_okay=False), help="Save the row-by-row result as an Excel file.")
    def import_booking_report_command(path, apply, out):
        """Write the Booking Report's agreed rate per container to each booking's Cost per Container,
        in every Odoo connection that is on (see flows/booking_report.py)."""
        from openpyxl import load_workbook

        from .flows import booking_report
        from .odoo.client import Odoo
        rows = booking_report.read_rows(load_workbook(path, read_only=True, data_only=True))
        print(f"{len(rows)} booking rows read from {path}")
        db = store.connect()
        try:
            for connection in store.connections(db, on=True):
                odoo = Odoo.from_connection(db, connection)
                outcomes, summary = booking_report.import_rates(odoo, rows, apply=apply)
                print(f"{connection['name']}{'' if apply else ' (report only, nothing written)'}:")
                for reason, count in summary.most_common():
                    print(f"  {count:5}  {reason}")
                if out:
                    booking_report.write_report(out, outcomes)
                    print(f"  row-by-row result: {out}")
        finally:
            db.close()

    @app.cli.command("payables-worker")
    def payables_worker_command():
        """Read the due Freight Payables mailboxes forever (no Odoo)."""
        payables_worker.run_payables_forever()

    @app.cli.command("payables-run-once")
    def payables_run_once_command():
        """One pass over the due Freight Payables mailboxes."""
        db = payables_worker.connect()
        try:
            payables_worker.run_payables_once(db)
        finally:
            db.close()

    return app
