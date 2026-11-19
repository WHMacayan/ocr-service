"""Applying one document to its booking: each value filled, kept, moved or disputed, by the rules."""

import json
import logging
import re
from datetime import date

from app.shared.documents.text import normalize_identifier, parse_date
from app.shared.odoo.mappings import m2o_id, resolve_mapping
from app.freight_booking.rules.identifiers import _INVOICE_NUMBER, is_container_number, same_identifier
from app.freight_booking.rules.models import (BL, BOOKING, CHANGE, CONFLICT, CONTAINER, FACTORY_DOCS, INVOICE,
                                              SCHEDULE_FIELDS, TRUSTED, UPDATED_BY_CONFIRMATION)
from app.freight_booking.rules.parties import CARRIER, CUSTOMS_BROKERS, CUSTOMS_BROKER_DOMAINS
from app.freight_booking.rules.ports import (INLAND_BY_REFERENCE, _TERMINAL, is_destination_port, is_inland_point,
                                             min_sea_days, reference_city)
from app.freight_booking.rules.team_pos import _PurchaseHistory, copy_po_details
from app.freight_booking.rules.values import plausible_date, plausible_vessel, same_value, swapped_date

_logger = logging.getLogger(__name__)


class _Consolidation:
    def __init__(self, booker, booking, doc):
        self.booker = booker
        self.odoo = booker.odoo
        self.booking = booking
        self.id = booking["id"]
        self.doc = doc
        self.values = doc["values"]
        self.source = doc["source"]
        self.updates = {}
        self.notes = []
        self.replaced = set()  # parties whose name this document replaced: their partner is found again
        self.history_order = None  # the team's freight PO _PurchaseHistory found, with its vendor

    def current(self, name):
        return self.updates[name] if name in self.updates else self.booking.get(name)

    def apply(self):
        kind = self.doc["doc_class"]
        trusted = {group for group, kinds in TRUSTED.items() if kind in kinds}
        if "schedule" in trusted or "factory_schedule" in trusted:
            pod = self.values.get("port_of_discharge")
            first_stop = bool(pod) and not is_destination_port(pod) and not is_inland_point(pod)
            for name in SCHEDULE_FIELDS:
                if name == "eta" and first_stop:
                    continue  # the ETA at that hub (YANTIAN on Hapag's first leg, 37643170), not in North America
                if name == "etd" and kind == "shipping_notice" and self.current("etd"):
                    continue  # an arrival notice's dates are the arrival: Hapag's moved 14546118's ETD to it
                self.set(name, self.values.get(name))
        if "booking_number" in trusted:
            self.fill_booking_number(normalize_identifier(self.values.get("booking_number")))
        if "reference" in trusted and self.values.get("shipment_reference") and not self.current("shipment_reference"):
            self.updates["shipment_reference"] = self.values["shipment_reference"]
        if self.values.get("rail_ramp") and trusted & {"schedule", "factory_schedule", "email_schedule"}:
            self.set_rail_ramp(self.values["rail_ramp"])
        reference = self.values.get("shipment_reference") or self.doc.get("subject") or ""
        if reference_city(reference) in INLAND_BY_REFERENCE:
            self.set_rail_ramp(reference_city(reference), source=f"shipment reference {reference[:80]}")
        if "email_schedule" in trusted:
            for name in ("etd", "eta"):
                self.set(name, self.values.get(name))
        if kind == "freight_invoice" and self.values.get("eta") and not self.current("eta") and self.from_forwarder():
            # The carrier's or forwarder's own invoice prints the arrival (Hapag's on 40%); a trucker's or a customs
            # broker's prints its own dates, so only the booking's forwarder fills an ETA none of the papers gave.
            pod = self.values.get("port_of_discharge")
            if not pod or is_destination_port(pod) or is_inland_point(pod):
                self.set("eta", self.values["eta"])
        if "bl" in trusted:
            invoices = {normalize_identifier(i) for i in self.values.get("invoices") or []}
            stem = normalize_identifier(re.sub(r"\.[A-Z0-9]{2,4}$", "", (self.doc.get("source") or "").upper().strip()))
            numbers = {name: normalize_identifier(self.values.get(name)) for name in ("master_bl", "house_bl")}
            if numbers["master_bl"] and stem == numbers["master_bl"] + "H" and not numbers["house_bl"]:
                # Kerry names its house bill "<number>H.pdf": the number read as the MBL is the house bill.
                numbers = {"master_bl": "", "house_bl": numbers["master_bl"]}
            for name, bl_type in (("master_bl", "master"), ("house_bl", "house")):
                number = numbers[name]
                if is_container_number(number):
                    number = ""  # a container number read where the bill of lading number goes
                if number in invoices or _INVOICE_NUMBER.match(number):
                    number = ""  # a factory invoice number (F26AR10THA0693, GRKA-P-2026436) is never a B/L
                if name == "master_bl" and number.isdigit() and number == normalize_identifier(
                        self.current("booking_number") or self.values.get("booking_number")):
                    number = ""  # the bare booking number: the MBL on 35 of 15,763 team POs only (13204963)
                self.set(name, number)
                self.add_bl(bl_type, number)
        for role in ("forwarder", "factory"):
            if role == "forwarder" and self.booking.get("purchase_order_id"):
                continue  # the team's PO is linked: its vendor is the one paid (a trucker's invoice made 31652425 Mentfield)
            if role in trusted:
                self.set_party(role, self.values.get(f"{role}_name"))
        invoice_ids = {}
        if "invoices" in trusted and self.values.get("invoices"):
            invoice_ids = self.add_invoices(self.values["invoices"])
        if "containers" in trusted and self.values.get("containers"):
            # A factory invoice's containers are that invoice's: its freight PO carries them.
            own = (list(invoice_ids.values())[0] if kind == "commercial_invoice" and len(invoice_ids) == 1
                   else None)
            self.add_containers(self.values["containers"], own)
        if "amounts" in trusted:
            self.set_amounts()
        self.mirror_bls()
        self.check_eta()
        if self.current("freight_mode") == "unknown" and (
                self.current("master_bl") or self.current("house_bl") or self.values.get("containers")):
            self.updates["freight_mode"] = "ocean"
        if not self.booking["purchase_order_id"] and (
                self.current("master_bl") or self.current("house_bl") or self.current("booking_number")):
            self.updates.update(_PurchaseHistory(self).suggestions())
        if self.updates:
            self.odoo.write(BOOKING, [self.id], self.updates)
        self.link_existing_po()
        for note in self.notes:
            self.odoo.call(BOOKING, "message_post", [self.id], body=note)

    def from_forwarder(self):
        """Whether this paper is the booking's own forwarder's (or carrier's): by its sender or its printed name."""
        held = self.current("forwarder_id")
        held = held[0] if isinstance(held, list) else held
        if not held:
            return False
        sender = resolve_mapping(self.booker.mappings, "forwarder", sender_domain=(self.doc["sender"] or "").rpartition("@")[2])
        printed = resolve_mapping(self.booker.mappings, "forwarder", name=self.values.get("forwarder_name") or "")
        return any(m is not None and m["partner_id"] == held for m in (sender, printed))

    def check_eta(self):
        """An ETA too soon after the ETD is a stop on the way, or a date the sailing has since moved past (14546118
        kept 21 Sep after it sailed on 2 Oct): it is dropped, so the arrival is taken from the next paper."""
        etd, eta = self.current("etd"), self.current("eta")
        if not etd or not eta or self.booking.get("eta_confirmed"):
            return
        try:
            days = (date.fromisoformat(str(eta)[:10]) - date.fromisoformat(str(etd)[:10])).days
        except ValueError:
            return
        least = min_sea_days(self.current("port_of_loading"))
        if days >= least:
            return
        self.updates["eta"] = False
        if "eta_source" in self.booker.labels():
            self.updates["eta_source"] = False
        self.notes.append(f"ETA {str(eta)[:10]} dropped: {days} day(s) after the ETD {str(etd)[:10]} is not the arrival "
                          f"(a crossing from {self.current('port_of_loading') or 'there'} takes {least} days or more).")

    def link_existing_po(self):
        """The team already raised this shipment's freight PO (same B/L, forwarder and container count, found by
        _PurchaseHistory): link it, so the booking shows PO Matched and no second PO is drafted (user, 17 Nov 2026).
        The module's Link Existing PO also copies the PO's vendor and rate per container onto the booking."""
        order = self.updates.get("history_purchase_order_id")
        if not order or self.booking.get("purchase_order_id"):
            return
        if self.booker.has_field(INVOICE, "purchase_order_id") and self.odoo.call(
                INVOICE, "search_count", [("booking_id", "=", self.id), ("purchase_order_id", "!=", False)]):
            return  # per-invoice POs exist already: linking another would bill the shipment twice
        try:
            self.odoo.call(BOOKING, "action_link_existing_po", [self.id])
        except Exception as exc:  # an older module without the action, or Odoo refusing it
            _logger.warning("Booking %s: existing freight PO %s not linked: %s", self.id, order, exc)
            return
        self.notes.append("PO already existing: the team's freight PO for this shipment (same B/L, forwarder and "
                          "containers) was linked by the OCR service; no new PO is needed.")
        vendor = (self.history_order or {}).get("partner_id")
        forwarder = self.current("forwarder_id")
        forwarder = forwarder[0] if isinstance(forwarder, list) else forwarder
        if vendor and forwarder and m2o_id(vendor) != forwarder:
            # The PO's vendor is the one paid: it replaces the forwarder the papers named.
            mapped = [m["partner_name"] for m in self.booker.mappings
                      if m["partner_id"] == forwarder and "partner_name" in m.keys() and m["partner_name"]]
            was = mapped[0] if mapped else self.current("forwarder_name")
            self.odoo.write(BOOKING, [self.id], {"forwarder_id": m2o_id(vendor), "forwarder_name": vendor[1]})
            self.notes.append(f"Forwarder set to {vendor[1]}, the vendor of the team's PO {self.history_order['name']} "
                              f"(the papers named {was}).")
        copied = copy_po_details(self.odoo, self.id, order)
        if copied:
            self.notes.append(f"From the team's PO: {', '.join(copied)}.")

    def take_value(self, name, value):
        self.updates[name] = value
        if f"{name}_source" in self.booker.labels():  # only some fields name their document
            self.updates[f"{name}_source"] = self.source

    def set(self, name, value):
        """Fill an empty field; keep the first value and open a conflict for a different one, unless it is
        the same value written another way, the feeder vessel beside the ocean one, a newer document moving
        the schedule (an older one never disputes it), or a carrier naming itself beside the forwarder."""
        if self.booking.get(f"{name}_confirmed"):
            return
        if name in ("etd", "eta"):
            value = plausible_date(name, parse_date(value), self.doc["received"]) or ""
            if name == "eta" and value:
                # The paper's own ETD, except an arrival notice's (its "ETD" is the arrival), and the booking's.
                own = None if self.doc["doc_class"] == "shipping_notice" else plausible_date(
                    "etd", parse_date(self.values.get("etd")), self.doc["received"])
                least = min_sea_days(self.values.get("port_of_loading") or self.current("port_of_loading"))
                for etd in {str(d)[:10] for d in (own, self.current("etd")) if d}:
                    try:
                        days = (date.fromisoformat(value[:10]) - date.fromisoformat(etd)).days
                    except ValueError:
                        continue
                    if days < least:
                        # Too soon after sailing to be the arrival (a feeder leg, another date): it never replaces
                        # the ETA held (97851501 lost 8 Nov to Hapag's 2-day feeder date, 17 Nov 2026).
                        return
        else:
            value = str(value or "").strip()
            if name == "vessel" and value and not plausible_vessel(value):
                return
        if not value:
            return
        old = self.current(name) or ""
        if name == "port_of_discharge" and is_inland_point(value):
            if not is_inland_point(old):
                self.set_rail_ramp(value)  # the inland ramp, printed where the seaport goes
                return
        elif name == "port_of_discharge" and is_inland_point(old):
            # The booking held the inland ramp as its port: the seaport takes the place, the ramp moves.
            self.set_rail_ramp(old, source=self.booking.get("port_of_discharge_source") or "the port of discharge")
            self.take_value(name, value)
            self.notes.append(f"Port of Discharge set to the seaport {value}; {old} is the rail ramp ({self.source}).")
            return
        if not old or self.booking.get(f"{name}_assumed"):
            self.take_value(name, value)
            if f"{name}_assumed" in self.booking:
                self.updates[f"{name}_assumed"] = False
                self.booking[f"{name}_assumed"] = False
            return
        if str(self.booking.get(f"{name}_source") or "").startswith("Booking Report") \
                and self.doc["doc_class"] != "email_message":
            # The team's plan filled it while no paper had: the paper replaces the plan.
            self.take_value(name, value)
            self.notes.append(f"{self.booker.labels().get(name, name)} set to {value} by {self.source} "
                              f"(was {old} from the {self.booking.get(f'{name}_source')}).")
            return
        if name in ("etd", "eta") and swapped_date(old, value):
            return  # the same date with day and month the other way round
        if name == "port_of_discharge" and is_destination_port(value) != is_destination_port(old):
            # The cargo comes to North America: a foreign port held as the port of discharge is a transshipment hub
            # (SINGAPORE, YANTIAN on a Hapag confirmation's first leg) or the port of loading (TANGER MED).
            if is_destination_port(value):
                self.take_value(name, value)
                self.notes.append(f"Port of Discharge set to {value}; {old} is not a destination port ({self.source}).")
            return
        if name in ("master_bl", "house_bl", "booking_number") and same_identifier(old, value):
            return  # one look-alike character read differently (HLCUBKK2609ATNKS, ATNK5): the first reading stays
        terminal_new, terminal_old = bool(_TERMINAL.search(value)), bool(_TERMINAL.search(old))
        if name in ("port_of_loading", "port_of_discharge") and terminal_new != terminal_old:
            if terminal_new:
                return  # a terminal in the port never disputes the port
            self.take_value(name, value)  # the port replaces the terminal that held its place
            return
        a, b = normalize_identifier(old), normalize_identifier(value)
        # An email, or the factory's own paper, fills empty fields but never disputes a value.
        email = self.doc["doc_class"] == "email_message" or self.doc["doc_class"] in FACTORY_DOCS
        if a == b or (b and b in a):
            return
        if name == "house_bl" and not email and a == normalize_identifier(self.current("master_bl")):
            # The HBL only repeated the MBL (no house bill known yet): the forwarder's own house bill takes it.
            self.take_value(name, value)
            self.notes.append(f"House B/L set to {value} (it only repeated the MBL {old}) by {self.source}.")
            return
        if a and a in b and not email:
            self.take_value(name, value)  # the same value, stated more fully
            return
        if same_value(name, old, value, self.booker.mappings):
            return
        label = self.booker.labels().get(name, name)
        if name in ("vessel", "voyage"):
            feeders = self.booker.history.pre_carriers(self.id)
            if b in feeders:
                return  # a bill of lading prints the first (feeder) vessel; the booking keeps the ocean one
            if a in feeders and not email:
                self.take_value(name, value)
                self.notes.append(f"{label} set to the ocean vessel {value} (was the feeder {old}) by {self.source}.")
                return
        if name == "forwarder_name":
            # A mapped forwarder's own papers (Kerry's notices from kln.com) also print a staff handle (BRIGHT #241)
            # or the carrier's agent (MARINE (HKG) LTD.): an unmapped name there never disputes; the forwarder's
            # own mapped name replaces such a name.
            sender = resolve_mapping(self.booker.mappings, "forwarder",
                                     sender_domain=(self.doc["sender"] or "").rpartition("@")[2])
            if sender:
                printed = resolve_mapping(self.booker.mappings, "forwarder", name=value)
                if not printed:
                    return
                if printed["partner_id"] == sender["partner_id"] \
                        and not resolve_mapping(self.booker.mappings, "forwarder", name=old):
                    self.take_value(name, value)
                    return
            if CARRIER.search(value) and not CARRIER.search(old):
                return  # the carrier's own document names the carrier; the forwarder stays
            # Only a mapped forwarder replaces a carrier: an unmapped name may be a shipper misread as forwarder. Its own
            # house bill does (De Well's drafts); an invoice does not: the two that did were a trucker's (Canary Yellow
            # on Hapag shipments, 17 Nov 2026).
            if CARRIER.search(old) and not CARRIER.search(value) and not email \
                    and self.doc["doc_class"] != "freight_invoice" \
                    and resolve_mapping(self.booker.mappings, "forwarder", name=value):
                self.take_value(name, value)
                self.replaced.add("forwarder")
                self.notes.append(f"Forwarder set to {value} (was the carrier {old}) by {self.source}.")
                return
        if name in UPDATED_BY_CONFIRMATION and not email:
            then = self.booker.history.received(self.id, self.booking.get(f"{name}_source"))
            now = self.doc.get("received")
            if then and now and str(now) != str(then):
                if str(now) > str(then):  # the shipment was re-planned: the later document stands
                    self.take_value(name, value)
                    self.notes.append(f"{label} updated from {old} to {value} by {self.source}, "
                                      f"a later document than {self.booking.get(f'{name}_source')}.")
                return  # an older document never disputes a newer one
        if name in UPDATED_BY_CONFIRMATION and self.doc["doc_class"] == "booking_confirmation" \
                and self.booker.newest_confirmation(self.doc, self.id):
            self.take_value(name, value)
            self.notes.append(f"{label} updated from {old} to {value} by {self.source}.")
            return
        if name == "forwarder_name":
            # The forwarder paid is the Booking Report's (the hourly fill sets it): a paper naming another is a note.
            self.notes.append(f"{self.source} names the forwarder {value}; the booking keeps {old}.")
            return
        if not email:  # an email never disputes a document
            self.conflict(name, old, value)

    def conflict(self, name, old, new):
        """One conflict per field and value, open or settled: reading the same
        file again, or a value a person already decided, adds nothing."""
        if self.odoo.call(CONFLICT, "search_count", [("booking_id", "=", self.id), ("field_name", "=", name),
                                                     ("new_value", "=", str(new))]):
            return
        self.odoo.create(CONFLICT, {"booking_id": self.id, "field_name": name,
                                    "field_label": self.booker.labels().get(name, name),
                                    "existing_value": str(old), "new_value": str(new), "source": self.source})

    def set_party(self, role, name):
        if not name:
            return
        if any(own and own in normalize_identifier(name) for own in self.booker.own_names()):
            return  # the importer printed as consignee or booking party
        if role == "forwarder" and any(broker in normalize_identifier(name) for broker in CUSTOMS_BROKERS):
            return  # the customs broker's own papers (DO invoice, ISF) name it, not the ocean forwarder
        domain = (self.doc["sender"] or "").rpartition("@")[2]
        by_domain = resolve_mapping(self.booker.mappings, role, sender_domain=domain)
        if role == "forwarder" and by_domain and CARRIER.search(name) and not CARRIER.search(by_domain["document_name"] or ""):
            # A forwarder sends the carrier's own confirmation (De Well forwards Hapag-Lloyd's, 37643170): the carrier
            # printed there is not the payable vendor, the sender is (check of the Ready for PO bookings, 17 Nov 2026).
            name = (by_domain["partner_name"] if "partner_name" in by_domain.keys() else None) or by_domain["document_name"]
        self.set(f"{role}_name", name)
        if self.current(f"{role}_id") and role not in self.replaced:
            return
        by_printed = resolve_mapping(self.booker.mappings, role, name=name)
        # One sender domain can serve several factories (Sentury Thailand and Morocco, Jinfei and
        # Vossen): when the printed name names another mapped vendor, the document wins.
        mapping = by_printed if by_printed and by_domain and by_printed is not by_domain else (by_domain or by_printed)
        by_name = mapping is not by_domain
        if not mapping:
            return
        self.updates[f"{role}_id"] = mapping["partner_id"]
        if role == "forwarder":
            if by_name:
                self.updates["has_uncertain_match"] = True
            if mapping["freight_product_id"]:
                self.updates["mapping_freight_product_id"] = mapping["freight_product_id"]
            if mapping["cost_per_container"]:
                self.updates["mapping_cost_per_container"] = mapping["cost_per_container"]
            if mapping["freight_mode"]:
                self.updates["freight_mode"] = mapping["freight_mode"]

    def add_bl(self, bl_type, number):
        if number and not self.odoo.call(BL, "search_count", [("booking_id", "=", self.id), ("bl_type", "=", bl_type),
                                                              ("bl_number", "=", number)]):
            self.odoo.create(BL, {"booking_id": self.id, "bl_type": bl_type, "bl_number": number,
                                  "source": self.source})

    def mirror_bls(self):
        """A shipment with a single bill: the other B/L is assumed equal until a
        document states it or a person confirms it."""
        for name, other in (("master_bl", "house_bl"), ("house_bl", "master_bl")):
            if self.current(other) and not self.current(name):
                self.updates.update({name: self.current(other), f"{name}_assumed": True,
                                     f"{name}_source": self.updates.get(f"{other}_source") or self.source})

    def set_rail_ramp(self, value, source=None):
        """The inland rail ramp or place of delivery beyond the seaport. A ramp a document prints replaces one
        only inferred from the shipment reference; any other ramp held is kept."""
        if not value or not self.booker.has_field(BOOKING, "rail_ramp"):
            return
        held, held_from = self.current("rail_ramp"), str(self.current("rail_ramp_source") or "")
        if held and not (source is None and held_from.startswith("shipment reference")):
            return
        self.updates["rail_ramp"] = value
        if self.booker.has_field(BOOKING, "rail_ramp_source"):
            self.updates["rail_ramp_source"] = source or self.source

    def fill_booking_number(self, number):
        """A booking a factory invoice started without a number takes the first one a trusted document
        states. A number already held is never replaced; a number another booking holds is left to a person."""
        if not number or self.current("booking_number"):
            return
        others = self.booker._search([("booking_number", "=", number), ("id", "!=", self.id)])
        if others:
            self.notes.append(f"{self.source} states booking number {number}, which booking #{others[0]} "
                              f"already holds: the two may be one shipment.")
            return
        self.updates["booking_number"] = number
        self.notes.append(f"Booking number {number} set by {self.source}.")

    def add_containers(self, containers, invoice_id=None):
        """New containers of a booking that has none are added. A factory invoice's containers are added
        tagged with their invoice, and so are those of a document listing every container the factory
        invoices already gave plus more (the shipment's other invoices). Any other new container is a
        replacement or an addition a person decides on (a container change)."""
        tagging = self.booker.has_field(CONTAINER, "invoice_id")
        invoice_id = invoice_id if tagging else None
        lines = self.odoo.search_read(CONTAINER, [("booking_id", "=", self.id)],
                                      ["container_no", "state"] + (["invoice_id"] if tagging else []))
        known = {line["container_no"]: line for line in lines}
        if invoice_id:  # the factory invoice's own containers belong to it, whoever gave or tagged them first
            # (the fill tags a booking's containers to its only invoice; a later invoice takes back its own)
            others = [known[c]["id"] for c in containers if c in known and known[c]["state"] == "active"
                      and m2o_id(known[c].get("invoice_id")) != invoice_id]
            if others:
                self.odoo.write(CONTAINER, others, {"invoice_id": invoice_id})
        new = [c for c in containers if c not in known]
        if not new:
            return
        active = [line for line in lines if line["state"] == "active"]
        superset = tagging and active and all(line.get("invoice_id") for line in active) \
            and {line["container_no"] for line in active} <= set(containers)
        if not active or invoice_id or superset:
            for number in new:
                values = {"booking_id": self.id, "container_no": number, "source": self.source}
                if invoice_id:
                    values["invoice_id"] = invoice_id
                self.odoo.create(CONTAINER, values)
            return
        active = [line["id"] for line in active]
        # A replacement or an addition: a person decides in Odoo.
        if self.odoo.call(CHANGE, "search_count", [("booking_id", "=", self.id), ("source", "=", self.source),
                                                   ("state", "=", "open")]):
            return
        self.odoo.create(CHANGE, {
            "booking_id": self.id, "source": self.source, "existing_container_ids": [(6, 0, active)],
            "new_container_lines": json.dumps([{"container_no": number} for number in new])})

    def add_invoices(self, numbers):
        """``{number: invoice line id}`` for the factory invoices ``numbers``, the new ones added with the
        seller as printed (the key is seller + invoice number)."""
        # One invoice however it is written: the Booking Report's GRKA-P-2026497 is the load plan's GRKAP2026497
        # (EBKG19094541 got both, the second without containers, 17 Nov 2026). The line keeps its first spelling.
        lines = {normalize_identifier(line["invoice_number"]): line["id"] for line in self.odoo.search_read(
            INVOICE, [("booking_id", "=", self.id)], ["invoice_number"])}
        seller = self.values.get("factory_name") if self.doc["doc_class"] == "commercial_invoice" else None
        out = {}
        for number in numbers:
            key = normalize_identifier(number)
            if key not in lines:
                values = {"booking_id": self.id, "invoice_number": number, "source": self.source}
                if seller and self.booker.has_field(INVOICE, "factory_name"):
                    values["factory_name"] = seller
                lines[key] = self.odoo.create(INVOICE, values)
            out[number] = lines[key]
        return out

    def set_amounts(self):
        amounts = {name: self.values[name] for name in ("freight_cost", "additional_charges") if self.values.get(name)}
        sender = (self.doc["sender"] or "").rpartition("@")[2].lower()
        if not amounts or sender in CUSTOMS_BROKER_DOMAINS:
            return  # a customs broker's DO invoice bills its own fees ($110 entry), not the ocean freight
        currency = self.odoo.search_read("res.currency", [("name", "=", (self.values.get("currency") or "").upper())],
                                         ["name", "rounding"], context={"active_test": False}, limit=1)
        if not currency:
            return  # an amount in an unknown currency is not an amount
        currency = currency[0]
        held = self.booking["freight_currency_id"]
        if held and held[0] != currency["id"]:
            self.notes.append(f"{self.source} states freight in {currency['name']}, the booking holds {held[1]}: "
                              f"its amounts were not taken.")
            return
        self.updates["freight_currency_id"] = currency["id"]
        # The freight PO is priced from the agreed rate per container, never from these amounts (freight PO study,
        # 16 Nov 2026): another amount is noted on the booking, never a conflict for a person.
        for name, amount in amounts.items():
            old = self.booking[name]
            if not old:
                self.updates[name] = amount
            elif abs(old - amount) > currency["rounding"]:
                self.notes.append(f"{self.source} states {self.booker.labels().get(name, name)} {amount:,.2f}; "
                                  f"the booking keeps {old:,.2f}.")
