"""The web pages: every role, the security rules, and the full journey from sign-up to check-in."""

import re

import pytest

from conftest import PASSWORD, Clock, at
from convene.__main__ import DEMO_PASSWORD, init_demo
from convene.web import create_app


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(tmp_path, clock):
    app = create_app(f"sqlite:///{tmp_path / 'web.db'}", secret_key="test-secret", clock=clock)
    app.config["TESTING"] = True
    svc = app.extensions["convene"]
    org = svc.create_user("andrea@convene.test", "Andrea Cupid", "organiser", PASSWORD).id
    svc.create_user("devon@convene.test", "Devon Ollivierre", "organiser", PASSWORD)
    svc.create_user("shane@convene.test", "Shane Browne", "staff", PASSWORD)
    for name in ("kerry", "marlon", "tricia"):
        svc.create_user(f"{name}@example.test", name.capitalize(), "attendee", PASSWORD)
    room = svc.create_venue(org, "Training Room 2", 20).id
    e = svc.create_event(org, room, "Phishing Awareness Workshop", at(10, 10), at(10, 12), capacity=1,
                         description="Bring a laptop.")
    svc.publish(org, e.id)
    app.config["EVENT"] = e.id
    app.config["ROOM"] = room
    yield app
    app.extensions["convene_engine"].dispose()


@pytest.fixture
def svc(app):
    return app.extensions["convene"]


def token(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)


def client_for(app, email, password=PASSWORD):
    c = app.test_client()
    r = c.post("/login", data={"email": email, "password": password, "csrf_token": token(c)})
    assert r.status_code == 302, f"login failed for {email}"
    return c


def post(client, path, **data):
    data["csrf_token"] = token(client, "/")
    return client.post(path, data=data, follow_redirects=True)


def text(response):
    return response.get_data(as_text=True)


# ---- Logging in and signing up --------------------------------------------------------

def test_everything_needs_a_login(app):
    c = app.test_client()
    for path in ("/", "/my/tickets", "/manage", "/check-in", f"/events/{app.config['EVENT']}"):
        r = c.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path


def test_wrong_password(app):
    c = app.test_client()
    r = c.post("/login", data={"email": "kerry@example.test", "password": "nope-nope-nope", "csrf_token": token(c)})
    assert r.status_code == 401 and "Wrong email or password" in text(r)


def test_sign_up_creates_an_attendee_and_logs_in(app, svc):
    c = app.test_client()
    r = c.post("/signup", data={"name": "Nadia Pope", "email": "nadia@example.test", "password": PASSWORD,
                                "confirm": PASSWORD, "csrf_token": token(c, "/signup")}, follow_redirects=True)
    assert "Welcome, Nadia Pope" in text(r)
    assert svc.authenticate("nadia@example.test", PASSWORD).role == "attendee"   # can't sign up as organiser


def test_sign_up_problems_keep_what_was_typed(app):
    c = app.test_client()
    r = c.post("/signup", data={"name": "Nadia", "email": "nadia@example.test", "password": PASSWORD,
                                "confirm": "different-password", "csrf_token": token(c, "/signup")})
    assert r.status_code == 400 and "don&#39;t match" in text(r) and 'value="nadia@example.test"' in text(r)
    r = c.post("/signup", data={"name": "Kerry", "email": "KERRY@example.test", "password": PASSWORD,
                                "confirm": PASSWORD, "csrf_token": token(c, "/signup")})
    assert r.status_code == 400 and "already exists" in text(r)


def test_forms_need_a_csrf_token(app):
    c = client_for(app, "kerry@example.test")
    assert c.post(f"/events/{app.config['EVENT']}/register").status_code == 400
    assert c.post(f"/events/{app.config['EVENT']}/register", data={"csrf_token": "forged"}).status_code == 400


def test_open_redirect_blocked(app):
    c = app.test_client()
    r = c.post("/login?next=//evil.example", data={"email": "kerry@example.test", "password": PASSWORD,
                                                    "csrf_token": token(c)})
    assert r.headers["Location"] == "/"


def test_security_headers(app):
    r = client_for(app, "kerry@example.test").get("/")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["Cache-Control"] == "no-store"


# ---- Who can open what -----------------------------------------------------------------

@pytest.mark.parametrize("email, path, expected", [
    ("kerry@example.test", "/manage", 403),
    ("kerry@example.test", "/venues", 403),
    ("kerry@example.test", "/events/new", 403),
    ("kerry@example.test", "/check-in", 403),
    ("shane@convene.test", "/check-in", 200),
    ("shane@convene.test", "/manage", 403),
    ("andrea@convene.test", "/check-in", 200),
    ("andrea@convene.test", "/manage", 200),
])
def test_pages_by_role(app, email, path, expected):
    assert client_for(app, email).get(path).status_code == expected


