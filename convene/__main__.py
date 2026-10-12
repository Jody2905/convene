"""Command line.

  python -m convene demo                    walk through the booking rules with a throwaway database
  python -m convene init-demo [--reset]     create convene.db with demo accounts and events
  python -m convene serve                   start the web app on http://127.0.0.1:5000
"""

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import demo

DEMO_PASSWORD = "Convene-2026"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="convene")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="walk through the booking rules step by step")
    default_db = os.environ.get("CONVENE_DATABASE_URL", "convene.db")
    db_help = "SQLite file or database URL (default: $CONVENE_DATABASE_URL or convene.db)"
    i = sub.add_parser("init-demo", help="create a database with demo accounts and events")
    i.add_argument("--db", default=default_db, help=db_help)
    i.add_argument("--reset", action="store_true", help="delete existing data first")
    s = sub.add_parser("serve", help="start the web app (on this computer only)")
    s.add_argument("--db", default=default_db, help=db_help)
    s.add_argument("--port", type=int, default=5000)
    args = parser.parse_args(argv)

    if args.command == "demo":
        demo.run()
        return 0
    if args.command == "init-demo":
        return init_demo(args.db, args.reset)
    if args.command == "serve":
        url = _as_url(args.db)
        if url.startswith("sqlite:///") and not Path(url[len("sqlite:///"):]).exists():
            print(f"No database at {args.db}. Run:  python -m convene init-demo", file=sys.stderr)
            return 1
        from .web import create_app
        print(f"Convene running at http://127.0.0.1:{args.port} using {_describe(url)}  (Ctrl+C to stop)")
        create_app(url).run(host="127.0.0.1", port=args.port, debug=False)
    return 0


