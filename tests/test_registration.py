import pytest

from conftest import at
from convene.service import NotAllowed, RuleViolation


def test_seats_then_waitlist_in_order(svc, workshop, attendees):
    guests = attendees(5)
    regs = [svc.register(g, workshop) for g in guests]
    assert [r.status for r in regs] == ["confirmed", "confirmed", "confirmed", "waitlisted", "waitlisted"]
    assert [svc.waitlist_position(r.id) for r in regs] == [None, None, None, 1, 2]


def test_cancelling_a_confirmed_place_promotes_the_next_in_line(svc, clock, workshop, attendees):
    guests = attendees(5)
    regs = [svc.register(g, workshop) for g in guests]
    clock.advance(hours=1)
    promoted = svc.cancel_registration(guests[1], regs[1].id)
    assert promoted.user_id == guests[3]
    assert svc.registration(regs[3].id).status == "confirmed"
    assert svc.waitlist_position(regs[4].id) == 1                # moved up
    assert svc.seats(workshop).confirmed == 3


def test_cancelling_a_waitlisted_place_promotes_nobody(svc, workshop, attendees):
    regs = [svc.register(g, workshop) for g in attendees(5)]
    assert svc.cancel_registration(regs[3].user_id, regs[3].id) is None
    assert svc.waitlist_position(regs[4].id) == 1
    assert svc.seats(workshop).confirmed == 3


def test_rejoining_goes_to_the_back_of_the_queue_with_a_new_ticket(svc, clock, workshop, attendees):
    guests = attendees(5)
    regs = [svc.register(g, workshop) for g in guests]
    old_ticket = regs[0].ticket_code
    svc.cancel_registration(guests[0], regs[0].id)               # guest 4 takes the seat
    clock.advance(minutes=5)
    again = svc.register(guests[0], workshop)
    assert again.id == regs[0].id                                 # same row, reused
    assert again.status == "waitlisted"
    assert svc.waitlist_position(again.id) == 2                   # behind guest 5
    assert again.ticket_code != old_ticket                        # the old ticket no longer works


def test_cannot_register_twice(svc, workshop, attendees):
    g = attendees(1)[0]
    svc.register(g, workshop)
    with pytest.raises(RuleViolation, match="already confirmed"):
        svc.register(g, workshop)


def test_registration_closes_on_time(svc, clock, workshop, attendees):
    clock.set(at(9, 16, 59))
    svc.register(attendees(1)[0], workshop)
    clock.set(at(9, 17, 0))
    with pytest.raises(RuleViolation, match="closed"):
        svc.register(attendees(1)[0], workshop)


def test_drafts_are_not_open(svc, people, hall, attendees):
    draft = svc.create_event(people["organiser"], hall, "Not yet announced", at(20, 9), at(20, 11))
    with pytest.raises(RuleViolation, match="isn't open"):
        svc.register(attendees(1)[0], draft.id)


def test_cannot_hold_places_at_two_events_at_once(svc, people, hall, workshop, attendees):
    overlapping = svc.create_event(people["organiser"], hall, "Vendor Expo", at(10, 11), at(10, 15))
    svc.publish(people["organiser"], overlapping.id)
    later = svc.create_event(people["organiser"], hall, "Evening Mixer", at(10, 18), at(10, 20))
    svc.publish(people["organiser"], later.id)
    g = attendees(1)[0]
    svc.register(g, workshop)
    with pytest.raises(RuleViolation, match="already confirmed for \"Phishing Awareness Workshop\""):
        svc.register(g, overlapping.id)
    assert svc.register(g, later.id).status == "confirmed"


def test_only_the_attendee_or_organiser_can_cancel(svc, people, workshop, attendees):
    me, someone_else = attendees(2)
    reg = svc.register(me, workshop)
    with pytest.raises(NotAllowed):
        svc.cancel_registration(someone_else, reg.id)
    svc.cancel_registration(people["organiser"], reg.id)        # organiser may
    assert svc.registration(reg.id).status == "cancelled"


def test_cannot_cancel_after_the_start(svc, clock, workshop, attendees):
    g = attendees(1)[0]
    reg = svc.register(g, workshop)
    clock.set(at(10, 10))
    with pytest.raises(RuleViolation, match="has started"):
        svc.cancel_registration(g, reg.id)


def test_tickets_are_unguessable(svc, workshop, attendees):
    codes = [svc.register(g, workshop).ticket_code for g in attendees(3)]
    assert len(set(codes)) == 3
    assert all(len(c) >= 20 for c in codes)


def test_audit_trail_records_bookings_and_promotions(svc, workshop, attendees):
    regs = [svc.register(g, workshop) for g in attendees(4)]
    svc.cancel_registration(regs[0].user_id, regs[0].id)
    actions = [a.action for a in svc.audit_trail(workshop)]
    assert actions.count("registered") == 4
    assert "promoted_from_waitlist" in actions
    assert actions[-1] == "registration_cancelled"


def test_returned_objects_can_be_read_after_the_transaction(svc, people, room, workshop, attendees):
    """Callers (the web pages, the demo) read related names from what the service returns."""
    reg = svc.register(attendees(1)[0], workshop)
    assert reg.user.name == "Guest 1"
    assert reg.event.title == "Phishing Awareness Workshop"
    assert reg.event.venue.name == "Training Room 2"
    event = svc.create_event(people["organiser"], room, "Follow-up", at(10, 15), at(10, 16))
    assert (event.venue.name, event.organiser.name) == ("Training Room 2", "Andrea Cupid")
