# Convene

Event registration with real-world booking rules: capacity limits, a first-come waitlist that promotes people automatically, no double-booked venues, registration deadlines and check-in at the door.

> Work in progress. The rules, database, web app and reports are done; full documentation follows.

## Try it

```bash
python -m pip install -r requirements-dev.txt
python -m convene demo         # walks through every rule with a throwaway database
python -m convene init-demo    # creates convene.db with demo accounts and events
python -m convene serve        # web app on http://127.0.0.1:5000
python -m pytest               # run the tests
```

Demo logins (password `Convene-2026`): **andrea@convene.test** (organiser), **shane@convene.test** (door staff), **kerry@example.test** and **marlon@example.test** (attendees).

Built by **Jody-Ann Trimmingham** · [GitHub](https://github.com/Jody2905)
