"""Freight Booking's part of a pipeline pass: each shipping document, and each email's own subject and body, goes to
the company's Booker (rules.py), which finds or starts the booking and consolidates it."""

from app.freight_booking.history import BookingHistory
from app.freight_booking.reading import NOT_RELEVANT, email_values
from app.freight_booking.rules import Booker


class FreightHandler:
    def __init__(self, run):
        self.run = run
        self._bookers = {}

    def booker(self, company_id):
        if company_id not in self._bookers:
            self._bookers[company_id] = Booker(self.run.odoo, company_id, self.run.mappings, self.newest_confirmation,
                                               BookingHistory(self.run.db, self.run.connection_id))
        return self._bookers[company_id]

    def newest_confirmation(self, doc, booking_id):
        """Whether no booking confirmation already on this booking was received later."""
        later = self.run.db.execute(
            "SELECT 1 FROM file f JOIN message m ON m.id = f.message_id JOIN file_target t ON t.file_id = f.id "
            "AND t.connection_id = ? WHERE t.odoo_id = ? AND f.id != ? AND f.doc_class = 'booking_confirmation' "
            "AND t.state = 'done' AND m.received > ? LIMIT 1",
            (self.run.connection_id, booking_id, doc.get("file_id") or 0, doc["received"] or "")).fetchone()
        return not later

    def take(self, message, doc):
        """``(state, note, booking id)`` for a document the AI read."""
        if doc["doc_class"] in NOT_RELEVANT:
            return "skipped", f"Not relevant: {doc['doc_class']}.", None
        return self.booker(message["company_id"]).take(doc)

    def take_email(self, message, base, body):
        """``(state, note)`` for what the email's own subject and newest message state; None when they state nothing."""
        values = email_values(self.run.formats, message["booking_number_pattern"], base["subject"], body,
                              base["sender"], message["received"])
        if not values:
            return None
        doc = dict(base, doc_class="email_message", values=values, source=f"Email: {base['subject']}"[:120], file_id=None)
        state, note, _booking_id = self.booker(message["company_id"]).take(doc)
        return state, note
