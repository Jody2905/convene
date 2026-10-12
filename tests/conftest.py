import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from convene.db import init_db, session_factory  # noqa: E402
from convene.service import ConveneService  # noqa: E402

PASSWORD = "correct-horse-battery"
START = datetime(2026, 11, 2, 9, 0)        # "now" at the start of every test: Monday 2 Nov, 09:00


class Clock:
    """A clock the tests can move forward."""

    def __init__(self, now=START):
        self.current = now

    def __call__(self):
        return self.current

    def advance(self, **kwargs):
        self.current += timedelta(**kwargs)

    def set(self, when):
        self.current = when


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def engine(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'test.db'}")
    yield engine
    engine.dispose()


@pytest.fixture
def svc(engine, clock):
    return ConveneService(session_factory(engine), clock=clock)


@pytest.fixture
def people(svc):
    return {
        "organiser": svc.create_user("ops@convene.test", "Andrea Cupid", "organiser", PASSWORD).id,
        "other_organiser": svc.create_user("comms@convene.test", "Devon Ollivierre", "organiser", PASSWORD).id,
        "staff": svc.create_user("door@convene.test", "Shane Browne", "staff", PASSWORD).id,
    }


@pytest.fixture
def attendees(svc):
    """make(n) -> list of n new attendee ids."""
    count = {"n": 0}

    def make(n):
        ids = []
        for _ in range(n):
            count["n"] += 1
            i = count["n"]
            ids.append(svc.create_user(f"guest{i}@example.test", f"Guest {i}", "attendee", PASSWORD).id)
        return ids
    return make


@pytest.fixture
def hall(svc, people):
    return svc.create_venue(people["organiser"], "Harbourview Conference Hall", 120, "Bay Street").id


@pytest.fixture
def room(svc, people):
    return svc.create_venue(people["organiser"], "Training Room 2", 20).id


def at(day, hour, minute=0):
    """A time in November 2026."""
    return datetime(2026, 11, day, hour, minute)


@pytest.fixture
def workshop(svc, people, room):
    """A published 3-seat workshop on 10 Nov, 10:00-12:00, registration closing the day before."""
    e = svc.create_event(people["organiser"], room, "Phishing Awareness Workshop", at(10, 10), at(10, 12),
                         capacity=3, registration_closes_at=at(9, 17))
    svc.publish(people["organiser"], e.id)
    return e.id
