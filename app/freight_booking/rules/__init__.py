"""The rules a freight booking is built by, one topic per file. Everything is re-exported here, so
`from app.freight_booking.rules import Booker, BOOKING` keeps working.

    booker.py          finding the booking a document belongs to (or starting one); the hourly team-PO look-up
    consolidation.py   applying one document to its booking: each value filled, kept, moved or disputed
    team_pos.py        the team's own freight POs: finding the one for a booking, copying what it knows
    identifiers.py     booking, B/L, container and invoice numbers, as the team writes them
    ports.py           ports as their city, inland rail ramps, destination ports, days at sea
    values.py          the same value written another way, and impossible values (dates, vessels)
    parties.py         carriers, customs brokers, the importer's own names
    models.py          the Odoo models a booking is made of; which document types state which values
    words.py           words a name carries that say nothing about which one it is
"""

from app.freight_booking.rules.models import (BOOKING, CONTAINER, INVOICE, BL, CONFLICT, CHANGE, _SCHEDULE,
                                              FACTORY_DOCS, TRUSTED, OPEN, UPDATED_BY_CONFIRMATION,
                                              SCHEDULE_FIELDS, _READ)  # noqa: F401
from app.freight_booking.rules.words import (_LEGAL, _PLACES, _US_STATES, _OPERATORS, _words)  # noqa: F401
from app.freight_booking.rules.parties import (CARRIER, OWN_NAMES, CUSTOMS_BROKERS, CUSTOMS_BROKER_DOMAINS,
                                               party_key)  # noqa: F401
from app.freight_booking.rules.ports import (_PORT_ALIASES, _LOCODES, INLAND_POINTS, INLAND_BY_REFERENCE,
                                             _REFERENCE_CITY, reference_city, is_inland_point, _one_edit,
                                             DESTINATION_PORTS, _NORTH_AMERICA, is_destination_port, _TERMINAL,
                                             min_sea_days, port_key)  # noqa: F401
from app.freight_booking.rules.identifiers import (_SCAC, _INVOICE_NUMBER, _CONTAINER_NUMBER, is_container_number,
                                                   _HOUSE_PREFIX, _HOUSE_IN_NAME, _BOOKING_IN_NAME, clean_bl,
                                                   house_bill_in_name, _LOOK_ALIKE, same_identifier,
                                                   reference_number)  # noqa: F401
from app.freight_booking.rules.values import (swapped_date, vessel_key, _contains, same_value, _DATE_WINDOW,
                                              plausible_date, _NOT_A_VESSEL, plausible_vessel)  # noqa: F401
from app.freight_booking.rules.team_pos import (copy_po_details, _PurchaseHistory)  # noqa: F401
from app.freight_booking.rules.booker import (_History, Booker, link_team_pos)  # noqa: F401
from app.freight_booking.rules.consolidation import (_Consolidation)  # noqa: F401
from app.shared.odoo.mappings import m2o_id, resolve_mapping  # noqa: F401
