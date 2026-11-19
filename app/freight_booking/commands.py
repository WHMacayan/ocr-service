"""Freight Booking's commands: clean-up (close the conflicts newer rules settle), import-booking-report (rates only)
and fill-bookings (the hourly fill, by hand)."""

import click

from app.shared.core import store


def register(cli):
    @cli.command("clean-up")
    @click.option("--apply", is_flag=True, help="Write the changes; without it, only report them.")
    def clean_up_command(apply):
        """After the booking rules changed: close the conflicts they settle (see cleanup.py)."""
        from app.freight_booking import cleanup
        from app.shared.odoo.client import Odoo
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

    @cli.command("import-booking-report")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    @click.option("--apply", is_flag=True, help="Write the rates; without it, only report them.")
    @click.option("--out", type=click.Path(dir_okay=False), help="Save the row-by-row result as an Excel file.")
    def import_booking_report_command(path, apply, out):
        """Write the Booking Report's agreed rate per container to each booking's Cost per Container,
        in every Odoo connection that is on (see booking_report.py)."""
        from openpyxl import load_workbook

        from app.freight_booking import booking_report
        from app.shared.odoo.client import Odoo
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

    @cli.command("fill-bookings")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    @click.option("--apply", is_flag=True, help="Write the values; without it, only report them.")
    def fill_bookings_command(path, apply):
        """Fill the empty fields a freight PO needs (factory invoice, Deliver To, rate, factory, charges) from the
        Booking Report and the team's own freight POs, in every Odoo connection that is on (booking_fill.py)."""
        from openpyxl import load_workbook

        from app.freight_booking import booking_fill
        from app.shared.odoo.client import Odoo
        rows = booking_fill.read_details(load_workbook(path, read_only=True, data_only=True))
        print(f"{len(rows)} booking rows read from {path}")
        db = store.connect()
        try:
            mappings = db.execute("SELECT * FROM vendor_mapping WHERE active = 1").fetchall()
            for connection in store.connections(db, on=True):
                odoo = Odoo.from_connection(db, connection)
                done, summary = booking_fill.fill(odoo, mappings, rows, apply=apply)
                print(f"{connection['name']}{'' if apply else ' (report only, nothing written)'}: "
                      f"{len(done)} bookings filled")
                for field, count in summary.most_common():
                    print(f"  {count:5}  {field}")
                if apply:
                    store.log_activity(db, "settings", f"Filled {len(done)} bookings from the Booking Report and the "
                                       f"team's freight POs ({connection['name']})", actor="admin")
                    db.commit()
        finally:
            db.close()
