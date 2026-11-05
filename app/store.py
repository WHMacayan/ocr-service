"""The service's own state: one SQLite file and the source files on the data volume.

The volume also holds secret.key, made on first start, which encrypts the
secrets kept in SQLite (API keys, the Outlook client secret and tokens).
"""

import os
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet

from . import defaults
from .settings import SECRET_SETTINGS

SCHEMA = Path(__file__).with_name("schema.sql")
SECRET_MAILBOX_FIELDS = ("client_secret", "refresh_token", "access_token")


def data_dir():
    return Path(os.environ.get("OCR_DATA_DIR", "/data"))


class Connection(sqlite3.Connection):
    fernet = None
    directory = None


def _fernet(directory):
    key_file = directory / "secret.key"
    if not key_file.exists():
        # Web and worker start together: write aside, then link, so only one key wins.
        draft = directory / f"secret.key.{os.getpid()}"
        draft.write_bytes(Fernet.generate_key())
        draft.chmod(0o600)
        try:
            os.link(draft, key_file)
        except FileExistsError:
            pass
        finally:
            draft.unlink()
    return Fernet(key_file.read_bytes())


def connect(path=None):
    """Open the database, creating the tables and default settings when missing."""
    path = Path(path) if path else data_dir() / "ocr.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, factory=Connection)
    db.row_factory = sqlite3.Row
    db.directory = path.parent
    db.fernet = _fernet(path.parent)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    with db:
        db.executescript(SCHEMA.read_text())
        for table, column, kind in ADDED_COLUMNS:
            if column not in {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        db.executemany("INSERT OR IGNORE INTO setting (key, value) VALUES (?, ?)",
                       defaults.DEFAULT_SETTINGS.items())
    return db


# Columns added to tables after their first release: CREATE TABLE IF NOT EXISTS leaves existing tables as they are.
ADDED_COLUMNS = [("message", "processed_at", "TEXT"), ("odoo_change", "values_json", "TEXT"),
                 ("mailbox", "sender_domains", "TEXT")]


def files_dir(db):
    """Where source files are kept: next to the database file."""
    return db.directory / "files"


def encrypt(db, value):
    return db.fernet.encrypt(value.encode()).decode() if value else value


def decrypt(db, value):
    return db.fernet.decrypt(value.encode()).decode() if value else value


# ── settings ─────────────────────────────────────────────────────────

def get_setting(db, key, cast=str):
    """A setting's value, decrypted; None when missing or empty."""
    row = db.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    if not row or row["value"] in (None, ""):
        return None
    value = decrypt(db, row["value"]) if key in SECRET_SETTINGS else row["value"]
    return cast(value)


def set_setting(db, key, value):
    value = "" if value is None else str(value)
    if key in SECRET_SETTINGS:
        value = encrypt(db, value)
    with db:
        db.execute("INSERT INTO setting (key, value) VALUES (?, ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))


# ── activity log ─────────────────────────────────────────────────────

ACTIVITY_KINDS = ("settings", "security", "processing", "odoo")


def log_activity(db, kind, summary, detail=None, level="info", actor="worker", link=None):
    """One entry in the activity log. Never raises: a full log must not stop the work."""
    try:
        with db:
            db.execute("INSERT INTO activity (kind, level, actor, summary, detail, link) VALUES (?, ?, ?, ?, ?, ?)",
                       (kind, level, actor, summary[:500], (detail or None) and detail[:4000], link))
    except sqlite3.Error:
        pass


def prune_activity(db):
    days = get_setting(db, "activity_days", int)
    if days:
        with db:
            db.execute("DELETE FROM activity WHERE at < datetime('now', ?)", (f"-{days} days",))


# ── mailboxes ────────────────────────────────────────────────────────

def mailbox(db, mailbox_id):
    """One mailbox as a dict with its secrets decrypted."""
    row = db.execute("SELECT * FROM mailbox WHERE id = ?", (mailbox_id,)).fetchone()
    if not row:
        return None
    values = dict(row)
    for field in SECRET_MAILBOX_FIELDS:
        values[field] = decrypt(db, values[field])
    return values


def active_mailboxes(db):
    return [mailbox(db, row["id"]) for row in db.execute("SELECT id FROM mailbox WHERE active = 1")]


def update_mailbox(db, mailbox_id, values):
    """Write mailbox fields, encrypting the secret ones."""
    values = {key: encrypt(db, value) if key in SECRET_MAILBOX_FIELDS else value
              for key, value in values.items()}
    if not values:
        return
    with db:
        db.execute(f"UPDATE mailbox SET {', '.join(f'{key} = ?' for key in values)} WHERE id = ?",
                   (*values.values(), mailbox_id))
