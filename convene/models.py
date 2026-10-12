"""Database tables, defined as Python classes with SQLAlchemy.

Each class is one table; each attribute is one column. The important rules
are ALSO written into the database as constraints (CHECK, UNIQUE, foreign
keys), so even code that bypasses the service layer can't store an event
that ends before it starts or register the same person twice.

Times are the venue's local wall-clock time. Convene is for one organisation
in one time zone, which keeps "doors open at 9:00" meaning 9:00.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

ROLES = ("organiser", "staff", "attendee")
EVENT_STATUSES = ("draft", "published", "cancelled")
REGISTRATION_STATUSES = ("confirmed", "waitlisted", "cancelled")


def _in(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in("role", ROLES), name="valid_role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(default=True)

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"


class Venue(Base):
    __tablename__ = "venues"
    __table_args__ = (CheckConstraint("capacity > 0", name="venue_capacity_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    address: Mapped[str] = mapped_column(String(200), default="")
    capacity: Mapped[int]


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ends_after_start"),
        CheckConstraint("capacity > 0", name="event_capacity_positive"),
        CheckConstraint("registration_closes_at <= starts_at", name="registration_closes_before_start"),
        CheckConstraint(_in("status", EVENT_STATUSES), name="valid_event_status"),
        Index("ix_events_venue_time", "venue_id", "starts_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text, default="")
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"))
    organiser_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    capacity: Mapped[int]
    registration_closes_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(20), default="draft")
    cancel_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime]

    venue: Mapped[Venue] = relationship(lazy="joined")
    organiser: Mapped[User] = relationship(lazy="joined")


class Registration(Base):
    """One person's place at one event: confirmed, on the waitlist, or cancelled.

    A person has at most one row per event (UNIQUE). If they cancel and sign up
    again, the same row is reused and they join the back of the queue.
    """
    __tablename__ = "registrations"
    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="one_registration_per_person"),
        CheckConstraint(_in("status", REGISTRATION_STATUSES), name="valid_registration_status"),
        CheckConstraint("checked_in_at IS NULL OR status = 'confirmed'", name="only_confirmed_check_in"),
        Index("ix_registrations_queue", "event_id", "status", "queued_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20))
    ticket_code: Mapped[str] = mapped_column(String(40), unique=True)
    queued_at: Mapped[datetime]                 # when they (last) joined; decides waitlist order
    confirmed_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]
    checked_in_at: Mapped[datetime | None]
    checked_in_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    event: Mapped[Event] = relationship(lazy="joined")
    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")


class AuditEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime]
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"))
    action: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(String(500), default="")
