"""The Import / Export page: the Booking Report upload, the setup workbook, the bookings status export and
the full backup (core/transfer.py does the work). Every upload is first shown as a preview; nothing changes
until Apply."""

import io
import shutil
from collections import Counter
from datetime import datetime

from flask import flash, g, redirect, render_template, request, send_file, url_for
from openpyxl import load_workbook

from . import bp
from ..core import store, transfer
from ..flows import booking_fill
from ..odoo.client import Odoo

REPORT = "booking_report.xlsx"
PENDING = {"report": "booking_report.pending.xlsx", "setup": "setup_import.pending.xlsx",
           "restore": "backup_restore.pending.json"}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _path(name):
    return store.data_dir() / name


def _stamp():
    return datetime.now().strftime("%Y-%m-%d")


def report_summary(path):
    """What a Booking Report holds, read without Odoo: rows per tab, and how many give each value."""
    rows = booking_fill.read_details(load_workbook(io.BytesIO(path.read_bytes()), read_only=True, data_only=True))
    tabs = Counter(r["tab"] for r in rows)
    return {"rows": len(rows), "tabs": sorted(tabs.items()),
            "invoices": sum(bool(r["invoices"]) for r in rows), "fd": sum(bool(r["fd"]) for r in rows),
            "rates": sum(booking_fill.rate_of(r["freight"])[0] == "rate" for r in rows),
            "factories": sum(bool(r["factory_email"]) for r in rows)}


@bp.route("/import-export")
def import_export():
    current = _path(REPORT)
    report = None
    if current.exists():
        report = {"saved": datetime.fromtimestamp(current.stat().st_mtime), "size": current.stat().st_size}
    previews = {kind: _preview(kind) for kind in PENDING if _path(PENDING[kind]).exists()}
    return render_template("settings/data.html", title="Import / Export", report=report, previews=previews,
                           last_fill=store.get_setting(g.db, "fill_last_at"),
                           connections=[c["name"] for c in store.connections(g.db, on=True)])


def _preview(kind):
    path = _path(PENDING[kind])
    try:
        if kind == "report":
            return {"summary": report_summary(path)}
        data = path.read_bytes()
        summary, notes = (transfer.import_setup if kind == "setup" else transfer.restore)(g.db, data, apply=False)
        return {"changes": sorted(summary.items()), "notes": notes}
    except Exception as exc:  # a file that cannot be read is shown as such, never applied
        return {"error": f"The file could not be read: {exc}"}


@bp.route("/import-export/<kind>", methods=["POST"])
def import_upload(kind):
    """Upload a file for a preview, apply the previewed file, or drop it."""
    if kind not in PENDING:
        return redirect(url_for("web.import_export"))
    pending = _path(PENDING[kind])
    action = request.form.get("action", "preview")
    if action == "cancel":
        pending.unlink(missing_ok=True)
        return redirect(url_for("web.import_export"))
    if action == "apply":
        if not pending.exists():
            flash("Upload the file again: its preview is gone.", "error")
            return redirect(url_for("web.import_export"))
        if _preview(kind).get("error"):
            flash("That file cannot be applied: it could not be read.", "error")
            return redirect(url_for("web.import_export"))
        _apply(kind, pending)
        return redirect(url_for("web.import_export"))
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Choose a file first.", "error")
        return redirect(url_for("web.import_export"))
    upload.save(pending)
    return redirect(url_for("web.import_export") + f"#{kind}")


def _apply(kind, pending):
    if kind == "report":
        current = _path(REPORT)
        if current.exists():
            shutil.copyfile(current, _path("booking_report.previous.xlsx"))
        shutil.move(str(pending), current)
        with g.db:  # the worker fills on its next pass, not in an hour
            g.db.execute("DELETE FROM setting WHERE key = 'fill_last_at'")
        store.log_activity(g.db, "settings", "Booking Report uploaded: bookings are filled from it on the next pass",
                           actor="admin")
        flash("Booking Report saved. The bookings are filled from it on the worker's next pass (within minutes).",
              "success")
    else:
        data = pending.read_bytes()
        summary, _notes = (transfer.import_setup if kind == "setup" else transfer.restore)(g.db, data, apply=True)
        pending.unlink(missing_ok=True)
        what = "Setup workbook imported" if kind == "setup" else "Backup restored"
        detail = "; ".join(f"{k}: {v}" for k, v in sorted(summary.items()))
        store.log_activity(g.db, "settings", what, detail or None, actor="admin")
        flash(f"{what}. {detail}" if detail else f"{what}: nothing to change.", "success")
    g.db.commit()


@bp.route("/import-export/setup.xlsx")
def export_setup():
    return send_file(io.BytesIO(transfer.export_setup(g.db)), mimetype=XLSX, as_attachment=True,
                     download_name=f"OCR service setup {_stamp()}.xlsx")


@bp.route("/import-export/backup.json")
def export_backup():
    store.log_activity(g.db, "settings", "Full backup downloaded", actor="admin")
    g.db.commit()
    return send_file(io.BytesIO(transfer.backup_bytes(g.db)), mimetype="application/json", as_attachment=True,
                     download_name=f"OCR service backup {_stamp()}.json")


@bp.route("/import-export/bookings.xlsx")
def export_bookings():
    connections = []
    for connection in store.connections(g.db, on=True):
        try:
            connections.append((connection["name"], Odoo.from_connection(g.db, connection)))
        except Exception as exc:
            flash(f"{connection['name']} could not be reached: {exc}", "error")
    if not connections:
        flash("No Odoo connection is on: there are no bookings to export.", "error")
        return redirect(url_for("web.import_export"))
    return send_file(io.BytesIO(transfer.bookings_workbook(connections)), mimetype=XLSX, as_attachment=True,
                     download_name=f"Freight bookings status {_stamp()}.xlsx")
