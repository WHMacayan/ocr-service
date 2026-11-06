"""OCR service: reads Outlook, extracts the documents, writes suggestions into Odoo.

    flask --app app run        the Settings page (Docker runs it under gunicorn)
    flask --app app reset-password   forget the admin password
    flask --app app worker     run continuously
    flask --app app run-once   one pass: due mailboxes, then the new files
"""

import logging
import secrets
from datetime import timedelta

from flask import Flask

from .core import store
from .web import bp
from .worker import run_forever, run_once


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
        # Its own name: browsers share cookies across ports, so another local app's "session"
        # cookie would otherwise replace this one and sign you out.
        SESSION_COOKIE_NAME="ocr_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Signed in for a week from the last visit (each visit renews it).
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
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

    return app