def test_only_attendees_register(app):
    r = post(client_for(app, "shane@convene.test"), f"/events/{app.config['EVENT']}/register")
    assert r.status_code == 403


def test_drafts_are_hidden_from_everyone_but_their_organiser(app, svc):
    org = svc.authenticate("andrea@convene.test", PASSWORD).id
    draft = svc.create_event(org, app.config["ROOM"], "Secret planning", at(20, 9), at(20, 10)).id
    assert client_for(app, "kerry@example.test").get(f"/events/{draft}").status_code == 404
    assert client_for(app, "devon@convene.test").get(f"/events/{draft}").status_code == 404
    assert client_for(app, "andrea@convene.test").get(f"/events/{draft}").status_code == 200
    assert "Secret planning" not in text(client_for(app, "kerry@example.test").get("/"))


def test_missing_event_is_404(app):
    assert client_for(app, "kerry@example.test").get("/events/999").status_code == 404


# ---- Registering, the waitlist and tickets -----------------------------------------------------

def test_register_waitlist_and_promotion(app, svc):
    event = app.config["EVENT"]
    kerry, marlon = client_for(app, "kerry@example.test"), client_for(app, "marlon@example.test")
    assert "You&#39;re confirmed" in text(post(kerry, f"/events/{event}/register"))
    html = text(post(marlon, f"/events/{event}/register"))
    assert "on the waitlist at #1" in html

    reg = svc.registration_for(svc.authenticate("kerry@example.test", PASSWORD).id, event)
    post(kerry, f"/registrations/{reg.id}/cancel")
    html = text(marlon.get(f"/events/{event}"))
    assert "You're confirmed" in html and "View ticket" in html


def test_ticket_and_qr_only_for_the_holder_organiser_and_staff(app, svc):
    event = app.config["EVENT"]
    kerry = client_for(app, "kerry@example.test")
    post(kerry, f"/events/{event}/register")
    code = svc.registration_for(svc.authenticate("kerry@example.test", PASSWORD).id, event).ticket_code

    page = kerry.get(f"/tickets/{code}")
    assert page.status_code == 200 and code in text(page) and "Kerry" in text(page)
    qr = kerry.get(f"/tickets/{code}/qr.svg")
    assert qr.status_code == 200 and qr.mimetype == "image/svg+xml" and b"<svg" in qr.data

    for email, expected in (("marlon@example.test", 404), ("devon@convene.test", 404),
                            ("andrea@convene.test", 200), ("shane@convene.test", 200)):
        assert client_for(app, email).get(f"/tickets/{code}").status_code == expected, email


def test_cancelled_ticket_is_shown_as_void(app, svc):
    event = app.config["EVENT"]
    kerry = client_for(app, "kerry@example.test")
    post(kerry, f"/events/{event}/register")
    reg = svc.registration_for(svc.authenticate("kerry@example.test", PASSWORD).id, event)
    post(kerry, f"/registrations/{reg.id}/cancel")
    assert "Not valid for entry" in text(kerry.get(f"/tickets/{reg.ticket_code}"))
    assert kerry.get(f"/tickets/{reg.ticket_code}/qr.svg").status_code == 404


def test_cannot_cancel_someone_elses_place(app, svc):
    event = app.config["EVENT"]
    post(client_for(app, "kerry@example.test"), f"/events/{event}/register")
    reg = svc.registration_for(svc.authenticate("kerry@example.test", PASSWORD).id, event)
    html = text(post(client_for(app, "marlon@example.test"), f"/registrations/{reg.id}/cancel"))
    assert "only cancel your own" in html
    assert svc.registration(reg.id).status == "confirmed"


def test_my_tickets_lists_places_with_waitlist_position(app):
    event = app.config["EVENT"]
    post(client_for(app, "kerry@example.test"), f"/events/{event}/register")
    marlon = client_for(app, "marlon@example.test")
    post(marlon, f"/events/{event}/register")
    html = text(marlon.get("/my/tickets"))
    assert "Phishing Awareness Workshop" in html and "#1 in line" in html


# ---- Organisers --------------------------------------------------------------------------

def test_create_publish_and_see_an_event(app):
    andrea = client_for(app, "andrea@convene.test")
    r = andrea.post("/events/new", data={"csrf_token": token(andrea, "/events/new"), "title": "Budget Town Hall",
                                         "description": "Have your say.", "venue_id": str(app.config["ROOM"]),
                                         "date": "2026-11-12", "start": "18:00", "end": "20:00", "capacity": ""},
                    follow_redirects=True)
    html = text(r)
    assert "Draft saved" in html and "Budget Town Hall" in html and "Publish and open registration" in html
    event_id = re.search(r"/events/(\d+)/publish", html).group(1)
    assert "Published" in text(post(andrea, f"/events/{event_id}/publish"))
    assert "Budget Town Hall" in text(client_for(app, "kerry@example.test").get("/"))


