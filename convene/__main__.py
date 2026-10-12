"""Command line.

  python -m convene demo                    walk through the booking rules with a throwaway database
  python -m convene init-demo [--reset]     create convene.db with demo accounts and events
  python -m convene serve                   start the web app on http://127.0.0.1:5000
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import demo

DEMO_PASSWORD = "Convene-2026"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="convene")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="walk through the booking rules step by step")
    i = sub.add_parser("init-demo", help="create a database with demo accounts and events")
    i.add_argument("--db", default="convene.db")
    i.add_argument("--reset", action="store_true", help="delete an existing database first")
    s = sub.add_parser("serve", help="start the web app (on this computer only)")
    s.add_argument("--db", default="convene.db")
    s.add_argument("--port", type=int, default=5000)
    args = parser.parse_args(argv)

    if args.command == "demo":
        demo.run()
        return 0
    if args.command == "init-demo":
        return init_demo(Path(args.db), args.reset)
    if args.command == "serve":
        if not Path(args.db).exists():
            print(f"No database at {args.db}. Run:  python -m convene init-demo", file=sys.stderr)
            return 1
        from .web import create_app
        print(f"Convene running at http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
        create_app(f"sqlite:///{args.db}").run(host="127.0.0.1", port=args.port, debug=False)
    return 0


def init_demo(db_path, reset, now=None):
    from .db import init_db, session_factory
    from .service import ConveneService

    if db_path.exists():
        if not reset:
            print(f"{db_path} already exists. Use --reset to start again (this deletes it).", file=sys.stderr)
            return 1
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)

    now = (now or datetime.now()).replace(second=0, microsecond=0)
    engine = init_db(f"sqlite:///{db_path}")
    svc = ConveneService(session_factory(engine), clock=lambda: now)

    org = svc.create_user("andrea@convene.test", "Andrea Cupid", "organiser", DEMO_PASSWORD).id
    svc.create_user("shane@convene.test", "Shane Browne", "staff", DEMO_PASSWORD)
    names = ["Kerry-Ann Baptiste", "Marlon Jack", "Shanique Glasgow", "Dwayne Lewis", "Tricia Samuel",
             "Jamal Edwards", "Keisha Ballantyne"]
    people = {n.split()[0].split("-")[0].lower(): svc.create_user(
        f"{n.split()[0].split('-')[0].lower()}@example.test", n, "attendee", DEMO_PASSWORD).id for n in names}

    hall = svc.create_venue(org, "Harbourview Conference Hall", 120, "Bay Street").id
    room = svc.create_venue(org, "Training Room 2", 20, "Administrative Building, 2nd floor").id
    roof = svc.create_venue(org, "Rooftop Terrace", 40, "Harbour Road").id

    def day(n, hour):
        return (now + timedelta(days=n)).replace(hour=hour, minute=0)

    def publish(*args, **kwargs):
        e = svc.create_event(org, *args, **kwargs)
        svc.publish(org, e.id)
        return e.id

    # Starts at the next full hour, so its doors (check-in) are open straight away
    soon = now.replace(minute=0) + timedelta(hours=1)
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

    print(f"Created {db_path}\n")
    print(f"Demo logins (password for all: {DEMO_PASSWORD})")
    print("  andrea@convene.test    organiser   creates and manages events")
    print("  shane@convene.test     door staff  checks people in")
    print("  kerry@example.test     attendee    has a ticket for today's workshop")
    print("  marlon@example.test    attendee    workshop is full: try the waitlist")
    print(f"\nToday's \"Phishing Awareness Workshop\" starts at {soon:%H:%M}, so check-in is open now.")
    print("\nStart the app with:  python -m convene serve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
