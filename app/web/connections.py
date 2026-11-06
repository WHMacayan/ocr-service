"""Odoo connections: add, edit and test, turn on (with the mapping check) and off, delete."""

import re
from datetime import datetime, timezone

from flask import abort, flash, g, redirect, render_template, request, url_for

from . import bp
from .base import audit
from .forms import _shown, describe_changes, Field, parse_form, secret_hints
from .settings import odoo_warnings
from ..core import store
from ..odoo import mapping_check
from ..odoo.client import Odoo


# ── Odoo connections ─────────────────────────────────────────────────

CONNECTION_FIELDS = [
    Field("name", "Name", help="For example Production, Staging (1103) or Development",
          tip="A name for this Odoo, shown on the Settings page, in the activity log and on each email."),
    Field("url", "Odoo address", help="For example http://katana.local",
          tip="The web address of this Odoo. The service sends booking and bill suggestions there."),
    Field("database", "Database", tip="The Odoo database the suggestions go into, for example 1025."),
    Field("login", "Login", help="The user's login in Odoo (Settings > Users), not the display name",
          tip="The Odoo user the service signs in as. Everything it writes in Odoo is recorded under this user, "
              "and it can only do what this user is allowed to do."),
    Field("api_key", "API key", "secret", help="Odoo: Preferences > Account Security > New API Key",
          tip="Lets the service sign in to Odoo without a password. It must be made by the user above. "
              "Delete the key in Odoo to cut the service off at once."),
    Field("is_production", "This is production", "bool",
          help="Shown in red on every page while on; turning it on asks you to type its name",
          tip="Tick for the Odoo your company really works in, so it is never written to by mistake."),
]
# Changing these points the connection at another Odoo or user: it is checked again before it stays on.
WHERE_FIELDS = ("url", "database", "login", "api_key")


def _connection(connection_id):
    return store.connection(g.db, connection_id) or abort(404)


def _connection_link(connection_id):
    return url_for("web.connection", connection_id=connection_id)


def check_connection(connection):
    """(odoo, problems): log in to ``connection`` and run the mapping check against the reference
    connection. ``odoo`` is None when it could not log in."""
    try:
        odoo = Odoo.from_connection(g.db, connection)
    except Exception as exc:
        return None, [f"{connection['name']} could not be reached: {exc}"]
    reference = store.reference_connection(g.db)
    reference_odoo = None
    if reference and reference["id"] != connection["id"]:
        try:
            reference_odoo = Odoo.from_connection(g.db, reference)
        except Exception as exc:
            return odoo, [f"{reference['name']} could not be reached to compare the mappings with: {exc}"]
    try:
        problems = mapping_check.check(g.db, odoo, connection["name"], reference_odoo,
                                     reference["name"] if reference_odoo else None)
    except Exception as exc:
        problems = [f"The mappings could not be checked in {connection['name']}: {exc}"]
    return odoo, problems


def _record_check(connection_id, problems):
    with g.db:
        g.db.execute("UPDATE odoo_connection SET last_check_at = CURRENT_TIMESTAMP, last_check_note = ? WHERE id = ?",
                     ("\n".join(problems) or None, connection_id))


def set_connection_on(connection, on, start_from=None):
    """Switch a connection on or off, log it, and bring the email summaries in line."""
    with g.db:
        g.db.execute("UPDATE odoo_connection SET active = ?, start_from = ? WHERE id = ?",
                     (int(on), start_from if on else connection["start_from"], connection["id"]))
        store.refresh_messages(g.db)
    production = connection["is_production"]
    detail = f"Writes mail received from {start_from[:10]}." if on and start_from else None
    audit("settings", f"Turned {'on' if on else 'off'} Odoo connection {connection['name']}"
          + (" (production)" if production else ""), detail, level="warning" if production else "info",
          link=_connection_link(connection["id"]))


