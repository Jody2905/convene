"""The rules of Convene. Every change goes through here.

Each public method is one database transaction: either everything in it
happens, or (if a rule is broken) nothing does. The web app in Phase 2 only
calls these methods, so the rules can't be skipped by a crafted request.

Rules enforced
  Events     - only organisers create them; they must start in the future,
               end after they start, fit in the venue, and not clash with
               another published event at the same venue (incl. turnaround).
  Booking    - only for published events, before registration closes;
               confirmed while seats remain, otherwise waitlisted in
               first-come order; no double-booking yourself at the same time.
  Waitlist   - when a confirmed person cancels, or capacity goes up, the
               longest-waiting person is confirmed automatically.
  Check-in   - staff/organisers only, from 60 minutes before the start until
               the end; confirmed tickets only; never twice.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from . import passwords
from .models import ROLES, AuditEntry, Event, Registration, User, Venue

TURNAROUND = timedelta(minutes=30)          # time to reset a venue between events
CHECK_IN_OPENS = timedelta(minutes=60)      # doors open this long before the start
ACTIVE = ("confirmed", "waitlisted")


class RuleViolation(Exception):
    """A business rule said no. The message is safe to show to the user."""


class NotAllowed(RuleViolation):
    """The user doesn't have permission for this action."""


class NotFound(RuleViolation):
    pass


@dataclass(frozen=True)
class Seats:
    capacity: int
    confirmed: int
    waitlisted: int
    checked_in: int

    @property
    def available(self):
        return max(self.capacity - self.confirmed, 0)


@dataclass(frozen=True)
class CheckInResult:
    registration: Registration
    message: str


