"""The clean-up pass: which open booking conflicts the updated rules settle, and how."""

from app.freight_booking.cleanup import close_false_conflicts, verdict
from app.freight_booking.rules import BOOKING, CONFLICT

from fakes import mapping, odoo


class History:
    def __init__(self, received=None, feeders=()):
        self._received, self._feeders = received or {}, set(feeders)

    def received(self, booking_id, source):
        return self._received.get(source)

    def pre_carriers(self, booking_id):
        return self._feeders


def judge(name, old, new, booking=None, received_new="2026-11-08 05:00:00", history=None, feeders=(set(), []),
          mappings=()):
    return verdict(name, old, new, dict({"id": 1}, **(booking or {})), received_new, history or History(), feeders,
                   mappings)


def test_the_rules_settle_what_is_not_a_real_disagreement():
    assert judge("port_of_discharge", "USCHS CHARLESTON, SC", "CHARLESTON, SC, US")[:2] == (
        "the same value written another way", False)
    assert judge("etd", "2026-11-03", "2026-03-11", received_new="2026-11-02 08:12:44")[:2] == (
        "a date read with its day and month swapped", False)
    assert judge("vessel", "WAN HAI A19", "SGSIN SINGAPORE")[0] == "not a vessel name"
    assert judge("vessel", "ONE MEISHAN", "PHUC KHANH",
                 feeders=(set(), ["PRECARRIERPHUCKHANH441S9V0LATESTETAETD"]))[0] == "the feeder vessel beside the ocean vessel"
    assert judge("forwarder_name", "DE WELL CONTAINER SHIPPING, INC.", "HAPAG-LLOYD (VIETNAM) LTD")[:2] == (
        "the carrier naming itself beside the forwarder", False)
    dewell = mapping(document_name="DE WELL", partner_id=4)
    assert judge("forwarder_name", "HAPAG-LLOYD (VIETNAM) LTD", "DE WELL SHIPPING INC.", mappings=[dewell])[:2] == (
        "the forwarder replacing the carrier", True)
    # an unmapped name may be the shipper misread as forwarder: a person decides
    assert judge("forwarder_name", "HAPAG-LLOYD (THAILAND) LTD.", "GENERAL RUBBER (THAILAND) CO., LTD.",
                 mappings=[dewell])[0] is None


def test_the_schedule_follows_the_later_document_and_ignores_an_older_one():
    history = History(received={"bc.pdf": "2026-10-23 07:46:48"})
    booking = {"etd_source": "bc.pdf"}
    assert judge("etd", "2026-10-29", "2026-11-12", booking, "2026-11-08 05:14:47", history) == (
        "a later document moving the schedule", True, "2026-11-12")
    assert judge("etd", "2026-10-29", "2026-10-20", booking, "2026-10-20 01:00:00", history)[:2] == (
        "an older document than the value kept", False)


def test_a_real_disagreement_stays_for_a_person():
    assert judge("port_of_loading", "HO CHI MINH CITY", "VUNG TAU ,VIET NAM")[0] is None
    assert judge("master_bl", "HDMUBKKM18827600", "A2611310053")[0] is None


def test_the_pass_reports_first_and_writes_only_when_applied(db, monkeypatch):
    fake = odoo()
    b = fake.add(BOOKING, company_id=1, booking_number="BK1", port_of_discharge="USCHS CHARLESTON, SC")
    c = fake.add(CONFLICT, booking_id=b, field_name="port_of_discharge", field_label="Port Of Discharge",
                 existing_value="USCHS CHARLESTON, SC", new_value="CHARLESTON, SC, US", source="hbl.pdf", state="open")
    d = fake.add(CONFLICT, booking_id=b, field_name="master_bl", field_label="Master BL",
                 existing_value="HDMUBKKM18827600", new_value="A2611310053", source="hbl.pdf", state="open")
    report = close_false_conflicts(db, fake, 1)
    assert report == {"the same value written another way": 1, "still a conflict for a person": 1}
    assert fake.rows(CONFLICT)[c]["state"] == "open"                         # nothing written yet
    close_false_conflicts(db, fake, 1, apply=True)
    assert fake.rows(CONFLICT)[c]["state"] == "resolved" and fake.rows(CONFLICT)[d]["state"] == "open"
    assert any("Conflicts closed" in str(p) for p in fake.posts)
