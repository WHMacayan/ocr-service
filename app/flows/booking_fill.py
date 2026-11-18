"""Fill the bookings' empty fields from the team's own records, so a booking can reach Ready for PO.

The documents never give these fields (freight PO study, 16-17 Nov 2026):
- Factory invoice: the Booking Report's "Factory invoice" column (the team PO's Factory Invoice on 456 of 460).
- Deliver To: the warehouse the team chose before for the report's FD address (93% right on 636 shipments);
  an address never seen before goes to WTD Factory Direct (85% of shipments).
- Rate per container: the Booking Report's FREIGHT (the team PO's rate on 95%).
- Factory: the report's Factory e-mail, through the vendor mapping by sender domain.
- Additional charges: the forwarder's fixed handling fee on the team's POs (Quartz 65, Mentfield 75, Apex 50),
  else "No additional charges".
- Freight cost: rate x containers, the total the PO will carry (the forwarder's own invoice comes weeks later).
- House B/L: confirmed equal to the MBL when the forwarder is the carrier itself (a carrier-direct shipment).
- Forwarder: the report's FORWARDER (QUARTZ, APEX, DEWELL), as the team's POs name it. The report is what the team
  uploads as the PO, so a different forwarder on the papers is a conflict for a person (BKKM68473200, 17 Nov 2026).
- ETA: the report's ETA (the team's plan) when no paper gave a US arrival; the first paper that does replaces it.

Only empty fields of bookings without a freight PO are filled; every booking gets a note saying what came from
where. Nothing is written unless ``apply``.
"""

import re
from collections import Counter, defaultdict
from datetime import date, datetime

from .booking_report import key, rate_of, LOWEST_RATE
from .freight import BOOKING, CONFLICT, CONTAINER, INVOICE, CARRIER, copy_po_details, min_sea_days, resolve_mapping

FREIGHT_PRODUCT_NAME = "Ocean Freight"
HANDLING_FEE_NAME = "Handling fee"
_SPLIT = re.compile(r"[,/;&\n]+|\s{2,}|\bAND\b", re.IGNORECASE)


def read_details(workbook):
    """Every booking row of the Booking Report with the columns the fill uses."""
    rows = []
    for ws in workbook.worksheets:
        if not hasattr(ws, "iter_rows"):
            continue
        it = ws.iter_rows(values_only=True, max_col=40)
        columns = None
        for row_number, values in enumerate(it, 1):
            names = [str(v).strip().upper() if v is not None else "" for v in values]
            if {"FORWARDER", "FREIGHT", "BOOKING CONFIRMATION"} <= set(names):
                columns = {name: i for i, name in reversed(list(enumerate(names))) if name}
                break
            if row_number > 10:
                break
        if not columns:
            continue
        for row_number, values in enumerate(it, row_number + 1):
            if not values or not any(v not in (None, "") for v in values):
                continue
            cell = lambda name: values[columns[name]] if name in columns and columns[name] < len(values) else None  # noqa: E731
            numbers = [n for n in (key(p) for p in re.split(r"[/,;\n]", str(cell("BOOKING CONFIRMATION") or ""))) if len(n) >= 6]
            if not numbers:
                continue
            invoices = [n for n in (re.sub(r"[^A-Z0-9-]", "", p.strip().upper()) for p in _SPLIT.split(str(cell("FACTORY INVOICE") or "")))
                        if len(re.sub(r"[^A-Z0-9]", "", n)) >= 6 and re.search(r"\d", n)]
            eta = cell("ETA")
            rows.append({"tab": ws.title, "row": row_number, "numbers": numbers, "freight": cell("FREIGHT"),
                         "fd": fd_key(cell("FD")), "invoices": invoices,
                         "factory_email": str(cell("FACTORY") or "").strip().lower(),
                         "departure": str(cell("DEPARTURE PORT") or "").strip().upper(),
                         "forwarder": " ".join(str(cell("FORWARDER") or "").upper().split()),
                         "eta": eta.date().isoformat() if isinstance(eta, datetime) else None})
    return rows


def fd_key(value):
    text = re.sub(r"\(NEW\)|[^A-Z0-9 ]", " ", str(value or "").upper())
    text = re.sub(r"\s+", " ", text).strip()
    return "" if text in ("", "N A", "NA") else text


