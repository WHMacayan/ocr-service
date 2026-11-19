"""Finding the booking a document belongs to (or starting one), and the hourly look-up of the team's POs."""

from app.shared.documents.text import (find_labelled_booking_number, normalize_identifier, plausible_identifier,
                                       valid_container_number)
from app.shared.odoo.mappings import m2o_id, resolve_mapping
from app.freight_booking.rules.consolidation import _Consolidation
from app.freight_booking.rules.identifiers import (_BOOKING_IN_NAME, _SCAC, clean_bl, house_bill_in_name,
                                                   same_identifier)
from app.freight_booking.rules.models import BOOKING, FACTORY_DOCS, INVOICE, OPEN, TRUSTED, _READ
from app.freight_booking.rules.parties import OWN_NAMES
from app.freight_booking.rules.team_pos import _PurchaseHistory
from app.freight_booking.rules.values import same_value, vessel_key
from app.freight_booking.rules.words import _OPERATORS, _words


class _History:
    """What a booking's documents said before. Without the pipeline's records nothing is known:
    the schedule then moves only with a newer booking confirmation, as before."""

    def received(self, booking_id, source):
        """When the document named ``source`` on this booking was received; None when unknown."""
        return None

    def pre_carriers(self, booking_id):
        """The feeder vessels and voyages this booking's documents printed, as identifiers."""
        return set()