@bp.route("/connections/new", methods=["GET", "POST"], defaults={"connection_id": None})
@bp.route("/connections/<int:connection_id>", methods=["GET", "POST"])
def connection(connection_id):
    current = _connection(connection_id) if connection_id else {"is_production": 0, "active": 0}
    if request.method == "POST":
        values, error = parse_form(CONNECTION_FIELDS, request.form)
        if not error and not values.get("name"):
            error = "Name is required."
        if not error and g.db.execute("SELECT 1 FROM odoo_connection WHERE name = ? AND id != ?",
                                      (values["name"], connection_id or 0)).fetchone():
            error = f"There is already a connection named {values['name']}."
        if error:
            flash(error, "error")
            current.update(values)
        else:
            is_new = not connection_id
            changes = describe_changes(CONNECTION_FIELDS, current, values)
            moved = any(_shown(current.get(f)) != _shown(values[f]) for f in WHERE_FIELDS if f in values)
            connection_id = store.save_connection(g.db, connection_id, values)
            link = _connection_link(connection_id)
            if changes or is_new:
                audit("settings", f"{'Created' if is_new else 'Updated'} Odoo connection {values['name']}",
                      "\n".join(changes), link=link)
            saved = store.connection(g.db, connection_id)
            test = request.form.get("then") == "test"
            if saved["active"] and not store.is_complete(saved):
                set_connection_on(saved, False)
                flash("Saved. The connection is no longer complete, so it was turned off.", "error")
            elif (saved["active"] and moved) or (test and store.is_complete(saved)):
                odoo, problems = check_connection(saved)
                _record_check(connection_id, problems)
                if saved["active"] and problems:
                    set_connection_on(saved, False)
                    flash("Saved, but the connection no longer checks out, so it was turned off.", "error")
                if odoo:
                    flash(f"Saved. Connected to {saved['name']} as user {odoo.uid}.")
                    for warning in odoo_warnings(odoo):
                        flash(warning, "error")
                for problem in problems:
                    flash(problem, "error")
            elif test:
                flash("Saved. Fill in the address, database, login and API key to test the connection.", "error")
            else:
                flash("Connection saved.")
            return redirect(link)
    return render_template("connection.html", title="Odoo connection", connection_id=connection_id, record=current,
                           fields=CONNECTION_FIELDS, hints=secret_hints(CONNECTION_FIELDS, current),
                           complete=store.is_complete(current))


@bp.route("/connections/<int:connection_id>/on", methods=["GET", "POST"])
def connection_on(connection_id):
    """Turning on: the mapping check, a start date, and for production the name typed out."""
    connection = _connection(connection_id)
    link = _connection_link(connection_id)
    if connection["active"]:
        return redirect(link)
    if not store.is_complete(connection):
        flash("Fill in the address, database, login and API key before turning this connection on.", "error")
        return redirect(link)
    _odoo, problems = check_connection(connection)
    _record_check(connection_id, problems)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = request.form.get("start_from") or today
    if request.method == "POST":
        error = None
        if problems:
            error = "The connection was not turned on: the mappings do not check out."
        elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
            error = "Enter the start date as a date."
        elif connection["is_production"] and (request.form.get("confirm_name") or "").strip() != connection["name"]:
            error = f"Type {connection['name']} exactly to turn production on."
        if error:
            flash(error, "error")
        else:
            set_connection_on(connection, True, f"{start} 00:00:00")
            flash(f"{connection['name']} is on. It gets mail received from {start}.")
            return redirect(url_for("web.settings"))
    return render_template("connection_on.html", title=f"Turn on {connection['name']}", connection=connection,
                           problems=problems, start=start, mapped=mapping_check.mapped_ids(g.db),
                           reference=store.reference_connection(g.db))


@bp.route("/connections/<int:connection_id>/off", methods=["POST"])
def connection_off(connection_id):
    connection = _connection(connection_id)
    if connection["active"]:
        set_connection_on(connection, False)
        flash(f"{connection['name']} is off. Nothing more is written to it.")
    return redirect(request.form.get("next") or url_for("web.settings"))


@bp.route("/connections/<int:connection_id>/delete", methods=["POST"])
def delete_connection(connection_id):
    connection = _connection(connection_id)
    link = _connection_link(connection_id)
    if connection["active"]:
        flash("Turn the connection off before deleting it.", "error")
        return redirect(link)
    if (request.form.get("confirm_name") or "").strip() != connection["name"]:
        flash(f"Type {connection['name']} exactly to delete it.", "error")
        return redirect(link)
    with g.db:
        g.db.execute("DELETE FROM odoo_change WHERE connection_id = ?", (connection_id,))
        g.db.execute("DELETE FROM odoo_connection WHERE id = ?", (connection_id,))
        store.refresh_messages(g.db)
    audit("settings", f"Deleted Odoo connection {connection['name']}",
          "Its record of what each email wrote there is removed. Records in that Odoo are not touched.",
          level="warning")
    flash(f"{connection['name']} deleted.")
    return redirect(url_for("web.settings"))
