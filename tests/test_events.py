import pytest
from sqlalchemy.exc import IntegrityError

from conftest import PASSWORD, at
from convene.models import Event, Registration
from convene.service import NotAllowed, RuleViolation


# ---- Accounts --------------------------------------------------------------------

def test_emails_are_unique_and_case_insensitive(svc):
    svc.create_user("Kim@Example.test", "Kim", "attendee", PASSWORD)
    with pytest.raises(RuleViolation, match="already exists"):
        svc.create_user("kim@example.TEST", "Kim again", "attendee", PASSWORD)


def test_authenticate(svc):
    svc.create_user("kim@example.test", "Kim", "attendee", PASSWORD)
    assert svc.authenticate("KIM@example.test", PASSWORD).name == "Kim"
    assert svc.authenticate("kim@example.test", "wrong-password") is None
    assert svc.authenticate("nobody@example.test", PASSWORD) is None


def test_weak_password_and_bad_role_refused(svc):
    with pytest.raises(RuleViolation, match="at least 10"):
        svc.create_user("a@example.test", "A", "attendee", "short")
    with pytest.raises(RuleViolation, match="Role"):
        svc.create_user("a@example.test", "A", "admin", PASSWORD)


# ---- Creating events ------------------------------------------------------------------

def test_only_organisers_create_events_and_venues(svc, people, room, attendees):
    guest = attendees(1)[0]
    with pytest.raises(NotAllowed):
        svc.create_event(guest, room, "My party", at(10, 18), at(10, 20))
    with pytest.raises(NotAllowed):
        svc.create_venue(people["staff"], "Back room", 10)


def test_event_defaults_to_venue_capacity_and_starts_as_draft(svc, people, hall):
    e = svc.create_event(people["organiser"], hall, "Budget Town Hall", at(12, 18), at(12, 20))
    assert e.capacity == 120 and e.status == "draft"
    assert e.registration_closes_at == e.starts_at


@pytest.mark.parametrize("starts, ends, capacity, closes, message", [
    (at(1, 10), at(1, 12), 10, None, "start in the future"),            # 1 Nov is in the past
    (at(10, 12), at(10, 10), 10, None, "end after it starts"),
    (at(10, 10), at(11, 12), 10, None, "at most 24 hours"),
    (at(10, 10), at(10, 12), 0, None, "at least 1"),
    (at(10, 10), at(10, 12), 21, None, "only holds 20"),
    (at(10, 10), at(10, 12), 10, at(10, 11), "close before the event starts"),
])
def test_invalid_events_are_refused(svc, people, room, starts, ends, capacity, closes, message):
    with pytest.raises(RuleViolation, match=message):
        svc.create_event(people["organiser"], room, "Bad event", starts, ends, capacity=capacity,
                         registration_closes_at=closes)


def test_venue_cannot_be_double_booked(svc, people, room, workshop):
    """The workshop has the room 10:00-12:00; 30 minutes are needed to reset it."""
    clash = svc.create_event(people["organiser"], room, "Lunch and Learn", at(10, 12, 15), at(10, 13))
    with pytest.raises(RuleViolation, match="is booked for \"Phishing Awareness Workshop\""):
        svc.publish(people["organiser"], clash.id)

    ok = svc.create_event(people["organiser"], room, "Afternoon session", at(10, 12, 30), at(10, 14))
    assert svc.publish(people["organiser"], ok.id).status == "published"


def test_events_at_different_venues_or_after_cancellation_dont_clash(svc, people, room, hall, workshop):
    elsewhere = svc.create_event(people["organiser"], hall, "Same time, other venue", at(10, 10), at(10, 12))
    svc.publish(people["organiser"], elsewhere.id)
    svc.cancel_event(people["organiser"], workshop, "Facilitator unavailable")
    again = svc.create_event(people["organiser"], room, "Replacement", at(10, 10), at(10, 12))
    assert svc.publish(people["organiser"], again.id).status == "published"


def test_only_the_events_organiser_can_change_it(svc, people, workshop):
    with pytest.raises(NotAllowed, match="event's organiser"):
        svc.change_capacity(people["other_organiser"], workshop, 10)
    with pytest.raises(NotAllowed):
        svc.cancel_event(people["other_organiser"], workshop, "Not mine to cancel")


def test_database_constraints_back_up_the_rules(svc, engine, people, room, workshop, attendees):
    """Even code that skips the service can't store impossible data."""
    from sqlalchemy.orm import Session
    with Session(engine) as s, pytest.raises(IntegrityError):
        e = s.get(Event, workshop)
        e.ends_at = e.starts_at
        s.commit()
    guest = attendees(1)[0]
    svc.register(guest, workshop)
    with Session(engine) as s, pytest.raises(IntegrityError):
        s.add(Registration(event_id=workshop, user_id=guest, status="confirmed", ticket_code="dup",
                           queued_at=at(2, 9)))
        s.commit()


# ---- Capacity and cancellation -----------------------------------------------------------

def test_capacity_cannot_drop_below_confirmed(svc, people, workshop, attendees):
    for g in attendees(3):
        svc.register(g, workshop)
    with pytest.raises(RuleViolation, match="3 people are already confirmed"):
        svc.change_capacity(people["organiser"], workshop, 2)


def test_raising_capacity_confirms_people_from_the_waitlist(svc, people, workshop, attendees):
    guests = attendees(6)
    regs = [svc.register(g, workshop) for g in guests]
    assert [r.status for r in regs] == ["confirmed"] * 3 + ["waitlisted"] * 3
    promoted = svc.change_capacity(people["organiser"], workshop, 5)
    assert [r.user_id for r in promoted] == guests[3:5]          # first come, first served
    s = svc.seats(workshop)
    assert (s.confirmed, s.waitlisted, s.available) == (5, 1, 0)


def test_cancelling_an_event_cancels_everyone_and_lists_who_to_tell(svc, people, workshop, attendees):
    guests = attendees(4)
    for g in guests:
        svc.register(g, workshop)
    notify = svc.cancel_event(people["organiser"], workshop, "Facilitator unavailable")
    assert sorted(u.id for u in notify) == sorted(guests)
    assert svc.seats(workshop).confirmed == 0
    with pytest.raises(RuleViolation, match="isn't open"):
        svc.register(attendees(1)[0], workshop)
    assert svc.audit_trail(workshop)[-1].detail == "Facilitator unavailable (4 registrations cancelled)"


def test_cancelling_needs_a_reason(svc, people, workshop):
    with pytest.raises(RuleViolation, match="reason"):
        svc.cancel_event(people["organiser"], workshop, " ")
