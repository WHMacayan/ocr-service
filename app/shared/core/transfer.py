"""Moving the service's data in and out (the Import / Export page).

- Setup as Excel: vendor mappings, subject formats and plain settings, to edit in bulk or carry to another
  install. Rows are matched by what they are (a mapping by role, name and sender domain; a subject format
  by name; a setting by key), not by id, so a workbook from another machine lands on the right rows.
- Full backup: every setup table as one JSON file. Secrets (Outlook client secrets and tokens, Odoo API keys,
  the OpenAI key) stay encrypted: they open again only with this install's secret.key, so a restore on another
  machine needs that key file copied too, or the secrets entered again.

An import or a restore first reports what it would change; nothing is written unless ``apply``.
"""

import io
import json
from collections import Counter
from datetime import datetime, timezone

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from app.shared.core import store
from app.shared.core.settings import SECRET_SETTINGS

BACKUP_VERSION = 1
# Never carried: this install's own sign-in and session secrets, and the worker's bookkeeping.
PRIVATE_SETTINGS = {"admin_password_hash", "web_secret", "session_version", "fill_last_at"}
_TIMES = ("id", "created_at", "updated_at")

# sheet name, table, the columns that say which row it is
SETUP_SHEETS = (("Vendor mappings", "vendor_mapping", ("role", "document_name", "sender_domain")),
                ("Subject formats", "subject_format", ("name",)),
                ("Settings", "setting", ("key",)))
# table, the columns that say which row it is; payables_setting follows its mailbox (by name)
BACKUP_TABLES = (("setting", ("key",)), ("odoo_connection", ("name",)), ("mailbox", ("name",)),
                 ("vendor_mapping", ("role", "document_name", "sender_domain")), ("subject_format", ("name",)),
                 ("payables_setting", ("mailbox_id",)))


def _columns(db, table):
    return [r["name"] for r in db.execute(f"PRAGMA table_info({table})")]


def _norm(value):
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip() if isinstance(value, str) else value
    return None if text == "" else text


def _key(row, keys):
    return tuple(str(_norm(row.get(k)) or "").upper() for k in keys)


def _plain_settings(db):
    return [dict(r) for r in db.execute("SELECT key, value FROM setting ORDER BY key")
            if r["key"] not in SECRET_SETTINGS and r["key"] not in PRIVATE_SETTINGS
            and not r["key"].startswith("export_fields.")]  # a last field pick is a preference, not setup


# ── setup as Excel ───────────────────────────────────────────────────

def export_setup(db):
    """The setup workbook as bytes."""
    wb = Workbook()
    wb.remove(wb.active)
    for sheet, table, _keys in SETUP_SHEETS:
        ws = wb.create_sheet(sheet)
        if table == "setting":
            columns, rows = ["key", "value"], _plain_settings(db)
        else:
            columns = [c for c in _columns(db, table) if c not in _TIMES]
            rows = [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY id")]
        ws.append(columns)
        for cell in ws[1]:
            cell.font = Font(name="Arial", bold=True)
        for row in rows:
            ws.append([row.get(c) for c in columns])
        for i, column in enumerate(columns, 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = max(12, min(40, len(column) + 4))
        ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def import_setup(db, data, apply=False):
    """(summary Counter, notes) for a setup workbook; writes only when ``apply``."""
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    summary, notes = Counter(), []
    for sheet, table, keys in SETUP_SHEETS:
        if sheet not in wb.sheetnames:
            continue
        rows = list(wb[sheet].iter_rows(values_only=True))
        if not rows:
            continue
        head = [str(h).strip() if h is not None else "" for h in rows[0]]
        allowed = set(_columns(db, table)) - set(_TIMES)
        unknown = [h for h in head if h and h not in allowed]
        if unknown:
            notes.append(f"{sheet}: columns not known here and left out: {', '.join(unknown)}.")
        if any(k not in head for k in keys):
            notes.append(f"{sheet}: needs the columns {', '.join(keys)}; the sheet was skipped.")
            continue
        current = {(_key(dict(r), keys)): dict(r) for r in db.execute(f"SELECT * FROM {table}")}
        for number, values in enumerate(rows[1:], 2):
            row = {h: _norm(v) for h, v in zip(head, values) if h in allowed}
            if not any(v is not None for v in row.values()):
                continue
            if table == "setting" and (row.get("key") in SECRET_SETTINGS or row.get("key") in PRIVATE_SETTINGS):
                notes.append(f"{sheet} row {number}: {row.get('key')} is set on the Settings page only; skipped.")
                continue
            if not all(row.get(k) for k in keys if k != "sender_domain"):
                summary[f"{sheet}: rows skipped (missing {', '.join(keys)})"] += 1
                continue
            existing = current.get(_key(row, keys))
            if existing:
                changed = {c: v for c, v in row.items() if _norm(existing.get(c)) != v}
                if not changed:
                    summary[f"{sheet}: unchanged"] += 1
                    continue
                summary[f"{sheet}: changed"] += 1
                if apply:
                    where = " AND ".join(f"COALESCE({k}, '') = COALESCE(?, '')" for k in keys)
                    db.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in changed)} WHERE {where}",
                               (*changed.values(), *(existing.get(k) for k in keys)))
            else:
                summary[f"{sheet}: added"] += 1
                if apply:
                    db.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                               tuple(row.values()))
    if apply:
        db.commit()
    return summary, notes