class Booker:
    """Consolidates documents for one company. ``newest_confirmation(doc,
    booking_id)`` tells whether ``doc`` is the latest booking confirmation the
    booking has received; the pipeline answers it from its own records, and
    ``history`` (when given) what its documents said before: see _History."""

    def __init__(self, odoo, company_id, mappings, newest_confirmation, history=None):
        self.odoo = odoo
        self.company_id = company_id
        self.mappings = mappings
        self.newest_confirmation = newest_confirmation
        self.history = history or _History()
        self._fields = {}
        self._own_names = None

    def fields(self, model):
        """The fields ``model`` has in this Odoo: a module not yet upgraded lacks the newer ones."""
        if model not in self._fields:
            self._fields[model] = self.odoo.call(model, "fields_get", attributes=["string", "required"])
        return self._fields[model]

    def has_field(self, model, name):
        return name in self.fields(model)

    def labels(self):
        return {name: info["string"] for name, info in self.fields(BOOKING).items()}

    def number_optional(self):
        """Whether a booking may exist without a booking number (b2b_ocr_documents from Nov 2026)."""
        return not self.fields(BOOKING).get("booking_number", {}).get("required")

    def own_names(self):
        if self._own_names is None:
            self._own_names = [normalize_identifier(c["name"])
                               for c in self.odoo.search_read("res.company", [], ["name"])] + list(OWN_NAMES)
        return self._own_names

    def _search(self, domain):
        return self.odoo.call(BOOKING, "search", [("company_id", "=", self.company_id)] + domain)

    # ------------------------------------------------------------------
    # Finding the booking of a document
    # ------------------------------------------------------------------

    def take(self, doc):
        """``(state, note, booking_id)`` for ``doc``: ``done`` when it was
        consolidated, ``waiting`` when no single booking fits yet."""
        values = doc["values"]
        # Values read before the reading checked them (stored files read again) are checked here too.
        wrong = [k for k in ("booking_number", "master_bl", "house_bl", "bl_number")
                 if values.get(k) and not plausible_identifier(values[k])]
        if wrong:
            values = {k: v for k, v in values.items() if k not in wrong}
            doc = dict(doc, values=values)
        if values.get("containers") and not all(valid_container_number(c) for c in values["containers"]):
            values = dict(values, containers=[c for c in values["containers"] if valid_container_number(c)])
            doc = dict(doc, values=values)
        # B/L numbers as the team writes them, and what a forwarder's file name states: its house bill (De Well's
        # HBL#, Tianshi's THSE/VNSE) and, on Tianshi's cargo receipt, the booking.
        cleaned = {k: clean_bl(values[k]) for k in ("master_bl", "house_bl", "bl_number") if values.get(k)}
        name = doc.get("source") or ""
        if doc["doc_class"] in TRUSTED["bl"] and not values.get("house_bl") and house_bill_in_name(name):
            cleaned["house_bl"] = house_bill_in_name(name)
        named = _BOOKING_IN_NAME.match(name)
        if named and not values.get("booking_number"):
            cleaned["booking_number"] = normalize_identifier(named.group(1))
        if any(values.get(k) != v for k, v in cleaned.items()):
            values = dict(values, **cleaned)
            doc = dict(doc, values=values)
        factory_doc = doc["doc_class"] in FACTORY_DOCS
        if factory_doc and values.get("bl_number") and not values.get("booking_number"):
            # Sentury prints the booking number as "B/L#": 96 of 96 invoices checked against the Import
            # Logsheet (12 Nov 2026) equal its booking number, or the carrier's bill that is that number.
            values = dict(values, booking_number=values["bl_number"])
            doc = dict(doc, values=values)
        keys = {name: normalize_identifier(values.get(name))
                for name in ("booking_number", "master_bl", "house_bl", "bl_number")}
        ids = set()
        if keys["booking_number"]:
            ids |= set(self._search([("booking_number", "=", keys["booking_number"])]))
        for name in ("master_bl", "house_bl", "bl_number"):
            if keys[name]:
                # A B/L# printed without its type (a factory invoice) may be either bill.
                fields = ("master_bl", "house_bl") if name == "bl_number" else (name,)
                clauses = [(f, "=", keys[name]) for f in fields] + [("bl_ids.bl_number", "=", keys[name])]
                ids |= set(self._search(["|"] * (len(clauses) - 1) + clauses))
        if not ids:
            ids = self._nearly(keys.values())
        if not ids and values.get("containers"):
            ids |= set(self._search([("container_ids.container_no", "in", values["containers"])] + OPEN))
        if not ids and factory_doc and values.get("containers") and values.get("vessel"):
            # A late invoice of a shipment whose other invoices have their POs: the booking takes it and
            # drafts only its PO. A reused container alone is not enough there: the vessel must agree too.
            ids |= {b["id"] for b in self.odoo.search_read(
                BOOKING, [("company_id", "=", self.company_id), ("container_ids.container_no", "in", values["containers"]),
                          ("purchase_order_id", "=", False), ("state", "=", "po_created")], ["vessel"])
                if vessel_key(b["vessel"]) == vessel_key(values["vessel"])}
        if not ids and factory_doc and values.get("invoices"):
            # The same factory invoice again: a revision, or the packing list sent on its own.
            found = {m2o_id(line["booking_id"]) for line in self.odoo.search_read(
                INVOICE, [("invoice_number", "in", values["invoices"])], ["booking_id"])}
            ids |= set(self._search([("id", "in", sorted(found))])) if found else set()
        if not ids:
            ids = self._same_sailing(values, factory_doc)
        if len(ids) > 1:
            names = [self.name(b) for b in self.odoo.call(BOOKING, "read", sorted(ids), ["booking_number"])]
            return "waiting", f"Its identifiers point to bookings {', '.join(names)}.", None
        if not ids and self._may_create(doc, keys["booking_number"]):
            booking_id = self.odoo.create(BOOKING, {"booking_number": keys["booking_number"] or False,
                                                    "company_id": self.company_id})
            if doc["doc_class"] == "email_message":
                self.odoo.write(BOOKING, [booking_id], {"has_uncertain_match": True})
            ids = {booking_id}
        if not ids:
            if factory_doc and values.get("invoices") and not keys["booking_number"] and not self.number_optional():
                return "waiting", ("It prints no booking number, and this Odoo needs one to start a booking "
                                   "(update the Freight Booking module)."), None
            if any(keys.values()) or values.get("containers"):
                return "waiting", "No booking has its booking number, B/L or containers yet.", None
            return "waiting", "It names no booking number, B/L or container.", None
        booking_id = ids.pop()
        self.consolidate(booking_id, doc)
        return "done", None, booking_id

    @staticmethod
    def name(booking):
        return booking["booking_number"] or f"#{booking['id']} (no booking number yet)"

    def _same_sailing(self, values, factory_doc):
        """The one open booking on the same vessel, voyage and ports. A factory paper looks among bookings of
        the same factory (or none yet); a forwarder's document only among bookings still without a booking
        number, which a factory invoice started. Several fit, or a value is missing: none."""
        vessel, voyage = values.get("vessel"), normalize_identifier(values.get("voyage"))
        pol, pod = values.get("port_of_loading"), values.get("port_of_discharge")
        words = [w for w in _words(vessel) if w not in _OPERATORS]
        if not (words and voyage and pol and pod):
            return set()
        domain = [("company_id", "=", self.company_id), ("vessel", "ilike", max(words, key=len))]
        # A factory invoice may come after the shipment's other invoices have their POs (state po_created):
        # only a whole-shipment PO linked to the booking closes it.
        domain += [("purchase_order_id", "=", False)] if factory_doc else OPEN + [("booking_number", "=", False)]
        ids = set()
        for booking in self.odoo.search_read(BOOKING, domain, ["vessel", "voyage", "port_of_loading",
                                                               "port_of_discharge", "factory_name"]):
            if vessel_key(booking["vessel"]) != vessel_key(vessel) or normalize_identifier(booking["voyage"]) != voyage:
                continue
            if not (same_value("port_of_loading", booking["port_of_loading"], pol)
                    and same_value("port_of_discharge", booking["port_of_discharge"], pod)):
                continue
            if factory_doc and booking["factory_name"] and values.get("factory_name") and not same_value(
                    "factory_name", booking["factory_name"], values["factory_name"], self.mappings):
                continue
            ids.add(booking["id"])
        return ids if len(ids) == 1 else set()

    def _nearly(self, keys):
        """The one booking whose number or bill is one of these written another way (see
        same_identifier); none when no booking, or several, fit."""
        ids = set()
        for key in {k for k in keys if k}:
            core = _SCAC.sub("", key)
            if len(core) < 9:
                continue
            tail = core[-6:]  # the database narrows by the tail, the comparison decides
            for booking in self.odoo.search_read(BOOKING, [("company_id", "=", self.company_id), "|", "|", "|",
                                                           ("booking_number", "ilike", tail), ("master_bl", "ilike", tail),
                                                           ("house_bl", "ilike", tail), ("bl_ids.bl_number", "ilike", tail)],
                                                 ["booking_number", "master_bl", "house_bl"]):
                if any(same_identifier(key, booking[f]) for f in ("booking_number", "master_bl", "house_bl")):
                    ids.add(booking["id"])
            if len(core) >= 10:  # one character lost: the tail is not where it was
                for booking in self.odoo.search_read(BOOKING, [("company_id", "=", self.company_id), "|", "|",
                                                               ("booking_number", "ilike", core[:8]), ("master_bl", "ilike", core[:8]),
                                                               ("house_bl", "ilike", core[:8])],
                                                     ["booking_number", "master_bl", "house_bl"]):
                    if any(same_identifier(key, booking[f]) for f in ("booking_number", "master_bl", "house_bl")):
                        ids.add(booking["id"])
        return ids if len(ids) == 1 else set()

    def _may_create(self, doc, number):
        """A booking confirmation creates its booking; so does a factory invoice naming its invoice and
        the shipment (containers or vessel), with or without a booking number; another document only
        from a booking number its subject labels; an email only in a Subject Format that confirms
        bookings, from a mapped forwarder."""
        values = doc["values"]
        if doc["doc_class"] == "commercial_invoice" and values.get("invoices") \
                and (values.get("containers") or values.get("vessel")):
            return bool(number) or self.number_optional()
        if not number:
            return False
        if doc["doc_class"] == "booking_confirmation":
            return True
        if doc["doc_class"] == "email_message":
            domain = (doc["sender"] or "").rpartition("@")[2]
            return bool(doc["values"].get("confirms_booking")
                        and resolve_mapping(self.mappings, "forwarder", sender_domain=domain))
        return find_labelled_booking_number(doc["subject"]) == number

    # ------------------------------------------------------------------
    # Consolidation
    # ------------------------------------------------------------------

    def consolidate(self, booking_id, doc):
        fields = list(_READ) + [f for f in ("rail_ramp", "rail_ramp_source", "port_of_discharge_source")
                                if self.has_field(BOOKING, f)]
        booking = self.odoo.call(BOOKING, "read", [booking_id], fields)[0]
        run = _Consolidation(self, booking, doc)
        run.apply()