def init_demo(db, reset, now=None):
    """Demo accounts, venues, three finished events (for the reports) and upcoming events."""
    import random

    from .db import init_db, session_factory
    from .service import ConveneService

    from sqlalchemy import func, inspect, select

    from .db import make_engine
    from .models import Base, User

    url = _as_url(db)
    engine = make_engine(url)
    has_data = False
    if inspect(engine).has_table("users"):
        with engine.connect() as conn:
            has_data = conn.execute(select(func.count()).select_from(User)).scalar() > 0
    if has_data:
        if not reset:
            engine.dispose()
            print(f"{_describe(url)} already has data. Use --reset to start again (this deletes it).",
                  file=sys.stderr)
            return 1
        Base.metadata.drop_all(engine)
    engine.dispose()

    now = (now or datetime.now()).replace(second=0, microsecond=0)
    clock = _DemoClock(now - timedelta(days=60))           # start in the past to build some history
    engine = init_db(url)
    svc = ConveneService(session_factory(engine), clock=clock)
    rng = random.Random(7)                                  # same demo every time

    def day(n, hour, minute=0):
        return (now + timedelta(days=n)).replace(hour=hour, minute=minute)

    org = svc.create_user("andrea@convene.test", "Andrea Cupid", "organiser", DEMO_PASSWORD).id
    door = svc.create_user("shane@convene.test", "Shane Browne", "staff", DEMO_PASSWORD).id
    named = ["Kerry-Ann Baptiste", "Marlon Jack", "Shanique Glasgow", "Dwayne Lewis", "Tricia Samuel",
             "Jamal Edwards", "Keisha Ballantyne"]
    people = {n.split()[0].split("-")[0].lower(): svc.create_user(
        f"{n.split()[0].split('-')[0].lower()}@example.test", n, "attendee", DEMO_PASSWORD).id for n in named}
    crowd = list(people.values()) + [
        svc.create_user(f"{first.lower()}.{last.lower()}@example.test", f"{first} {last}", "attendee",
                        DEMO_PASSWORD).id
        for first, last in _demo_names(rng, 48)]

    hall = svc.create_venue(org, "Harbourview Conference Hall", 120, "Bay Street").id
    room = svc.create_venue(org, "Training Room 2", 20, "Administrative Building, 2nd floor").id
    roof = svc.create_venue(org, "Rooftop Terrace", 40, "Harbour Road").id

    def publish(*args, **kwargs):
        e = svc.create_event(org, *args, **kwargs)
        svc.publish(org, e.id)
        return e.id

    # ---- History: three finished events and one that was cancelled ----
    def past_event(venue, title, days_ago, hour, capacity, signups, cancels, show_rate):
        clock.now = day(-days_ago - 20, 9)
        event = publish(venue, title, day(-days_ago, hour), day(-days_ago, hour + 2), capacity=capacity)
        regs = []
        for person in rng.sample(crowd, signups):
            clock.now += timedelta(minutes=rng.randint(20, 600))
            regs.append(svc.register(person, event))
        clock.now = day(-days_ago - 3, 12)
        for reg in rng.sample([r for r in regs if r.status == "confirmed"], cancels):
            svc.cancel_registration(reg.user_id, reg.id)
        confirmed = svc.attendees(event, "confirmed")
        arriving = rng.sample(confirmed, round(len(confirmed) * show_rate))
        start = day(-days_ago, hour)
        for i, reg in enumerate(sorted(arriving, key=lambda _: rng.random())):
            # most people arrive in the 30 minutes before the start; a few are late
            clock.now = start + timedelta(minutes=round(rng.triangular(-45, 25, -8)))
            svc.check_in(door, reg.ticket_code)
        return event

    past_event(room, "Password Manager Basics", 21, 10, 15, 19, 2, 0.8)
    past_event(hall, "Records Retention Briefing", 14, 14, 120, 48, 3, 0.65)
    past_event(room, "Mobile Device Security", 7, 10, 20, 27, 1, 0.85)
    clock.now = day(-30, 9)
    tour = publish(roof, "Server Room Tour", day(-10, 15), day(-10, 16), capacity=12)
    for person in crowd[:9]:
        svc.register(person, tour)
    clock.now = day(-12, 9)
    svc.cancel_event(org, tour, "Building access not approved in time")

    # ---- Upcoming ----
    clock.now = now
    soon = now.replace(minute=0) + timedelta(hours=1)       # doors (check-in) open straight away
    workshop = publish(room, "Phishing Awareness Workshop", soon, soon + timedelta(hours=2), capacity=5,
                       description="Spot a phishing email before it spots you. Bring a laptop or phone.")
    for who in ("kerry", "shanique", "dwayne", "tricia", "jamal", "keisha"):
        svc.register(people[who], workshop)              # 5 confirmed, Keisha waitlisted; Marlon not registered
    town_hall = publish(hall, "Budget Town Hall", day(3, 18), day(3, 20),
                        description="Have your say on next year's community budget priorities.")
    for who in ("kerry", "marlon", "dwayne"):
        svc.register(people[who], town_hall)
    publish(room, "Cyber Hygiene Clinic", day(7, 10), day(7, 12), capacity=15,
            registration_closes_at=day(6, 17), description="One-to-one help securing your accounts and devices.")
    svc.create_event(org, roof, "Year-end Staff Mixer", day(30, 17), day(30, 20),
                     description="Draft: date to be confirmed.")
    engine.dispose()

    print(f"Created the demo in {_describe(url)}\n")
    print(f"Demo logins (password for all: {DEMO_PASSWORD})")
    print("  andrea@convene.test    organiser   creates events, sees reports")
    print("  shane@convene.test     door staff  checks people in")
    print("  kerry@example.test     attendee    has a ticket for today's workshop")
    print("  marlon@example.test    attendee    workshop is full: try the waitlist")
    print(f"\nToday's \"Phishing Awareness Workshop\" starts at {soon:%H:%M}, so check-in is open now.")
    print("Three finished events are included, so the Reports page has figures to show.")
    print("\nStart the app with:  python -m convene serve")
    return 0


def _as_url(db):
    """'convene.db' -> 'sqlite:///convene.db'; URLs are passed through."""
    text = str(db)
    return text if "://" in text else f"sqlite:///{text}"


def _describe(url):
    """For messages: the file name, or the database without its password."""
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    from sqlalchemy.engine import make_url
    return make_url(url).render_as_string(hide_password=True)


class _DemoClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _demo_names(rng, n):
    firsts = ["Akeem", "Brianna", "Chevonne", "Darnell", "Ellisha", "Fabian", "Gillian", "Hadiya", "Isaiah",
              "Jodian", "Kemar", "Latoya", "Mikhail", "Nadine", "Orville", "Petra", "Quincy", "Rochelle",
              "Shamar", "Tamika", "Ulric", "Vanessa", "Winston", "Yanique", "Zara", "Adrian", "Beverly",
              "Clement", "Danielle", "Everton"]
    lasts = ["Adams", "Browne", "Charles", "Daniel", "Francis", "George", "Harry", "Isaac", "John", "King",
             "Lynch", "Matthews", "Nanton", "Ollivierre", "Primus", "Quow", "Richards", "Stapleton",
             "Thomas", "Williams", "Young", "Cottle", "Hazell", "Gonsalves"]
    pairs = [(f, l) for f in firsts for l in lasts]
    return rng.sample(pairs, n)


if __name__ == "__main__":
    sys.exit(main())
