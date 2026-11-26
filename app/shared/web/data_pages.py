"""Import / Export of records on every list, as Odoo's: pick the fields to export; upload a file, map its columns to the
fields of a record, test it, then apply it to create and update records (app/shared/data does the work).

    /data/<list>/export            the field picker; posting it downloads the file
    /data/<list>/import            upload, map the columns, test, apply
    /data/<list>/import/template   an empty file with the columns an import takes

A list that is the record of what happened (emails, activity) can be exported, never imported."""

import json
from datetime import datetime
import hashlib
from io import BytesIO

from flask import abort, flash, g, redirect, render_template, request, send_file, url_for

from app.shared.core import store
from app.shared.data import Context, auto_map, dataset, all_datasets, mapped_rows
from app.shared.data.tables import CONTENT_TYPES, CSV, XLSX, read_table, write_table
from app.shared.web import bp
from app.shared.web.base import audit

FILTERS = ("q", "state", "kind")  # what a list was showing, carried to its export
ALLOWED_FILES = (".xlsx", ".xlsm", ".csv")
SHOWN_ROWS = 300


@bp.app_template_global()
def data_sets(keys):
    """[{key, label, importable}] of the data sets a page offers in its Import / Export menu."""
    found = {d.key: d for d in all_datasets()}
    return [{"key": k, "label": found[k].label, "importable": found[k].importable} for k in keys if k in found]


def _filters():
    return {k: request.values.get(k) for k in FILTERS if request.values.get(k)}


def _fault(exc):
    return (str(getattr(exc, "faultString", exc)).strip().splitlines() or ["unknown error"])[-1][:300]


def _get(key, importing=False):
    ds = dataset(key) or abort(404)
    if importing and not ds.importable:
        abort(404)
    return ds


def _list_url(ds):
    return url_for(ds.endpoint, **ds.endpoint_args)


# ── export ───────────────────────────────────────────────────────────

@bp.route("/data/<key>/export", methods=["GET", "POST"])
def data_export(key):
    ds = _get(key)
    ctx, filters, error = Context(g.db), _filters(), None
    try:
        columns = ds.columns(ctx)
    except Exception as exc:
        columns, error = [], f"Odoo could not be read, so its fields are not known: {_fault(exc)}"
    names = [c.name for c in columns]
    if request.method == "POST" and not error:
        picked = [n for n in names if request.form.get(f"f_{n}")]
        fmt = request.form.get("format") if request.form.get("format") in (XLSX, CSV) else XLSX
        if not picked:
            flash("Pick at least one field to export.", "error")
            return redirect(url_for("web.data_export", key=key, **filters))
        try:
            rows = ds.rows(ctx, picked, filters)
        except Exception as exc:
            flash(f"The records could not be read: {_fault(exc)}", "error")
            return redirect(url_for("web.data_export", key=key, **filters))
        by_name = {c.name: c for c in columns}
        data = write_table([by_name[n].label for n in picked], [[row.get(n) for n in picked] for row in rows], fmt, sheet=ds.label)
        store.set_setting(g.db, f"export_fields.{key}", json.dumps(picked))
        audit("settings", f"Exported {len(rows)} {ds.label.lower()}", f"Fields: {', '.join(by_name[n].label for n in picked)}\n"
              f"Format: {fmt}" + (f"\nFilter: {', '.join(f'{k}={v}' for k, v in filters.items())}" if filters else ""))
        g.db.commit()
        return send_file(BytesIO(data), mimetype=CONTENT_TYPES[fmt], as_attachment=True,
                         download_name=f"{ds.label} {datetime.now():%Y-%m-%d}.{fmt}")
    saved = []
    try:
        saved = [n for n in json.loads(store.get_setting(g.db, f"export_fields.{key}") or "[]") if n in names]
    except ValueError:
        pass
    preset = request.args.get("preset")
    defaults = [n for n in ds.default_names(ctx)] if not error else []
    chosen = set(names if preset == "all" else [] if preset == "none" else defaults if preset == "defaults" else saved or defaults)
    return render_template("data/export.html", title=f"Export {ds.label.lower()}", ds=ds, columns=columns, chosen=chosen,
                           filters=filters, list_url=_list_url(ds), error=error)


# ── import ───────────────────────────────────────────────────────────

def _pending(key):
    found = sorted(store.data_dir().glob(f"import.{key}.pending.*"))
    return found[0] if found else None


def _discard(key):
    for old in store.data_dir().glob(f"import.{key}.pending.*"):
        old.unlink(missing_ok=True)


@bp.route("/data/<key>/import/template")
def data_template(key):
    ds = _get(key, importing=True)
    try:
        columns = [c for c in ds.columns(Context(g.db)) if c.importable]
    except Exception as exc:
        flash(f"Odoo could not be read, so the template cannot be made: {_fault(exc)}", "error")
        return redirect(url_for("web.data_import", key=key))
    fmt = request.args.get("format") if request.args.get("format") in (XLSX, CSV) else XLSX
    notes = [("About this file", f"One row per {ds.singular}. Leave the ID empty to add a new {ds.singular}"
                                  f"{'' if ds.creatable else ' (this list only updates: every row needs its ID)'}. "
                                  "An empty cell leaves the field as it is. Delete the columns you do not need.")]
    for c in columns:
        hint = {"bool": "Yes or No", "date": "YYYY-MM-DD", "datetime": "YYYY-MM-DD HH:MM:SS", "int": "a whole number",
                "float": "a number", "many2one": "the name of the record"}.get(c.kind, "text")
        if c.kind == "select":
            hint = "one of: " + ", ".join(str(label) for _k, label in c.options)
        notes.append((c.label, hint + (". Required for a new record." if c.required else "") + (f" {c.note}" if c.note else "")))
    data = write_table([c.label for c in columns], [], fmt, sheet=ds.label, notes=notes)
    return send_file(BytesIO(data), mimetype=CONTENT_TYPES[fmt], as_attachment=True,
                     download_name=f"{ds.label} import template.{fmt}")


