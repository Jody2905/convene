import pytest

from conftest import at
from convene.service import NotAllowed, NotFound, RuleViolation


@pytest.fixture
def booked(svc, workshop, attendees):
    """3 confirmed, 1 waitlisted."""
    return [svc.register(g, workshop) for g in attendees(4)]


def test_check_in_at_the_door(svc, clock, people, workshop, booked):
    clock.set(at(10, 9, 15))
    result = svc.check_in(people["staff"], booked[0].ticket_code)
    assert result.message == "Welcome, Guest 1"
    assert svc.registration(booked[0].id).checked_in_at == at(10, 9, 15)
    assert svc.seats(workshop).checked_in == 1


def test_no_double_check_in(svc, clock, people, booked):
    clock.set(at(10, 9, 15))
    svc.check_in(people["staff"], booked[0].ticket_code)
    clock.advance(minutes=20)
    with pytest.raises(RuleViolation, match="already checked in at 09:15"):
        svc.check_in(people["staff"], booked[0].ticket_code)


def test_check_in_window(svc, clock, people, booked):
    clock.set(at(10, 8, 59))
    with pytest.raises(RuleViolation, match="opens at 09:00"):
        svc.check_in(people["staff"], booked[0].ticket_code)
    clock.set(at(10, 12, 1))
    with pytest.raises(RuleViolation, match="finished"):
        svc.check_in(people["staff"], booked[0].ticket_code)
    clock.set(at(10, 11, 30))                                     # late arrivals are fine
    svc.check_in(people["staff"], booked[0].ticket_code)


def test_waitlisted_cancelled_and_unknown_tickets_refused(svc, clock, people, booked):
    svc.cancel_registration(booked[1].user_id, booked[1].id)     # guest 4 promoted
    clock.set(at(10, 9, 30))
    with pytest.raises(RuleViolation, match="was cancelled"):
        svc.check_in(people["staff"], booked[1].ticket_code)
    with pytest.raises(NotFound):
        svc.check_in(people["staff"], "made-up-code")
    svc.check_in(people["staff"], booked[3].ticket_code)          # promoted ticket now works


def test_waitlisted_ticket_refused(svc, clock, people, workshop, attendees, booked):
    extra = svc.register(attendees(1)[0], workshop)
    clock.set(at(10, 9, 30))
    with pytest.raises(RuleViolation, match="on the waitlist"):
        svc.check_in(people["staff"], extra.ticket_code)


def test_only_staff_and_organisers_check_in(svc, clock, people, booked):
    clock.set(at(10, 9, 30))
    with pytest.raises(NotAllowed):
        svc.check_in(booked[1].user_id, booked[0].ticket_code)  # an attendee can't check others in
    svc.check_in(people["organiser"], booked[0].ticket_code)


def test_cancelled_event_refuses_check_in(svc, clock, people, workshop, booked):
    svc.cancel_event(people["organiser"], workshop, "Storm warning")
    clock.set(at(10, 9, 30))
    with pytest.raises(RuleViolation):
        svc.check_in(people["staff"], booked[0].ticket_code)
