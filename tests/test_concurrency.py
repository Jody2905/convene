"""Many people booking the last seats at the same moment must never overbook."""

import threading

from convene.service import RuleViolation


def test_simultaneous_bookings_never_overbook(svc, workshop, attendees):
    guests = attendees(20)
    start = threading.Barrier(len(guests))
    results, errors = [], []

    def book(guest):
        start.wait()                       # everyone presses "Register" together
        try:
            results.append(svc.register(guest, workshop).status)
        except RuleViolation as e:         # pragma: no cover - would mean a rule fired unexpectedly
            errors.append(e)

    threads = [threading.Thread(target=book, args=(g,)) for g in guests]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert results.count("confirmed") == 3
    assert results.count("waitlisted") == 17
    seats = svc.seats(workshop)
    assert (seats.confirmed, seats.waitlisted) == (3, 17)
    positions = sorted(svc.waitlist_position(r.id) for r in svc.attendees(workshop, "waitlisted"))
    assert positions == list(range(1, 18))     # a clean queue: no gaps, no duplicates


def test_simultaneous_cancellations_promote_each_person_once(svc, clock, workshop, attendees):
    regs = [svc.register(g, workshop) for g in attendees(8)]          # 3 confirmed, 5 waiting
    start = threading.Barrier(3)

    def cancel(reg):
        start.wait()
        svc.cancel_registration(reg.user_id, reg.id)

    threads = [threading.Thread(target=cancel, args=(r,)) for r in regs[:3]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    confirmed = svc.attendees(workshop, "confirmed")
    assert sorted(r.id for r in confirmed) == sorted(r.id for r in regs[3:6])
    assert svc.seats(workshop).waitlisted == 2