# ── full backup ──────────────────────────────────────────────────────

def backup(db):
    """Every setup table as a JSON-ready dict; secrets as stored (encrypted)."""
    tables = {}
    for table, _keys in BACKUP_TABLES:
        rows = [dict(r) for r in db.execute(f"SELECT * FROM {table}")]
        if table == "setting":
            rows = [r for r in rows if r["key"] not in PRIVATE_SETTINGS]
        tables[table] = rows
    names = {r["id"]: r["name"] for r in tables["mailbox"]}
    for row in tables["payables_setting"]:
        row["mailbox_name"] = names.get(row["mailbox_id"])
    return {"service": "ocr-service", "version": BACKUP_VERSION,
            "made_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), "tables": tables}


def backup_bytes(db):
    return json.dumps(backup(db), indent=1, default=str).encode()


def restore(db, data, apply=False):
    """(summary Counter, notes) for a backup file; writes only when ``apply``. Rows are matched by name or
    key; a row of this install that the backup lacks is kept."""
    try:
        payload = json.loads(data)
    except ValueError:
        return Counter(), ["This is not a backup file from the OCR service."]
    if payload.get("service") != "ocr-service" or not isinstance(payload.get("tables"), dict):
        return Counter(), ["This is not a backup file from the OCR service."]
    summary, notes = Counter(), []
    if payload.get("version", 0) > BACKUP_VERSION:
        notes.append("The backup was made by a newer version of the service; unknown columns are left out.")
    mailbox_ids = {}
    for table, keys in BACKUP_TABLES:
        rows = payload["tables"].get(table) or []
        columns = set(_columns(db, table)) - set(_TIMES)
        if table == "payables_setting":
            columns |= {"mailbox_id"}
        current = {_key(dict(r), keys): dict(r) for r in db.execute(f"SELECT * FROM {table}")}
        for row in rows:
            if table == "setting" and row.get("key") in PRIVATE_SETTINGS:
                continue
            if table == "payables_setting":
                mailbox_id = mailbox_ids.get(row.get("mailbox_name"))
                if not mailbox_id:
                    summary["payables settings skipped (mailbox not found)"] += 1
                    continue
                row = dict(row, mailbox_id=mailbox_id)
            values = {c: row.get(c) for c in columns if c in row}
            existing = current.get(_key(values, keys))
            if existing:
                changed = {c: v for c, v in values.items() if existing.get(c) != v}
                summary[f"{table}: {'changed' if changed else 'unchanged'}"] += 1
                if apply and changed:
                    where = " AND ".join(f"COALESCE({k}, '') = COALESCE(?, '')" for k in keys)
                    db.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in changed)} WHERE {where}",
                               (*changed.values(), *(existing.get(k) for k in keys)))
            else:
                summary[f"{table}: added"] += 1
                if apply:
                    db.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                               tuple(values.values()))
            if table == "mailbox" and apply:
                mailbox_ids[row["name"]] = db.execute("SELECT id FROM mailbox WHERE name = ?", (row["name"],)).fetchone()[0]
            elif table == "mailbox":
                mailbox_ids[row["name"]] = (existing or {}).get("id") or -1
    if apply:
        db.commit()
    if not _secrets_open(db, payload):
        notes.append("Its secrets were saved by another install: copy that install's secret.key here, or enter the "
                     "Outlook client secrets, Odoo API keys and the OpenAI key again after restoring.")
    return summary, notes


def _secrets_open(db, payload):
    """Whether this install's key opens the backup's encrypted values."""
    for row in payload["tables"].get("odoo_connection") or []:
        if row.get("api_key"):
            try:
                store.decrypt(db, row["api_key"])
            except Exception:
                return False
            return True
    return True


# ── bookings status ──────────────────────────────────────────────────