def test_bad_event_form_shows_the_rule_and_keeps_input(app):
    andrea = client_for(app, "andrea@convene.test")
    r = andrea.post("/events/new", data={"csrf_token": token(andrea, "/events/new"), "title": "Backwards",
                                         "venue_id": str(app.config["ROOM"]), "date": "2026-11-12",
                                         "start": "20:00", "end": "18:00"})
    assert r.status_code == 400 and "end after it starts" in text(r) and 'value="Backwards"' in text(r)


def test_venue_clash_is_reported_on_publish(app):
    andrea = client_for(app, "andrea@convene.test")
    r = andrea.post("/events/new", data={"csrf_token": token(andrea, "/events/new"), "title": "Clash",
                                         "venue_id": str(app.config["ROOM"]), "date": "2026-11-10",
                                         "start": "11:00", "end": "13:00"}, follow_redirects=True)
    event_id = re.search(r"/events/(\d+)/publish", text(r)).group(1)
    assert "is booked for" in text(post(andrea, f"/events/{event_id}/publish"))


def test_organiser_sees_people_and_raising_capacity_promotes(app):
    event = app.config["EVENT"]
    for email in ("kerry@example.test", "marlon@example.test", "tricia@example.test"):
        post(client_for(app, email), f"/events/{event}/register")
    andrea = client_for(app, "andrea@convene.test")
    html = text(andrea.get(f"/events/{event}"))
    assert "kerry@example.test" in html and "People (3)" in html
    assert "2 confirmed from the waitlist" in text(post(andrea, f"/events/{event}/capacity", capacity="3"))


def test_another_organiser_cannot_manage_my_event(app):
    devon = client_for(app, "devon@convene.test")
    html = text(post(devon, f"/events/{app.config['EVENT']}/capacity", capacity="5"))
    assert "Only the event&#39;s organiser" in html


def test_cancel_event_reports_how_many_to_tell(app):
    post(client_for(app, "kerry@example.test"), f"/events/{app.config['EVENT']}/register")
    html = text(post(client_for(app, "andrea@convene.test"), f"/events/{app.config['EVENT']}/cancel",
                     reason="Facilitator unwell"))
    assert "1 people need to be told" in html and "Facilitator unwell" in html


def test_add_venue(app):
    andrea = client_for(app, "andrea@convene.test")
    assert "Venue added." in text(post(andrea, "/venues", name="Rooftop Terrace", capacity="40", address="Harbour Rd"))
    assert "Please enter a whole number" in text(post(andrea, "/venues", name="Bad", capacity="lots"))


# ---- At the door -------------------------------------------------------------------------

def test_check_in_at_the_door(app, svc, clock):
    event = app.config["EVENT"]
    post(client_for(app, "kerry@example.test"), f"/events/{event}/register")
    code = svc.registration_for(svc.authenticate("kerry@example.test", PASSWORD).id, event).ticket_code
    shane = client_for(app, "shane@convene.test")

    assert "No events are open for check-in" in text(shane.get("/check-in"))
    clock.set(at(10, 9, 30))
    html = text(shane.get("/check-in"))
    assert "Phishing Awareness Workshop" in html

    html = text(post(shane, "/check-in", code=code))
    assert "result-ok" in html and "Welcome, Kerry" in html
    html = text(post(shane, "/check-in", code=code))
    assert "result-bad" in html and "already checked in at 09:30" in html
    assert "result-bad" in text(post(shane, "/check-in", code="not-a-real-ticket"))


# ---- Escaping --------------------------------------------------------------------------

def test_names_and_text_are_escaped(app, svc):
    c = app.test_client()
    c.post("/signup", data={"name": "<script>alert(1)</script>", "email": "x@example.test", "password": PASSWORD,
                            "confirm": PASSWORD, "csrf_token": token(c, "/signup")})
    post(c, f"/events/{app.config['EVENT']}/register")
    html = text(client_for(app, "andrea@convene.test").get(f"/events/{app.config['EVENT']}"))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ---- The demo database ---------------------------------------------------------------------

def test_init_demo_builds_a_usable_database(tmp_path):
    from datetime import datetime
    db = tmp_path / "demo.db"
    assert init_demo(db, reset=False, now=datetime(2026, 11, 2, 14, 25)) == 0
    assert init_demo(db, reset=False) == 1                      # refuses to overwrite
    app = create_app(f"sqlite:///{db}", secret_key="x", clock=lambda: datetime(2026, 11, 2, 14, 30))
    try:
        shane = client_for(app, "shane@convene.test", DEMO_PASSWORD)
        assert "Phishing Awareness Workshop" in text(shane.get("/check-in"))   # doors already open
        html = text(client_for(app, "marlon@example.test", DEMO_PASSWORD).get("/"))
        assert "Budget Town Hall" in html and "Year-end" not in html          # the draft stays hidden
    finally:
        app.extensions["convene_engine"].dispose()
