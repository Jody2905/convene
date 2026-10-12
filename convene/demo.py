"""A walkthrough of the booking rules, printed step by step.

    python -m convene demo

Uses a throwaway database and a pretend clock, so it gives the same
result every time and never touches real data.
"""

import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .db import init_db, session_factory
from .service import ConveneService, RuleViolation

PASSWORD = "Demo-password-2026"


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def run(out=print):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        engine = init_db(f"sqlite:///{Path(folder) / 'demo.db'}")
        try:
            _walkthrough(ConveneService(session_factory(engine), clock=_Clock(datetime(2026, 11, 2, 9, 0))), out)
        finally:
            engine.dispose()       # release the file so the folder can be deleted (needed on Windows)


def _walkthrough(svc, out):
    clock = svc.clock

    def step(title):
        out("")
        out(f"== {title} " + "=" * max(0, 66 - len(title)))

    def refused(action):
        try:
            action()
            out("   !! expected this to be refused")
        except RuleViolation as e:
            out(f"   REFUSED: {e}")

    step("Setup (Mon 2 Nov, 09:00)")
    org = svc.create_user("ops@convene.test", "Andrea Cupid", "organiser", PASSWORD).id
    door = svc.create_user("door@convene.test", "Shane Browne", "staff", PASSWORD).id
    room = svc.create_venue(org, "Training Room 2", 20)
    out(f"   Organiser Andrea Cupid, door staff Shane Browne, venue {room.name} (holds {room.capacity})")
    ws = svc.create_event(org, room.id, "Phishing Awareness Workshop", datetime(2026, 11, 10, 10),
                          datetime(2026, 11, 10, 12), capacity=5,
                          registration_closes_at=datetime(2026, 11, 9, 17))
    svc.publish(org, ws.id)
    out(f"   Published \"{ws.title}\": Tue 10 Nov 10:00-12:00, 5 seats, registration closes Mon 9 Nov 17:00")

    step("Rule: a venue can't be double-booked")
    clash = svc.create_event(org, room.id, "Lunch and Learn", datetime(2026, 11, 10, 12, 15),
                             datetime(2026, 11, 10, 13, 15))
    out("   Trying to publish \"Lunch and Learn\" in the same room at 12:15...")
    refused(lambda: svc.publish(org, clash.id))

    step("Rule: confirmed while seats last, then a first-come waitlist")
    guests = [svc.create_user(f"guest{i}@example.test", name, "attendee", PASSWORD).id
              for i, name in enumerate(["Kerry-Ann", "Marlon", "Shanique", "Dwayne", "Tricia",
                                        "Jamal", "Keisha", "Andre"], start=1)]
    regs = {}
    for g in guests:
        clock.now += timedelta(minutes=7)
        r = svc.register(g, ws.id)
        regs[g] = r
        where = f"waitlist #{svc.waitlist_position(r.id)}" if r.status == "waitlisted" else "confirmed"
        out(f"   {clock.now:%H:%M}  {r.user.name:<10} -> {where}")
    refused(lambda: svc.register(guests[0], ws.id))

    step("Rule: when someone cancels, the next person in line gets the seat")
    clock.now += timedelta(hours=3)
    promoted = svc.cancel_registration(guests[1], regs[guests[1]].id)
    out(f"   Marlon cancels -> {promoted.user.name} is confirmed automatically")
    out(f"   Keisha is now waitlist #{svc.waitlist_position(regs[guests[6]].id)}")

    step("Rule: raising capacity confirms people from the waitlist")
    moved = svc.change_capacity(org, ws.id, 7)
    out(f"   Capacity 5 -> 7: confirmed {', '.join(r.user.name for r in moved)}")
    refused(lambda: svc.change_capacity(org, ws.id, 4))
    nadia = svc.create_user("nadia@example.test", "Nadia", "attendee", PASSWORD).id
    nadia_reg = svc.register(nadia, ws.id)
    out(f"   Nadia registers now -> waitlist #{svc.waitlist_position(nadia_reg.id)}")

    step("Rule: 20 people booking at the same instant never overbook")
    rush = svc.create_event(org, room.id, "Cyber Hygiene Clinic", datetime(2026, 11, 12, 14),
                            datetime(2026, 11, 12, 15), capacity=5)
    svc.publish(org, rush.id)
    crowd = [svc.create_user(f"rush{i}@example.test", f"Person {i}", "attendee", PASSWORD).id for i in range(20)]
    barrier, statuses = threading.Barrier(len(crowd)), []

    def book(person):
        barrier.wait()
        statuses.append(svc.register(person, rush.id).status)

    threads = [threading.Thread(target=book, args=(p,)) for p in crowd]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out(f"   5 seats, 20 simultaneous bookings: {statuses.count('confirmed')} confirmed, "
        f"{statuses.count('waitlisted')} waitlisted, 0 overbooked")

    step("Rule: registration closes on time")
    clock.now = datetime(2026, 11, 9, 17, 0)
    late = svc.create_user("late@example.test", "Latecomer", "attendee", PASSWORD).id
    refused(lambda: svc.register(late, ws.id))

    step("Event day: check-in at the door (Tue 10 Nov)")
    clock.now = datetime(2026, 11, 10, 8, 30)
    refused(lambda: svc.check_in(door, regs[guests[0]].ticket_code))
    clock.now = datetime(2026, 11, 10, 9, 40)
    for g in guests[:4]:
        if svc.registration(regs[g].id).status == "confirmed":
            clock.now += timedelta(minutes=3)
            out(f"   {clock.now:%H:%M}  {svc.check_in(door, regs[g].ticket_code).message}")
    out("   Same ticket scanned again:")
    refused(lambda: svc.check_in(door, regs[guests[0]].ticket_code))
    out("   Marlon's old (cancelled) ticket:")
    refused(lambda: svc.check_in(door, regs[guests[1]].ticket_code))
    out("   Nadia (still on the waitlist):")
    refused(lambda: svc.check_in(door, nadia_reg.ticket_code))

    step("Summary")
    s = svc.seats(ws.id)
    out(f"   {ws.title}: capacity {s.capacity}, confirmed {s.confirmed}, "
        f"waitlisted {s.waitlisted}, checked in {s.checked_in}")
    out("")
    out("   Audit trail (last 6):")
    for a in svc.audit_trail(ws.id)[-6:]:
        out(f"   {a.at:%d %b %H:%M}  {a.action:<24} {a.detail}")
