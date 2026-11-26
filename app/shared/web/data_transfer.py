"""The Import / Export page: the setup workbook and the full backup (core/transfer.py does the work), and the parts
the jobs add (Freight Booking: the Booking Report upload and the bookings status export). Every upload is first
shown as a preview; nothing changes until Apply."""

import hashlib
import io
import os
import uuid
from datetime import datetime

from flask import flash, g, redirect, render_template, request, send_file, url_for

from app.shared.core import store, transfer
from app.shared.data import all_datasets
from app.shared.web.data_pages import data_sets
from app.shared.jobs import TransferPart, all_jobs
from app.shared.web import bp

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _path(name):
    return store.data_dir() / name


def _stamp():
    return datetime.now().strftime("%Y-%m-%d")


def digest(path):
    """Fingerprint of a file's bytes, as a hex string."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _changes(outcome):
    summary, notes = outcome
    return {"changes": sorted(summary.items()), "notes": notes}


def _apply_setup(db, pending, restore=False):
    summary, _notes = (transfer.restore if restore else transfer.import_setup)(db, pending.read_bytes(), apply=True)
    pending.unlink(missing_ok=True)
    what = "Backup restored" if restore else "Setup workbook imported"
    detail = "; ".join(f"{k}: {v}" for k, v in sorted(summary.items()))
    store.log_activity(db, "settings", what, detail or None, actor="admin")
    flash(f"{what}. {detail}" if detail else f"{what}: nothing to change.", "success")


SHARED = (
    TransferPart("setup", "setup_import.pending.xlsx", "",
                 lambda path: _changes(transfer.import_setup(g.db, path.read_bytes(), apply=False)), _apply_setup),
    TransferPart("restore", "backup_restore.pending.json", "",
                 lambda path: _changes(transfer.restore(g.db, path.read_bytes(), apply=False)),
                 lambda db, pending: _apply_setup(db, pending, restore=True)),
)


def parts():
    """{kind: TransferPart}: the shared kinds, then each job's."""
    return {part.kind: part for part in SHARED + tuple(p for job in all_jobs() for p in job.transfer)}


def _preview(part, path=None):
    try:
        return part.preview(path or _path(part.pending))
    except Exception as exc:  # a file that cannot be read is shown as such, never applied
        return {"error": f"The file could not be read: {exc}"}


def _private_copy(pending):
    """A file of this request's own with the pending file's bytes (read once), and their digest: what is shown, and
    what is applied after it, is one set of bytes that no other upload can replace (review v003 R02). None when
    the pending file is gone."""
    try:
        data = pending.read_bytes()
    except FileNotFoundError:
        return None, None
    copy = pending.with_name(f".{uuid.uuid4().hex}{pending.suffix}")
    copy.write_bytes(data)
    return copy, hashlib.sha256(data).hexdigest()


@bp.route("/import-export")
def import_export():
    found = parts()
    previews = {}
    for kind, part in found.items():
        copy, fingerprint = _private_copy(_path(part.pending))
        if copy is None:
            continue
        try:
            previews[kind] = dict(_preview(part, copy), digest=fingerprint)
        finally:
            copy.unlink(missing_ok=True)
    job_parts = [part for job in all_jobs() for part in job.transfer]
    context = {}
    for part in job_parts:
        context.update(part.context(g.db))
    return render_template("settings/data.html", title="Import / Export", previews=previews, job_parts=job_parts,
                           record_sets=data_sets([d.key for d in all_datasets()]), **context)


@bp.route("/import-export/<kind>", methods=["POST"])
def import_upload(kind):
    """Upload a file for a preview, apply the previewed file, or drop it."""
    part = parts().get(kind)
    if not part:
        return redirect(url_for("web.import_export"))
    pending = _path(part.pending)
    action = request.form.get("action", "preview")
    if action == "cancel":
        pending.unlink(missing_ok=True)
        return redirect(url_for("web.import_export"))
    if action == "apply":
        # Take the pending file out of reach first: an upload arriving now makes a new pending file and cannot
        # change this one. What is checked, previewed and applied is this one file (review v003 R02).
        taken = pending.with_name(f".applying.{uuid.uuid4().hex}{pending.suffix}")
        try:
            os.replace(pending, taken)
        except FileNotFoundError:
            flash("Upload the file again: its preview is gone.", "error")
            return redirect(url_for("web.import_export"))
        try:
            if request.form.get("digest") != digest(taken):
                if not pending.exists():  # not the file that was previewed: leave it for its own preview
                    os.replace(taken, pending)
                flash("The file was replaced after you previewed it. Check the new preview, then apply it.", "error")
                return redirect(url_for("web.import_export"))
            if _preview(part, taken).get("error"):
                flash("That file cannot be applied: it could not be read.", "error")
                return redirect(url_for("web.import_export"))
            part.apply(g.db, taken)
            g.db.commit()
            return redirect(url_for("web.import_export"))
        finally:
            taken.unlink(missing_ok=True)
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Choose a file first.", "error")
        return redirect(url_for("web.import_export"))
    upload.save(pending)
    return redirect(url_for("web.import_export") + f"#{kind}")


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