def _connection(form):
    """The Odoo connection an import writes to: the one chosen among those that are on, else the one the pages read."""
    on = [c for c in store.connections(g.db, on=True) if store.is_complete(c)]
    chosen = next((c for c in on if str(c["id"]) == form.get("connection")), None)
    return chosen or (store.reference_connection(g.db) if on else None), on


@bp.route("/data/<key>/import", methods=["GET", "POST"])
def data_import(key):
    ds = _get(key, importing=True)
    action = request.form.get("action") if request.method == "POST" else None
    if action == "discard":
        _discard(key)
        return redirect(url_for("web.data_import", key=key))
    if action == "upload":
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a file first.", "error")
            return redirect(url_for("web.data_import", key=key))
        suffix = "." + upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
        raw = upload.read()
        try:
            if suffix not in ALLOWED_FILES:
                raise ValueError("Use an Excel workbook (.xlsx) or a CSV file (.csv).")
            read_table(raw, upload.filename)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("web.data_import", key=key))
        _discard(key)
        (store.data_dir() / f"import.{key}.pending{suffix}").write_bytes(raw)
        return redirect(url_for("web.data_import", key=key))

    pending = _pending(key)
    connection, on = _connection(request.form if request.method == "POST" else request.args) if ds.writes_to == "odoo" else (None, [])
    page = {"ds": ds, "list_url": _list_url(ds), "connections": on, "connection": connection, "title": f"Import {ds.label.lower()}",
            "step": "upload", "writes_to": None, "error": None}
    if not pending:
        return render_template("data/import.html", **page)
    raw_pending = pending.read_bytes()
    page["digest"] = hashlib.sha256(raw_pending).hexdigest()  # Apply must name the file whose mapping was tested (F17)
    try:
        headers, rows = read_table(raw_pending, pending.name)
    except ValueError as exc:
        _discard(key)
        flash(str(exc), "error")
        return redirect(url_for("web.data_import", key=key))
    ctx = Context(g.db, connection if ds.writes_to == "odoo" else None)
    try:
        columns = [c for c in ds.columns(ctx) if c.importable]
        page["writes_to"] = ds.target(ctx)
    except Exception as exc:
        page.update(step="map", headers=headers, rows=rows[:3], columns=[], mapping=[], plan=None, result=None, skip=False,
                    total=len(rows), error=f"Odoo could not be read, so the fields are not known: {_fault(exc)}")
        return render_template("data/import.html", **page)
    form = request.form if request.method == "POST" else {}
    valid = {c.name for c in columns}
    mapping = [form.get(f"col_{i}", "") for i in range(len(headers))] if action else auto_map(headers, columns)
    mapping = [m if m in valid else "" for m in mapping]
    seen, taken = set(), []
    for m in mapping:  # a field takes one column
        taken.append(m if m not in seen else "")
        seen.add(m)
    mapping = taken
    page.update(step="map", headers=headers, rows=rows[:3], columns=columns, mapping=mapping, plan=None, result=None,
                skip=bool(form.get("skip")), total=len(rows))
    if action == "apply" and request.form.get("digest") != page["digest"]:
        flash("The file was replaced after you chose its columns. Check the columns, test it, then import.", "error")
        return render_template("data/import.html", **page)
    if action in ("test", "apply"):
        if not any(mapping):
            flash("Map at least one column.", "error")
            return render_template("data/import.html", **page)
        if len(rows) > ds.max_rows:
            page["error"] = f"The file has {len(rows)} rows; this list takes at most {ds.max_rows} at a time: split it."
            return render_template("data/import.html", **page)
        try:
            plan = ds.plan(ctx, mapped_rows(rows, mapping))
        except Exception as exc:
            page["error"] = f"The file could not be checked: {_fault(exc)}"
            return render_template("data/import.html", **page)
        page["plan"] = plan
        if action == "apply":
            if plan.problems:
                flash("Nothing was imported: " + " ".join(plan.problems), "error")
            elif plan.errors and not page["skip"]:
                flash(f"{plan.errors} row(s) have a problem. Fix the file, or tick “Skip the rows with a problem”.", "error")
            else:
                try:
                    ds.apply(ctx, plan)
                except Exception as exc:
                    flash(f"The import stopped: {_fault(exc)}", "error")
                    return render_template("data/import.html", **page)
                _log_import(ds, plan, pending.name)
                _discard(key)
                page.update(step="result", result=plan)
    return render_template("data/import.html", **page)


def _log_import(ds, plan, filename):
    done = [r for r in plan.rows if r.done]
    created, updated = sum(1 for r in done if r.action == "create"), sum(1 for r in done if r.action == "update")
    failed = sum(1 for r in plan.rows if r.action == "error")
    lines = [f"Row {r.number}: {'added' if r.action == 'create' else 'updated'} {r.label}"
             + (f" ({'; '.join(f'{c}: {o if o is not None else chr(8212)} → {n}' for c, o, n in r.changes)})" if r.changes else "")
             for r in done[:60]]
    if len(done) > 60:
        lines.append(f"… and {len(done) - 60} more")
    audit("odoo" if ds.writes_to == "odoo" else "settings",
          f"Imported {ds.label.lower()}: {created} added, {updated} updated" + (f", {failed} skipped" if failed else ""),
          "\n".join(lines) or None)
    g.db.commit()
