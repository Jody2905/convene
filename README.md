# Convene

[![tests](https://github.com/Jody2905/convene/actions/workflows/tests.yml/badge.svg)](https://github.com/Jody2905/convene/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.13-blue)
![Databases](https://img.shields.io/badge/database-SQLite%20%7C%20PostgreSQL-4b3fd1)

**Event registration that handles the awkward parts: capacity limits, a fair waitlist, venues that can't be double-booked, QR-code tickets, check-in at the door, and attendance reports afterwards.**

Organisers create events and see who's coming. Attendees register and get a ticket, or join a waitlist that promotes them automatically when a seat frees up. Door staff scan tickets. Afterwards, the reports show who actually came, which events needed more seats, and which venues have the most no-shows.

> All names and events in this repository are made up.

| Browsing events | A QR-code ticket |
|---|---|
| ![Upcoming events with seat bars](docs/screenshots/1-events.png) | ![Ticket with QR code](docs/screenshots/2-ticket.png) |
| **The event is full: on the waitlist** | **Check-in at the door** |
| ![Waitlist position](docs/screenshots/3-waitlist.png) | ![Green check-in result](docs/screenshots/4-check-in.png) |

---

## The rules

All the rules live in one place (`convene/service.py`), so the web pages, the command line and the tests all go through the same checks. A hand-crafted web request can't skip them.

| Area | Rule |
|---|---|
| Capacity | Confirmed while seats last; after that, a first-come waitlist. Nobody jumps the queue. |
| Waitlist | When a confirmed person cancels, or the organiser adds seats, the longest-waiting person is confirmed automatically. |
| Venues | A venue can't host two published events at once, including 30 minutes to reset the room in between. |
| People | Nobody can hold places at two events that overlap. |
| Deadlines | Registration closes on time; places can't be cancelled after the start. |
| Capacity changes | Can go up to what the venue holds; can't go below the number already confirmed (no bumping people). |
| Check-in | Staff or organisers only, from 60 minutes before the start. Confirmed tickets only, and never twice. |
| Tickets | Random 128-bit codes, so a neighbour's ticket can't be guessed. Re-registering issues a new code. |
| Drafts | Visible only to their organiser until published. |

## Reports

![Reports across all events](docs/screenshots/5-reports.png)

- **Per event:** confirmed, checked in, no-shows (counted only after the event ends), people still waiting, people promoted from the waitlist, and a chart of **when people arrived**, in 15-minute steps from doors opening.
- **Across events:** attendance rate, people turned away, events that needed more seats, and no-show rates by venue.
- **Downloads:** attendee lists as CSV or Excel, including a **printable sign-in sheet**, and the overview as Excel with live formulas for the rates. Every download of personal data is recorded in the event's activity log.

![Arrival times for one event](docs/screenshots/6-event-report.png)

## Design decisions

**Two people booking the last seat at the same moment.** This is the classic race condition: both read "1 seat left", both get confirmed. Convene locks the event's row while a booking is made (`SELECT ... FOR UPDATE` on PostgreSQL; `BEGIN IMMEDIATE` on SQLite, which has no row locks). A test has 20 people book 3 seats at the same instant. It always ends 3 confirmed and 17 waitlisted. With the lock removed, PostgreSQL confirmed 14 people for 3 seats, so the test is known to catch the problem.

**The database enforces the rules too.** CHECK constraints (events end after they start, capacity is positive, only confirmed people can be checked in), a UNIQUE constraint (one registration per person per event) and foreign keys mean that even code that bypasses the service can't store impossible data. There are tests for this.

**One registration row per person per event.** Cancelling and re-registering reuses the row, sends the person to the back of the queue and issues a new ticket, so the old ticket stops working.

**SQLAlchemy ORM.** Tables are Python classes (`convene/models.py`), so the same code runs on SQLite and PostgreSQL. CI runs the full test suite on both.

**Times are the venue's local time.** For one organisation in one place, "doors open at 9:00" should mean 9:00. The service takes a clock as a parameter, so tests can set the time precisely ("it's 16:59, registration closes at 17:00").

## Security

- Passwords hashed with **scrypt** and a per-user salt. Login takes the same time whether or not the email exists, so it doesn't reveal who has an account.
- **CSRF token** on every form (including login), compared in constant time.
- **Content Security Policy** with no inline scripts or styles; `X-Frame-Options: DENY`; personal pages sent with `Cache-Control: no-store`.
- **Role checks** on every page: attendees, door staff and organisers each see only their own tools. Organisers can only manage their own events. Tickets are visible only to the holder, the event's organiser and door staff; anyone else gets "not found" rather than confirmation that the ticket exists.
- Templates **auto-escape** everything a user typed. A test signs up as `<script>alert(1)</script>`.
- **CSV/Excel formula injection** blocked in downloads: a name such as `=HYPERLINK(...)` is written as text, never as a formula.
- Safe redirect after login (no open redirects); form size limit.

## Run it

Needs Python 3.11 or newer.

```bash
git clone https://github.com/Jody2905/convene.git
cd convene
python -m pip install -r requirements-dev.txt

python -m convene init-demo     # demo accounts, venues, past events (for the reports) and upcoming ones
python -m convene serve         # http://127.0.0.1:5000
```

(On Windows, `py` instead of `python` also works.)

Demo logins (password **`Convene-2026`**):

| Email | Role | Try |
|---|---|---|
| `andrea@convene.test` | organiser | Reports, create and publish an event, raise a capacity |
| `shane@convene.test` | door staff | Check-in: paste a ticket code |
| `kerry@example.test` | attendee | Has a ticket for today's workshop |
| `marlon@example.test` | attendee | The workshop is full: join the waitlist |

Today's workshop starts at the next full hour after `init-demo`, so check-in is open straight away. `init-demo --reset` starts again.

To watch the rules without a browser:

```bash
python -m convene demo
```

### With PostgreSQL

```bash
python -m pip install -r requirements-postgres.txt
$env:CONVENE_DATABASE_URL="postgresql+psycopg://user:password@localhost/convene"   # Windows PowerShell
export CONVENE_DATABASE_URL=postgresql+psycopg://user:password@localhost/convene   # macOS/Linux
python -m convene init-demo
python -m convene serve
```

## Tests

```bash
python -m pytest
```

Around 90 tests cover the booking rules, the waitlist, check-in, simultaneous bookings, database constraints, every page for every role, security (CSRF, escaping, who can see what), the reports' arithmetic and the downloads. To run the same tests against PostgreSQL, set `CONVENE_TEST_DATABASE_URL` to an empty database.

GitHub Actions runs everything on every push: SQLite on Windows and Linux with Python 3.11 and 3.13, plus a PostgreSQL 16 job.

## Project layout

```
convene/
  models.py      tables and constraints (SQLAlchemy)
  db.py          connections, and the lock that stops overbooking on SQLite
  service.py     every rule: events, registration, waitlist, check-in, audit
  reports.py     attendance figures
  exports.py     CSV and Excel downloads, sign-in sheet
  web.py         Flask pages, logins, roles, security headers
  demo.py        the command-line walkthrough
  templates/     HTML pages
tests/           pytest suite
```

## Database

| Table | Holds |
|---|---|
| `users` | email (unique), name, role (organiser / staff / attendee), password hash |
| `venues` | name (unique), address, capacity |
| `events` | title, venue, organiser, start and end, capacity, registration deadline, status (draft / published / cancelled) |
| `registrations` | one row per person per event: status (confirmed / waitlisted / cancelled), ticket code (unique), when they joined the queue, check-in time and who scanned them |
| `audit_log` | every action: bookings, promotions, cancellations, check-ins, downloads |

## Possible next steps

- Email confirmations and "you've been promoted from the waitlist" notices
- Recurring events and multi-day conferences
- A phone camera scanner on the check-in page

---

Built by **Jody-Ann Trimmingham** · [GitHub](https://github.com/Jody2905)
