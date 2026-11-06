"""The Settings page."""

from urllib.parse import urlparse

from flask import flash, g, redirect, render_template, request, url_for

from . import bp
from .base import audit
from .forms import describe_changes, Field, parse_form, secret_hints, sections
from ..core import store
from ..core.settings import SETTING_GROUPS, SETTINGS


# ── settings ─────────────────────────────────────────────────────────

SETTING_FIELDS = [Field(s.key, s.label, s.kind, help=s.help, group=s.group, tip=s.tip) for s in SETTINGS]


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        values, error = parse_form(SETTING_FIELDS, request.form)
        if error:
            flash(error, "error")
        else:
            before = {f.name: store.get_setting(g.db, f.name) for f in SETTING_FIELDS}
            for key, value in values.items():
                store.set_setting(g.db, key, value)
            changes = describe_changes(SETTING_FIELDS, before, values)
            if changes:
                audit("settings", f"Changed settings: {', '.join(c.split(':')[0] for c in changes)}",
                      "\n".join(changes), link=url_for("web.settings"))
            flash("Settings saved.")
            return redirect(url_for("web.settings"))
    current = {f.name: store.get_setting(g.db, f.name) for f in SETTING_FIELDS}
    return render_template("form.html", title="Settings", sections=sections(SETTING_FIELDS, SETTING_GROUPS),
                           record=current, hints=secret_hints(SETTING_FIELDS, current), options={},
                           action=url_for("web.settings"), extra="settings",
                           connections=store.connections(g.db), reference=store.reference_connection(g.db))


def odoo_warnings(odoo):
    """Risks in how the service reaches Odoo, worth fixing before real use."""
    warnings = []
    if odoo.uid in (1, 2):
        warnings.append("This is Odoo's administrator account, so a leaked API key would give full control of Odoo. "
                        "Create a dedicated user in OCR Documents / Manager with read access to purchases, "
                        "bills and warehouses, and use its login and API key here.")
    url = urlparse(odoo.url or "")
    if url.scheme == "http" and url.hostname not in LOCAL_HOSTS:
        warnings.append("The Odoo address uses http://, so the API key and data cross the network unencrypted. "
                        "Use the https:// address.")
    return warnings


# Odoo on this PC: plain http never leaves the machine.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "katana.local", "host.docker.internal"}
