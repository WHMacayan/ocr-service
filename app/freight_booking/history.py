"""What a booking's documents said before, from this connection's own records (see rules._History)."""

import json

from app.shared.documents.text import normalize_identifier


class BookingHistory:
    """What a booking's documents said before, from this connection's own records."""

    _FILES = ("FROM file f JOIN message m ON m.id = f.message_id JOIN mailbox b ON b.id = m.mailbox_id "
              "AND b.purpose = 'freight_booking' JOIN file_target t ON t.file_id = f.id AND t.connection_id = ? "
              "WHERE t.odoo_id = ? AND t.state = 'done'")

    def __init__(self, db, connection_id):
        self.db = db
        self.connection_id = connection_id

    def received(self, booking_id, source):
        if not source:
            return None
        row = self.db.execute(f"SELECT MAX(m.received) {self._FILES} AND f.filename = ?",
                              (self.connection_id, booking_id, source)).fetchone()
        return row[0] if row else None

    def pre_carriers(self, booking_id):
        found = set()
        for row in self.db.execute(f"SELECT f.extracted {self._FILES}", (self.connection_id, booking_id)):
            values = json.loads(row["extracted"] or "null") or {}
            found |= {normalize_identifier(values[k]) for k in ("pre_carrier_vessel", "pre_carrier_voyage") if values.get(k)}
        return found