def learn_forwarders(rows, orders_by_number, ocean_ids=None):
    """The partner each of the report's forwarder names stands for, by the team's own ocean freight POs for those
    bookings: {"QUARTZ": (id, "Quartz Logistics (NY) Inc."), ...}. A booking's customs, trucking and duty POs are
    freight POs too, so only those carrying Ocean Freight (``ocean_ids``) vote: MENTFIELD is not Triumph Link."""
    votes = defaultdict(Counter)
    for row in rows:
        code = row.get("forwarder")
        found = [o for n in row["numbers"] for o in orders_by_number.get(n, [])
                 if ocean_ids is None or o.get("id") in ocean_ids]
        for order in found[:1]:
            if code and order.get("partner_id") and not re.search(r"/|&|AND", code):  # APEX / DEWELL: two
                votes[code][tuple(order["partner_id"])] += 1
    learned = {}
    for code, c in votes.items():
        partner, n = c.most_common(1)[0]
        if n >= 2 and n * 3 >= sum(c.values()) * 2:  # two POs at least, and two of three agree
            learned[code] = partner
    return learned


def forwarder_check(booking, row, learned):
    """What the report's forwarder says about the booking's: ("fill", id, name) when the booking has none, or a person
    chose the report's on its conflict (Use new writes that name); ("conflict", held name, report's name) when they
    differ; None when they agree or the report's name was never seen on a team PO."""
    found = learned.get(row.get("forwarder") or "")
    if not found:
        return None
    partner, name = found
    held = booking.get("forwarder_id")
    if not held:
        return "fill", partner, name
    if held[0] == partner:
        return None
    if (booking.get("forwarder_name") or "") == name:
        return "fill", partner, name
    return "conflict", held[1], name


def settle_report_conflicts(odoo, booking_id):
    """The booking's forwarder now is the report's (a later paper, or a person's choice): the conflict the report
    opened about it is settled, so it no longer holds the booking back. Returns how many."""
    ids = odoo.call(CONFLICT, "search", [("booking_id", "=", booking_id), ("field_name", "=", "forwarder_name"),
                                         ("state", "=", "open"), ("source", "ilike", "Booking Report")])
    if ids:
        odoo.write(CONFLICT, ids, {"state": "resolved"})
    return len(ids)


class TeamHistory:
    """What the team's own freight POs say: Deliver To per FD address, the handling fee per forwarder."""

    def __init__(self, odoo, rows):
        self.odoo = odoo
        orders = odoo.search_read("purchase.order", [("is_katana_freight_po", "=", True), ("state", "!=", "cancel")],
                                  ["partner_id", "partner_ref", "master_bl", "house_bl", "picking_type_id"])
        by_key = defaultdict(list)
        for o in orders:
            ref = re.split(r"[\s/]|-(?=\d{1,2}$)", (o["partner_ref"] or "").strip())[0] if o["partner_ref"] else ""
            for k in {key(ref), key(o["master_bl"]), key(o["house_bl"])} - {""}:
                by_key[k].append(o)
        types = {t["id"]: t["warehouse_id"][0] for t in odoo.search_read("stock.picking.type", [("code", "=", "incoming")],
                                                                          ["warehouse_id"]) if t["warehouse_id"]}
        votes = defaultdict(Counter)
        for row in rows:
            found = [o for n in row["numbers"] for o in by_key.get(n, [])]
            if row["fd"] and found and found[0]["picking_type_id"]:
                warehouse = types.get(found[0]["picking_type_id"][0])
                if warehouse:
                    votes[row["fd"]][warehouse] += 1
        self.warehouse_by_fd = {fd: c.most_common(1)[0][0] for fd, c in votes.items()}
        self.ocean_ids = set()  # the team's ocean freight POs (an Ocean Freight line): set by _fees
        self.fee_by_forwarder = self._fees(orders)
        self.forwarder_by_code = learn_forwarders(rows, by_key, self.ocean_ids)
        direct = odoo.search_read("stock.warehouse", [("name", "=", "WTD Factory Direct")], ["id"], limit=1)
        self.default_warehouse = direct[0]["id"] if direct else False

    def _fees(self, orders):
        """The forwarder's handling fee when most of its freight POs carry one (the most common amount), else 0."""
        lines = self.odoo.search_read("purchase.order.line", [("order_id", "in", [o["id"] for o in orders]),
                                                              ("product_id.name", "in", [FREIGHT_PRODUCT_NAME, HANDLING_FEE_NAME])],
                                      ["order_id", "partner_id", "product_id", "price_unit"])
        freight_pos, fee_pos, fees = defaultdict(set), defaultdict(set), defaultdict(Counter)
        for line in lines:
            vendor, order = line["partner_id"][0], line["order_id"][0]
            if line["product_id"][1].endswith(FREIGHT_PRODUCT_NAME):
                freight_pos[vendor].add(order)
                self.ocean_ids.add(order)
            else:
                fee_pos[vendor].add(order)
                fees[vendor][round(line["price_unit"], 2)] += 1
        return {vendor: (fees[vendor].most_common(1)[0][0] if len(fee_pos[vendor] & pos) * 2 >= len(pos) else 0.0)
                for vendor, pos in freight_pos.items() if pos}

    def warehouse(self, fd):
        """(warehouse, how): the team's choice for this address, or for it without its WH mark; a new address goes
        to Factory Direct; no address: none (a person decides)."""
        if not fd:
            return None, ""
        if fd in self.warehouse_by_fd:
            return self.warehouse_by_fd[fd], "the team's choice for this address"
        bare = re.sub(r"\s+(WH|WAREHOUSE)\b.*$", "", fd)
        if bare in self.warehouse_by_fd:
            return self.warehouse_by_fd[bare], f"the team's choice for {bare}"
        return self.default_warehouse, "new address: Factory Direct"