def link_team_pos(odoo, mappings):
    """The team uploads its freight POs from the logsheet after the papers arrive (user, 17 Nov 2026): every booking
    still without a PO is looked up again, and the team's PO, once it exists, is linked as PO already existing so no
    second PO is drafted. Run on the hourly pass. Returns how many bookings were linked."""
    bookers, linked = {}, 0
    fields = list(_READ) + ["company_id"]
    for booking in odoo.search_read(BOOKING, [("purchase_order_id", "=", False),
                                              ("state", "not in", ["po_created", "po_matched"])], fields):
        if not (booking["booking_number"] or booking["master_bl"] or booking["house_bl"]):
            continue
        company = booking["company_id"][0] if isinstance(booking["company_id"], list) else booking["company_id"]
        booker = bookers.setdefault(company, Booker(odoo, company, mappings, lambda _doc, _booking: False))
        run = _Consolidation(booker, booking, {"doc_class": "team_po_check", "values": {}, "source": "the team's POs",
                                               "subject": "", "sender": "", "received": None, "file_id": None})
        values = _PurchaseHistory(run).suggestions()
        if not values.get("history_purchase_order_id"):
            continue
        odoo.write(BOOKING, [booking["id"]], values)
        run.updates.update(values)
        run.link_existing_po()
        for note in run.notes:
            odoo.call(BOOKING, "message_post", [booking["id"]], body=note)
        linked += 1
    return linked
