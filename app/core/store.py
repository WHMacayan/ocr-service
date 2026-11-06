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
        _move_single_connection(db)
    return db


# Columns added to tables after their first release: CREATE TABLE IF NOT EXISTS leaves existing tables as they are.
ADDED_COLUMNS = [("message", "processed_at", "TEXT"), ("odoo_change", "values_json", "TEXT"),
                 ("mailbox", "sender_domains", "TEXT"),
                 ("odoo_change", "connection_id", "INTEGER REFERENCES odoo_connection(id) ON DELETE CASCADE")]

# The one Odoo connection the Settings page held before there were several.
OLD_CONNECTION_SETTINGS = ("odoo_url", "odoo_db", "odoo_user", "odoo_api_key")


def _move_single_connection(db):
    """The Settings page's one connection becomes a connection named Production, on when complete,
    with every email's history so far. Runs inside connect()'s transaction: all of it or none."""
    marks = ", ".join("?" * len(OLD_CONNECTION_SETTINGS))
    old = {row["key"]: row["value"] for row in db.execute(
        f"SELECT key, value FROM setting WHERE key IN ({marks})", OLD_CONNECTION_SETTINGS)}
    if not old:
        return
    if any(old.values()) and not db.execute("SELECT 1 FROM odoo_connection").fetchone():
        url, database, login, api_key = (old.get(key) or None for key in OLD_CONNECTION_SETTINGS)
        # The API key was encrypted with the same secret.key: it moves as it is.
        connection_id = db.execute(
            "INSERT INTO odoo_connection (name, url, database, login, api_key, is_production, active) "
            "VALUES ('Production', ?, ?, ?, ?, 1, ?)",
            (url, database, login, api_key, int(all((url, database, login, api_key))))).lastrowid
        db.execute("INSERT INTO message_target (message_id, connection_id, state, attempts, note, email_done, "
                   "processed_at) SELECT id, ?, state, attempts, note, email_done, processed_at FROM message "
                   "WHERE state != 'skipped'", (connection_id,))
        db.execute("INSERT INTO file_target (file_id, connection_id, state, note, odoo_id) "
                   "SELECT id, ?, state, note, odoo_id FROM file WHERE state != 'new'", (connection_id,))
        db.execute("UPDATE odoo_change SET connection_id = ? WHERE connection_id IS NULL", (connection_id,))
    db.execute(f"DELETE FROM setting WHERE key IN ({marks})", OLD_CONNECTION_SETTINGS)


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


# ── Odoo connections ─────────────────────────────────────────────────

CONNECTION_FIELDS = ("url", "database", "login", "api_key")
# Production first: it is written first and is the one the pages read Odoo through.
CONNECTION_ORDER = "is_production DESC, name COLLATE NOCASE, id"


def connection(db, connection_id):
    """One connection as a dict with its API key decrypted."""
    row = db.execute("SELECT * FROM odoo_connection WHERE id = ?", (connection_id,)).fetchone()
    if not row:
        return None
    return dict(row, api_key=decrypt(db, row["api_key"]))


def connections(db, on=False):
    """Every connection, or only those switched on, in writing order, API keys decrypted."""
    return [connection(db, row["id"]) for row in db.execute(
        f"SELECT id FROM odoo_connection {'WHERE active = 1' if on else ''} ORDER BY {CONNECTION_ORDER}")]


def is_complete(values):
    """All four details filled in. An incomplete connection is never used and never complains."""
    return all((values.get(field) or "").strip() for field in CONNECTION_FIELDS)


def reference_connection(db):
    """The connection the pages read Odoo through and link to: the first production connection
    that is on, else the first connection that is on; None when none is on."""
    on = connections(db, on=True)
    return on[0] if on else None


def reference_changes(db, alias="c"):
    """SQL condition and parameters keeping the odoo_change rows of the reference connection, and
    those recorded before connections were. Every row when no connection is on."""
    reference = reference_connection(db)
    if not reference:
        return "1 = 1", ()
    return f"({alias}.connection_id IS NULL OR {alias}.connection_id = ?)", (reference["id"],)


def save_connection(db, connection_id, values):
    """Write connection fields, encrypting the API key. Returns the connection's id."""
    values = {key: encrypt(db, value) if key == "api_key" else value for key, value in values.items()}
    with db:
        if connection_id:
            if values:
                db.execute(f"UPDATE odoo_connection SET {', '.join(f'{key} = ?' for key in values)} WHERE id = ?",
                           (*values.values(), connection_id))
            return connection_id
        return db.execute(f"INSERT INTO odoo_connection ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                          tuple(values.values())).lastrowid


# Which of an email's states the lists show when its connections disagree: the one needing a person first.
SUMMARY_ORDER = ("failed", "review", "new", "done")


def refresh_messages(db, message_ids=None):
    """Bring the summary on message and file in line with their state per connection: the email's
    state is the one most in need of a person, its files show what happened in the reference connection.
    A connection that is on but has not processed a due email yet counts as Queued. No commit."""
    on = db.execute(f"SELECT id, name, start_from FROM odoo_connection WHERE active = 1 "
                    f"ORDER BY {CONNECTION_ORDER}").fetchall()
    names = dict(db.execute("SELECT id, name FROM odoo_connection").fetchall())
    ranks = {row["id"]: rank for rank, row in enumerate(on)}
    if message_ids is None:
        message_ids = [row[0] for row in db.execute("SELECT id FROM message")]
    for message_id in message_ids:
        message = db.execute("SELECT state, received FROM message WHERE id = ?", (message_id,)).fetchone()
        if not message:
            continue
        targets = db.execute("SELECT * FROM message_target WHERE message_id = ?", (message_id,)).fetchall()
        if message["state"] == "skipped" and not targets:
            continue  # skipped when it was read: never processed anywhere
        done_in = {t["connection_id"] for t in targets}
        states = [t["state"] for t in targets] + [
            "new" for c in on if c["id"] not in done_in and (message["received"] or "") >= (c["start_from"] or "")]
        if not states:
            continue
        notes = [(names.get(t["connection_id"], "?"), t["note"]) for t in targets if t["note"]]
        note = notes[0][1] if len(targets) == 1 and notes else "\n".join(f"{name}: {text}" for name, text in notes)
        db.execute("UPDATE message SET state = ?, note = ?, attempts = ?, email_done = ? WHERE id = ?",
                   (next((s for s in SUMMARY_ORDER if s in states), states[0]), note or None,
                    max((t["attempts"] for t in targets), default=0),
                    int(bool(targets) and all(t["email_done"] for t in targets)), message_id))
        for file in db.execute("SELECT id FROM file WHERE message_id = ?", (message_id,)).fetchall():
            shown = min(db.execute("SELECT * FROM file_target WHERE file_id = ?", (file["id"],)).fetchall(),
                        key=lambda t: ranks.get(t["connection_id"], len(ranks)), default=None)
            if shown:
                db.execute("UPDATE file SET state = ?, note = ?, odoo_id = ? WHERE id = ?",
                           (shown["state"], shown["note"], shown["odoo_id"], file["id"]))