def fill(odoo, mappings, rows, apply=False):
    """(per booking: [(field, value, where)], summary Counter)."""
    history = TeamHistory(odoo, rows)
    usd = odoo.search_read("res.currency", [("name", "=", "USD")], ["id"], limit=1)
    bookings = odoo.search_read(BOOKING, [("state", "not in", ["po_created", "po_matched"])],
                                ["booking_number", "master_bl", "warehouse_id", "cost_per_container", "factory_id",
                                 "forwarder_id", "additional_charges", "no_additional_charges", "freight_cost",
                                 "house_bl", "house_bl_assumed", "house_bl_confirmed", "invoice_ids", "state",
                                 "forwarder_name", "etd", "eta", "port_of_loading"])
    index = defaultdict(list)
    for b in bookings:
        for value in (b["booking_number"], b["master_bl"]):
            if len(key(value)) >= 6:
                index[key(value)].append(b)
    report = defaultdict(list)
    for row in rows:
        for n in row["numbers"]:
            for b in index.get(n, []):
                report[b["id"]].append(row)
    done, summary = {}, Counter()
    warehouses = {w["id"]: w["name"] for w in odoo.search_read("stock.warehouse", [], ["name"])}
    # Bookings already linked to the team's PO: the PO's B/Ls, containers and mode fill their empty ones.
    for b in odoo.search_read(BOOKING, [("state", "=", "po_matched"), ("purchase_order_id", "!=", False),
                                        "|", "|", ("master_bl", "=", False), ("container_ids", "=", False),
                                        ("freight_mode", "=", "unknown")], ["purchase_order_id"]):
        if apply:
            copied = copy_po_details(odoo, b["id"], b["purchase_order_id"][0])
            if copied:
                summary["copied from the linked team PO"] += 1
                odoo.call(BOOKING, "message_post", [b["id"]], body=f"From the team's PO: {', '.join(copied)}.")
        else:
            summary["copied from the linked team PO"] += 1
    for b in bookings:
        rows_b = report.get(b["id"], [])
        row = rows_b[0] if rows_b else None
        where = f"Booking Report {row['tab']} row {row['row']}" if row else ""
        updates, filled = {}, []
        if row:
            invoices = [i for r in rows_b for i in r["invoices"]]
            existing = {re.sub(r"[^A-Z0-9]", "", i["invoice_number"]) for i in
                        (odoo.call(INVOICE, "read", b["invoice_ids"], ["invoice_number"]) if b["invoice_ids"] else [])}
            new = [i for i in dict.fromkeys(invoices) if re.sub(r"[^A-Z0-9]", "", i) not in existing]
            if new:
                filled.append(("Factory invoice", ", ".join(new), where))
                if apply:
                    ids = [odoo.create(INVOICE, {"booking_id": b["id"], "invoice_number": n, "source": where}) for n in new]
                    if len(ids) == 1 and not existing:  # one invoice: the shipment's containers are its containers
                        free = odoo.call(CONTAINER, "search", [("booking_id", "=", b["id"]), ("state", "=", "active"),
                                                                ("invoice_id", "=", False)])
                        if free:
                            odoo.write(CONTAINER, free, {"invoice_id": ids[0]})
            if not b["warehouse_id"]:
                warehouse, how = history.warehouse(row["fd"])
                if warehouse:
                    updates["warehouse_id"] = warehouse
                    filled.append(("Deliver To", warehouse, f"{where} FD '{row['fd']}': {how}"))
            kind, rate = rate_of(row["freight"])
            if not b["cost_per_container"] and kind == "rate" and rate >= LOWEST_RATE:
                updates["cost_per_container"] = rate
                b["cost_per_container"] = rate
                filled.append(("Cost per Container", rate, where))
            if not b["factory_id"] and row["factory_email"]:
                domain = row["factory_email"].rpartition("@")[2]
                mapping = resolve_mapping(mappings, "factory", sender_domain=domain)
                if "TANGER" in row["departure"] or "MOROCCO" in row["departure"]:
                    mapping = resolve_mapping(mappings, "factory", name="SENTURY TIRE MOROCCO") or mapping
                if mapping:
                    updates["factory_id"] = mapping["partner_id"]
                    filled.append(("Factory", mapping["partner_name"], f"{where} Factory {row['factory_email']}"))
            check = forwarder_check(b, row, history.forwarder_by_code)
            if apply and b["forwarder_id"] and (not check or check[0] == "fill"):
                settle_report_conflicts(odoo, b["id"])
            if check and check[0] == "fill":
                updates.update(forwarder_id=check[1], forwarder_name=check[2])
                b["forwarder_id"] = [check[1], check[2]]
                filled.append(("Forwarder", check[2], f"{where} FORWARDER {row['forwarder']}"))
            elif check:
                summary["Forwarder differs from the Booking Report"] += 1
                if apply and not odoo.call(CONFLICT, "search_count", [("booking_id", "=", b["id"]),
                                                                       ("field_name", "=", "forwarder_name"),
                                                                       ("new_value", "=", check[2])]):
                    odoo.create(CONFLICT, {"booking_id": b["id"], "field_name": "forwarder_name",
                                           "field_label": "Forwarder Name", "existing_value": check[1],
                                           "new_value": check[2], "source": f"{where} FORWARDER {row['forwarder']}"})
            if not b["eta"] and row.get("eta"):
                try:
                    days = (date.fromisoformat(row["eta"]) - date.fromisoformat(str(b["etd"])[:10])).days if b["etd"] else None
                except ValueError:
                    days = None
                if days is None or days >= min_sea_days(b["port_of_loading"]):
                    updates.update(eta=row["eta"], eta_source=where)
                    filled.append(("ETA", row["eta"], f"{where} (the team's plan, until a paper gives the arrival)"))
        invoice_ids = odoo.call(INVOICE, "search", [("booking_id", "=", b["id"])])
        free = odoo.call(CONTAINER, "search", [("booking_id", "=", b["id"]), ("state", "=", "active"),
                                                ("invoice_id", "=", False)])
        if len(invoice_ids) == 1 and free:  # one factory invoice: every container of the shipment is on it
            filled.append(("Containers tagged to the factory invoice", len(free), "the booking's only invoice"))
            if apply:
                odoo.write(CONTAINER, free, {"invoice_id": invoice_ids[0]})
        forwarder = b["forwarder_id"][0] if b["forwarder_id"] else None
        if forwarder and not b["additional_charges"] and not b["no_additional_charges"] and forwarder in history.fee_by_forwarder:
            fee = history.fee_by_forwarder[forwarder]
            if fee:
                updates.update(additional_charges=fee, freight_currency_id=usd[0]["id"] if usd else False)
                filled.append(("Additional Charges", fee, "the forwarder's handling fee on the team's POs"))
            else:
                updates["no_additional_charges"] = True
                filled.append(("No Additional Charges", True, "the team's POs to this forwarder carry no fee"))
        containers = odoo.call(CONTAINER, "search_count", [("booking_id", "=", b["id"]), ("state", "=", "active")])
        if not b["freight_cost"] and b["cost_per_container"] and containers:
            updates.update(freight_cost=b["cost_per_container"] * containers, freight_currency_id=usd[0]["id"] if usd else False)
            filled.append(("Freight Cost", b["cost_per_container"] * containers, f"rate x {containers} container(s)"))
        if b["house_bl_assumed"] and not b["house_bl_confirmed"] and b["forwarder_id"] and CARRIER.search(b["forwarder_id"][1]):
            updates["house_bl_confirmed"] = True
            filled.append(("HBL confirmed", b["house_bl"], "carrier-direct: the carrier's B/L is the only bill"))
        if filled:
            done[b["id"]] = filled
            summary.update(field for field, _v, _w in filled)
            if apply:
                if updates:
                    odoo.write(BOOKING, [b["id"]], updates)
                shown = lambda field, value: warehouses.get(value, value) if field == "Deliver To" else value  # noqa: E731
                odoo.call(BOOKING, "message_post", [b["id"]], body="Filled by the OCR service from the team's records: "
                          + "; ".join(f"{field} {shown(field, value)} ({where})" for field, value, where in filled))
    return done, summary