class ConveneService:

    def __init__(self, session_factory, clock=datetime.now):
        self.Session = session_factory
        self.clock = clock

    def now(self):
        return self.clock().replace(microsecond=0)

    # ==================================================================
    # People and venues
    # ==================================================================

    def create_user(self, email, name, role, password):
        email = (email or "").strip().lower()
        if "@" not in email or len(email) > 254:
            raise RuleViolation("Please enter a valid email address")
        if not (name or "").strip():
            raise RuleViolation("Please enter a name")
        if role not in ROLES:
            raise RuleViolation(f"Role must be one of {', '.join(ROLES)}")
        try:
            hashed = passwords.hash_password(password)
        except ValueError as e:
            raise RuleViolation(str(e)) from None
        with self.Session.begin() as s:
            if s.scalar(select(User).where(User.email == email)):
                raise RuleViolation("An account with that email already exists")
            user = User(email=email, name=name.strip(), role=role, password_hash=hashed)
            s.add(user)
            s.flush()
            return user

    def authenticate(self, email, password):
        with self.Session() as s:
            user = s.scalar(select(User).where(User.email == (email or "").strip().lower(), User.active))
        # Check a password even for unknown emails so the response time doesn't reveal which emails exist
        stored = user.password_hash if user else passwords.hash_password("timing-equaliser-x")
        return user if passwords.verify_password(password or "", stored) and user else None

    def create_venue(self, organiser_id, name, capacity, address=""):
        with self.Session.begin() as s:
            self._require(s, organiser_id, "organiser")
            if not (name or "").strip():
                raise RuleViolation("Please name the venue")
            if not isinstance(capacity, int) or capacity < 1:
                raise RuleViolation("Venue capacity must be a whole number of at least 1")
            if s.scalar(select(Venue).where(Venue.name == name.strip())):
                raise RuleViolation("A venue with that name already exists")
            venue = Venue(name=name.strip(), capacity=capacity, address=address.strip())
            s.add(venue)
            s.flush()
            return venue

    # ==================================================================
    # Events
    # ==================================================================

    def create_event(self, organiser_id, venue_id, title, starts_at, ends_at, capacity=None,
                     registration_closes_at=None, description=""):
        """Creates a draft. Nobody can register until it's published."""
        with self.Session.begin() as s:
            organiser = self._require(s, organiser_id, "organiser")
            venue = self._get(s, Venue, venue_id)
            capacity = venue.capacity if capacity is None else capacity
            closes = registration_closes_at or starts_at
            self._check_event_fields(title, starts_at, ends_at, capacity, closes, venue)
            event = Event(title=title.strip(), description=description.strip(), venue_id=venue.id,
                          organiser_id=organiser.id, starts_at=starts_at, ends_at=ends_at,
                          capacity=capacity, registration_closes_at=closes, status="draft",
                          created_at=self.now())
            s.add(event)
            self._loaded(s, event)
            self._audit(s, organiser.id, event.id, "event_created", f"{event.title} at {venue.name}")
            return event

    def publish(self, organiser_id, event_id):
        with self.Session.begin() as s:
            event = self._own_event(s, organiser_id, event_id)
            if event.status != "draft":
                raise RuleViolation(f"Only draft events can be published (this one is {event.status})")
            if event.starts_at <= self.now():
                raise RuleViolation("This event has already started, so it can't be published")
            self._check_venue_free(s, event.venue_id, event.starts_at, event.ends_at, exclude_id=event.id)
            event.status = "published"
            self._audit(s, organiser_id, event.id, "event_published")
            return event

    def change_capacity(self, organiser_id, event_id, capacity):
        """Raising capacity confirms people from the waitlist; lowering it can't bump anyone."""
        with self.Session.begin() as s:
            event = self._own_event(s, organiser_id, event_id, lock=True)
            if event.status == "cancelled":
                raise RuleViolation("This event is cancelled")
            if not isinstance(capacity, int) or capacity < 1:
                raise RuleViolation("Capacity must be a whole number of at least 1")
            if capacity > event.venue.capacity:
                raise RuleViolation(f"{event.venue.name} only holds {event.venue.capacity}")
            confirmed = self._count(s, event.id, "confirmed")
            if capacity < confirmed:
                raise RuleViolation(f"{confirmed} people are already confirmed. Capacity can't go below that; "
                                    "ask people to cancel first")
            old = event.capacity
            event.capacity = capacity
            promoted = self._promote_from_waitlist(s, event)
            self._audit(s, organiser_id, event.id, "capacity_changed",
                        f"{old} -> {capacity}; {len(promoted)} confirmed from waitlist")
            return promoted

    def cancel_event(self, organiser_id, event_id, reason):
        """Cancels the event and every registration. Returns the people to notify."""
        reason = self._reason(reason)
        with self.Session.begin() as s:
            event = self._own_event(s, organiser_id, event_id, lock=True)
            if event.status == "cancelled":
                raise RuleViolation("This event is already cancelled")
            if event.ends_at <= self.now():
                raise RuleViolation("This event has already finished")
            regs = s.scalars(select(Registration).where(Registration.event_id == event.id,
                                                        Registration.status.in_(ACTIVE))).all()
            for r in regs:
                r.status, r.cancelled_at = "cancelled", self.now()
            event.status, event.cancel_reason = "cancelled", reason
            self._audit(s, organiser_id, event.id, "event_cancelled",
                        f"{reason} ({len(regs)} registrations cancelled)")
            return [r.user for r in regs]

    def event(self, event_id):
        with self.Session() as s:
            return self._get(s, Event, event_id)

    def upcoming_events(self):
        with self.Session() as s:
            return s.scalars(select(Event).where(Event.status == "published", Event.ends_at > self.now())
                             .order_by(Event.starts_at)).all()

    def seats(self, event_id):
        with self.Session() as s:
            event = self._get(s, Event, event_id)
            checked_in = s.scalar(select(func.count()).where(Registration.event_id == event_id,
                                                             Registration.checked_in_at.is_not(None)))
            return Seats(event.capacity, self._count(s, event_id, "confirmed"),
                         self._count(s, event_id, "waitlisted"), checked_in)

    # ==================================================================
    # Registration and the waitlist
    # ==================================================================

    def register(self, user_id, event_id):
        """Confirmed if there's a seat, otherwise added to the end of the waitlist."""
        with self.Session.begin() as s:
            user = self._get(s, User, user_id)
            if not user.active:
                raise NotAllowed("This account is disabled")
            event = self._get(s, Event, event_id, lock=True)    # one booking at a time per event
            now = self.now()
            if event.status != "published":
                raise RuleViolation("This event isn't open for registration")
            if now >= event.registration_closes_at:
                raise RuleViolation("Registration for this event has closed")

            existing = s.scalar(select(Registration).where(Registration.event_id == event.id,
                                                           Registration.user_id == user.id))
            if existing and existing.status in ACTIVE:
                raise RuleViolation(f"You're already {existing.status} for this event")
            self._check_person_free(s, user.id, event)

            seat_free = self._count(s, event.id, "confirmed") < event.capacity
            # Nobody jumps the queue: a seat only goes to a newcomer if no one is waiting
            status = "confirmed" if seat_free and self._count(s, event.id, "waitlisted") == 0 else "waitlisted"
            reg = existing or Registration(event_id=event.id, user_id=user.id)
            reg.status, reg.queued_at, reg.ticket_code = status, now, _ticket_code()
            reg.confirmed_at = now if status == "confirmed" else None
            reg.cancelled_at = reg.checked_in_at = reg.checked_in_by = None
            if existing is None:
                s.add(reg)
            self._loaded(s, reg)
            self._audit(s, user.id, event.id, "registered", f"{user.name}: {status}")
            return reg

    def cancel_registration(self, user_id, registration_id):
        """By the attendee themself or the event's organiser. Frees the seat for the waitlist.
        Returns the registration that was promoted, if any."""
        with self.Session.begin() as s:
            reg = self._get(s, Registration, registration_id)
            event = self._get(s, Event, reg.event_id, lock=True)
            actor = self._get(s, User, user_id)
            if actor.id != reg.user_id and actor.id != event.organiser_id:
                raise NotAllowed("You can only cancel your own registration")
            if reg.status == "cancelled":
                raise RuleViolation("This registration is already cancelled")
            if self.now() >= event.starts_at:
                raise RuleViolation("The event has started; registrations can't be cancelled now")
            was = reg.status
            reg.status, reg.cancelled_at = "cancelled", self.now()
            s.flush()
            promoted = self._promote_from_waitlist(s, event) if was == "confirmed" else []
            self._audit(s, actor.id, event.id, "registration_cancelled",
                        f"{reg.user.name} (was {was})" + (f"; {promoted[0].user.name} confirmed from waitlist"
                                                          if promoted else ""))
            return promoted[0] if promoted else None

    def waitlist_position(self, registration_id):
        """1 = next in line. None if not on the waitlist."""
        with self.Session() as s:
            reg = self._get(s, Registration, registration_id)
            if reg.status != "waitlisted":
                return None
            ahead = s.scalar(select(func.count()).where(
                Registration.event_id == reg.event_id, Registration.status == "waitlisted",
                (Registration.queued_at < reg.queued_at)
                | ((Registration.queued_at == reg.queued_at) & (Registration.id < reg.id))))
            return ahead + 1

    def registration(self, registration_id):
        with self.Session() as s:
            return self._get(s, Registration, registration_id)

    def attendees(self, event_id, status=None):
        with self.Session() as s:
            q = select(Registration).where(Registration.event_id == event_id)
            if status:
                q = q.where(Registration.status == status)
            return s.scalars(q.order_by(Registration.queued_at, Registration.id)).all()

    # ==================================================================
    # Check-in at the door
    # ==================================================================

    def check_in(self, staff_id, ticket_code):
        with self.Session.begin() as s:
            staff = self._get(s, User, staff_id)
            if staff.role not in ("staff", "organiser"):
                raise NotAllowed("Only staff and organisers can check people in")
            reg = s.scalar(select(Registration).where(Registration.ticket_code == (ticket_code or "").strip()))
            if reg is None:
                raise NotFound("Ticket not recognised")
            event, now = reg.event, self.now()
            if event.status != "published":
                raise RuleViolation(f"This event is {event.status}")
            if reg.status == "waitlisted":
                raise RuleViolation(f"{reg.user.name} is on the waitlist, not confirmed")
            if reg.status == "cancelled":
                raise RuleViolation(f"{reg.user.name}'s registration was cancelled")
            if now < event.starts_at - CHECK_IN_OPENS:
                raise RuleViolation(f"Check-in opens at {(event.starts_at - CHECK_IN_OPENS):%H:%M} "
                                    f"on {event.starts_at:%d %b}")
            if now > event.ends_at:
                raise RuleViolation("This event has finished")
            if reg.checked_in_at:
                raise RuleViolation(f"{reg.user.name} already checked in at {reg.checked_in_at:%H:%M}")
            reg.checked_in_at, reg.checked_in_by = now, staff.id
            self._audit(s, staff.id, event.id, "checked_in", reg.user.name)
            return CheckInResult(reg, f"Welcome, {reg.user.name}")

    # ==================================================================
    # Audit
    # ==================================================================

    def audit_trail(self, event_id):
        with self.Session() as s:
            return s.scalars(select(AuditEntry).where(AuditEntry.event_id == event_id)
                             .order_by(AuditEntry.id)).all()

    # ==================================================================
    # Helpers
    # ==================================================================

    def _promote_from_waitlist(self, s, event):
        """Fill free seats from the front of the queue. Caller holds the event lock."""
        promoted = []
        free = event.capacity - self._count(s, event.id, "confirmed")
        if free <= 0:
            return promoted
        queue = s.scalars(select(Registration).where(Registration.event_id == event.id,
                                                     Registration.status == "waitlisted")
                          .order_by(Registration.queued_at, Registration.id).limit(free)).all()
        for reg in queue:
            reg.status, reg.confirmed_at = "confirmed", self.now()
            promoted.append(reg)
            self._audit(s, None, event.id, "promoted_from_waitlist", reg.user.name)
        s.flush()
        return promoted

    def _check_event_fields(self, title, starts_at, ends_at, capacity, closes, venue):
        if not (title or "").strip():
            raise RuleViolation("Please give the event a title")
        if len(title) > 150:
            raise RuleViolation("Please keep the title under 150 characters")
        if starts_at <= self.now():
            raise RuleViolation("The event must start in the future")
        if ends_at <= starts_at:
            raise RuleViolation("The event must end after it starts")
        if ends_at - starts_at > timedelta(hours=24):
            raise RuleViolation("Events can be at most 24 hours long; create one event per day")
        if not isinstance(capacity, int) or capacity < 1:
            raise RuleViolation("Capacity must be a whole number of at least 1")
        if capacity > venue.capacity:
            raise RuleViolation(f"{venue.name} only holds {venue.capacity}")
        if closes > starts_at:
            raise RuleViolation("Registration must close before the event starts")

    def _check_venue_free(self, s, venue_id, starts_at, ends_at, exclude_id=None):
        # Two slots clash if each starts before the other ends (plus turnaround time).
        # The arithmetic is done in Python so the query works the same on SQLite and PostgreSQL.
        other = s.scalars(select(Event).where(
            Event.venue_id == venue_id, Event.status == "published", Event.id != exclude_id,
            Event.starts_at < ends_at + TURNAROUND, Event.ends_at > starts_at - TURNAROUND)
            .order_by(Event.starts_at)).first()
        if other:
            raise RuleViolation(
                f"{other.venue.name} is booked for \"{other.title}\" "
                f"{other.starts_at:%d %b %H:%M}-{other.ends_at:%H:%M}, "
                f"plus {TURNAROUND.seconds // 60} minutes to reset the room")

    def _check_person_free(self, s, user_id, event):
        """Stops someone holding places at two events that overlap."""
        mine = s.scalars(select(Registration).join(Event).where(
            Registration.user_id == user_id, Registration.status.in_(ACTIVE),
            Event.status == "published", Event.id != event.id,
            Event.starts_at < event.ends_at, Event.ends_at > event.starts_at)).first()
        if mine:
            raise RuleViolation(f"You're already {mine.status} for \"{mine.event.title}\" at the same time")

    @staticmethod
    def _loaded(s, obj):
        """Save a new object and load its related rows (venue, user, ...) now, while the
        session is open, so callers can read e.g. registration.user.name afterwards."""
        s.flush()
        s.refresh(obj)
        return obj

    def _count(self, s, event_id, status):
        return s.scalar(select(func.count()).where(Registration.event_id == event_id,
                                                   Registration.status == status))

    def _get(self, s, model, obj_id, lock=False):
        q = select(model).where(model.id == obj_id)
        if lock:
            q = q.with_for_update()      # PostgreSQL row lock; SQLite is already serialised (see db.py)
        obj = s.scalar(q)
        if obj is None:
            raise NotFound(f"{model.__name__} not found")
        return obj

    def _require(self, s, user_id, role):
        user = self._get(s, User, user_id)
        if user.role != role or not user.active:
            raise NotAllowed(f"Only {role}s can do that")
        return user

    def _own_event(self, s, organiser_id, event_id, lock=False):
        self._require(s, organiser_id, "organiser")
        event = self._get(s, Event, event_id, lock=lock)
        if event.organiser_id != organiser_id:
            raise NotAllowed("Only the event's organiser can change it")
        return event

    @staticmethod
    def _reason(reason):
        text = (reason or "").strip()
        if len(text) < 5:
            raise RuleViolation("Please give a reason")
        return text[:500]

    def _audit(self, s, user_id, event_id, action, detail=""):
        s.add(AuditEntry(at=self.now(), user_id=user_id, event_id=event_id, action=action, detail=detail[:500]))


def _ticket_code():
    """Unguessable: 16 random bytes. Someone can't check in by guessing a neighbour's ticket."""
    return secrets.token_urlsafe(16)
