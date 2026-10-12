"""Attendance reports.

Definitions (written down so every number on screen means one thing):

  capacity        seats offered
  confirmed       people holding a confirmed place (not cancelled)
  checked in      confirmed people who were scanned in at the door
  no-shows        confirmed but never checked in; only counted once the event has ended
  attendance      checked in / confirmed
  fill            confirmed / capacity
  still waiting   people on the waitlist when registration closed: demand that wasn't met
  promoted        people who got a seat from the waitlist
  cancellations   registrations cancelled by the attendee or the organiser
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select

from .models import AuditEntry, Event, Registration


@dataclass
class EventReport:
    event: Event
    capacity: int
    confirmed: int
    checked_in: int
    waitlisted: int
    cancelled: int
    promoted: int
    finished: bool
    arrivals: list = field(default_factory=list)   # (bucket start, count) in 15-minute steps

    @property
    def no_shows(self):
        return self.confirmed - self.checked_in if self.finished else None

    @property
    def attendance_rate(self):
        return self.checked_in / self.confirmed if self.confirmed else None

    @property
    def fill_rate(self):
        return self.confirmed / self.capacity if self.capacity else None


@dataclass
class VenueSummary:
    name: str
    events: int
    confirmed: int
    checked_in: int

    @property
    def no_show_rate(self):
        return (self.confirmed - self.checked_in) / self.confirmed if self.confirmed else None


@dataclass
class Overview:
    reports: list
    venues: list

    @property
    def finished(self):
        return [r for r in self.reports if r.finished]

    @property
    def total_confirmed(self):
        return sum(r.confirmed for r in self.finished)

    @property
    def total_checked_in(self):
        return sum(r.checked_in for r in self.finished)

    @property
    def attendance_rate(self):
        return self.total_checked_in / self.total_confirmed if self.total_confirmed else None

    @property
    def turned_away(self):
        """People still waiting when a finished event happened."""
        return sum(r.waitlisted for r in self.finished)

    @property
    def unmet_demand(self):
        """Events that turned people away, most first."""
        return sorted((r for r in self.reports if r.waitlisted), key=lambda r: -r.waitlisted)


ARRIVAL_STEP = timedelta(minutes=15)


def event_report(svc, event_id):
    with svc.Session() as s:
        event = s.get(Event, event_id)
        if event is None:
            from .service import NotFound
            raise NotFound("Event not found")
        return _report(s, event, svc.now())


def overview(svc, organiser_id):
    """Every published or cancelled event this organiser has run, newest first."""
    with svc.Session() as s:
        events = s.scalars(select(Event).where(Event.organiser_id == organiser_id, Event.status != "draft")
                           .order_by(Event.starts_at.desc())).all()
        now = svc.now()
        reports = [_report(s, e, now) for e in events]
    venues = {}
    for r in reports:
        if r.finished and r.event.status == "published":
            v = venues.setdefault(r.event.venue.name, VenueSummary(r.event.venue.name, 0, 0, 0))
            v.events += 1
            v.confirmed += r.confirmed
            v.checked_in += r.checked_in
    return Overview(reports, sorted(venues.values(), key=lambda v: v.name))


def _report(s, event, now):
    counts = dict(s.execute(select(Registration.status, func.count()).where(Registration.event_id == event.id)
                            .group_by(Registration.status)).all())
    arrivals = s.scalars(select(Registration.checked_in_at).where(
        Registration.event_id == event.id, Registration.checked_in_at.is_not(None))).all()
    promoted = s.scalar(select(func.count()).where(AuditEntry.event_id == event.id,
                                                   AuditEntry.action == "promoted_from_waitlist"))
    return EventReport(
        event=event, capacity=event.capacity, confirmed=counts.get("confirmed", 0),
        checked_in=len(arrivals), waitlisted=counts.get("waitlisted", 0),
        cancelled=counts.get("cancelled", 0), promoted=promoted,
        finished=event.status == "published" and now >= event.ends_at,
        arrivals=_arrival_buckets(event, arrivals))


def _arrival_buckets(event, times):
    """Check-ins counted in 15-minute steps, from doors opening to the last arrival (or the start)."""
    if not times:
        return []
    from .service import CHECK_IN_OPENS
    first = event.starts_at - CHECK_IN_OPENS
    last = max(max(times), event.starts_at)
    counts = Counter(int((t - first) / ARRIVAL_STEP) for t in times)
    steps = int((last - first) / ARRIVAL_STEP) + 1
    return [(first + i * ARRIVAL_STEP, counts.get(i, 0)) for i in range(steps)]
