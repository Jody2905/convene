"""Convene web interface (Flask).

This file turns web requests into ConveneService calls and shows the result.
All the rules (who may do what, capacity, waitlist, deadlines) live in the
service, so a hand-crafted request can't get round them.

Security on every request:
  * a login is required for everything except the login and sign-up pages
  * every form that changes something carries a CSRF token
  * strict security headers (Content Security Policy, no framing, no caching
    of personal pages)
  * a ticket and its QR code are only shown to the ticket holder, the event's
    organiser and door staff
"""

import io
import os
import secrets
from datetime import date, datetime, time
from functools import wraps

import qrcode
import qrcode.image.svg
from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for

from .db import init_db, session_factory
from .service import CHECK_IN_OPENS, ConveneService, NotFound, RuleViolation

CONTENT_SECURITY_POLICY = ("default-src 'self'; style-src 'self'; img-src 'self'; object-src 'none'; "
                           "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


def create_app(db_url="sqlite:///convene.db", secret_key=None, clock=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key or os.environ.get("CONVENE_SECRET") or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=64 * 1024,          # forms only; nothing big is ever uploaded
    )
    engine = init_db(db_url)
    svc = ConveneService(session_factory(engine), **({"clock": clock} if clock else {}))
    app.extensions["convene"] = svc
    app.extensions["convene_engine"] = engine

    app.jinja_env.globals.update(csrf_token=_csrf_token, now=svc.now, CHECK_IN_OPENS=CHECK_IN_OPENS)
    app.jinja_env.filters.update(when=_when, day=_day, hm=lambda d: d.strftime("%H:%M") if d else "")

    # ---- Every request -------------------------------------------------------

    @app.before_request
    def load_user_and_check_csrf():
        g.user = None
        if "user_id" in session:
            user = svc.user(session["user_id"])
            if user and user.active:
                g.user = user
            else:
                session.clear()
        if request.method == "POST":
            sent, expected = request.form.get("csrf_token", ""), session.get("csrf_token", "")
            if not sent or not expected or not secrets.compare_digest(sent, expected):
                abort(400, "Your form expired. Go back, reload the page and try again.")

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if g.get("user"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def login_required(*roles):
        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                if g.user is None:
                    return redirect(url_for("login", next=request.path))
                if roles and g.user.role not in roles:
                    abort(403)
                return view(*args, **kwargs)
            return wrapped
        return decorator

    def attempt(action, success, back):
        """Run a service call; show its success message or the rule it broke."""
        try:
            result = action()
            flash(success(result) if callable(success) else success, "ok")
        except RuleViolation as e:
            flash(str(e), "error")
        return redirect(back)

    # ---- Accounts -------------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            user = svc.authenticate(request.form.get("email"), request.form.get("password"))
            if user is None:
                flash("Wrong email or password.", "error")
                return render_template("login.html", email=request.form.get("email", "")), 401
            session.clear()
            session["user_id"] = user.id
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
        return render_template("login.html", email="")

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        """Anyone can create an attendee account. Organisers and staff are set up by an administrator."""
        if request.method == "POST":
            form = request.form
            if form.get("password") != form.get("confirm"):
                flash("The two passwords don't match.", "error")
                return render_template("signup.html", form=form), 400
            try:
                user = svc.create_user(form.get("email"), form.get("name"), "attendee", form.get("password"))
            except RuleViolation as e:
                flash(str(e), "error")
                return render_template("signup.html", form=form), 400
            session.clear()
            session["user_id"] = user.id
            flash(f"Welcome, {user.name}. Your account is ready.", "ok")
            return redirect(url_for("index"))
        return render_template("signup.html", form={})

    @app.post("/logout")
    def logout():
        session.clear()
        flash("You've been logged out.", "ok")
        return redirect(url_for("login"))

    # ---- Browsing and registering --------------------------------------------------

    @app.get("/")
    @login_required()
    def index():
        events = svc.upcoming_events()
        return render_template("index.html", events=events, seats={e.id: svc.seats(e.id) for e in events},
                               mine={r.event_id: r for r in svc.my_registrations(g.user.id)})

    @app.get("/events/<int:event_id>")
    @login_required()
    def show_event(event_id):
        event = _found(svc.event, event_id)
        is_owner = g.user.id == event.organiser_id
        if event.status == "draft" and not is_owner:
            abort(404)                                  # drafts are private to their organiser
        reg = svc.registration_for(g.user.id, event_id)
        return render_template(
            "event.html", event=event, seats=svc.seats(event_id), reg=reg,
            position=svc.waitlist_position(reg.id) if reg else None, is_owner=is_owner,
            attendees=svc.attendees(event_id) if is_owner else [],
            audit=svc.audit_trail(event_id) if is_owner else [])

    @app.post("/events/<int:event_id>/register")
    @login_required("attendee")
    def register(event_id):
        def message(reg):
            if reg.status == "confirmed":
                return "You're confirmed. Your ticket is below."
            return f"The event is full, so you're on the waitlist at #{svc.waitlist_position(reg.id)}."
        return attempt(lambda: svc.register(g.user.id, event_id), message,
                       url_for("show_event", event_id=event_id))

    @app.post("/registrations/<int:registration_id>/cancel")
    @login_required()
    def cancel_registration(registration_id):
        reg = _found(svc.registration, registration_id)
        back = request.form.get("back") == "event" and url_for("show_event", event_id=reg.event_id)
        return attempt(lambda: svc.cancel_registration(g.user.id, registration_id),
                       "Registration cancelled.", back or url_for("my_tickets"))

    @app.get("/my/tickets")
    @login_required()
    def my_tickets():
        return render_template("my_tickets.html", regs=svc.my_registrations(g.user.id),
                               positions={r.id: svc.waitlist_position(r.id) for r in svc.my_registrations(g.user.id)})

    def visible_ticket(code):
        reg = svc.registration_by_ticket(code)
        if reg is None:
            abort(404)
        allowed = (g.user.id == reg.user_id or g.user.id == reg.event.organiser_id or g.user.role == "staff")
        if not allowed:
            abort(404)                                  # don't confirm the ticket exists
        return reg

    @app.get("/tickets/<code>")
    @login_required()
    def ticket(code):
        return render_template("ticket.html", reg=visible_ticket(code))

    @app.get("/tickets/<code>/qr.svg")
    @login_required()
    def ticket_qr(code):
        reg = visible_ticket(code)
        if reg.status != "confirmed":
            abort(404)
        image = qrcode.make(reg.ticket_code, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
        buffer = io.BytesIO()
        image.save(buffer)
        return Response(buffer.getvalue(), mimetype="image/svg+xml")

    # ---- Organisers -----------------------------------------------------------------

    @app.get("/manage")
    @login_required("organiser")
    def manage():
        events = svc.organiser_events(g.user.id)
        return render_template("manage.html", events=events, seats={e.id: svc.seats(e.id) for e in events})

    @app.route("/events/new", methods=["GET", "POST"])
    @login_required("organiser")
    def new_event():
        form = request.form if request.method == "POST" else {}
        if request.method == "POST":
            try:
                day = _parse_date(form.get("date"))
                starts = _combine(day, form.get("start"), "start time")
                ends = _combine(day, form.get("end"), "end time")
                closes = _parse_datetime(form.get("closes")) if form.get("closes") else None
                capacity = _parse_int(form.get("capacity")) if form.get("capacity") else None
                event = svc.create_event(g.user.id, _parse_int(form.get("venue_id")), form.get("title", ""),
                                         starts, ends, capacity=capacity, registration_closes_at=closes,
                                         description=form.get("description", ""))
                flash("Draft saved. Check the details, then publish it to open registration.", "ok")
                return redirect(url_for("show_event", event_id=event.id))
            except RuleViolation as e:
                flash(str(e), "error")
                return render_template("event_form.html", venues=svc.venues(), form=form), 400
        return render_template("event_form.html", venues=svc.venues(), form=form)

    @app.post("/events/<int:event_id>/publish")
    @login_required("organiser")
    def publish(event_id):
        return attempt(lambda: svc.publish(g.user.id, event_id), "Published. Registration is open.",
                       url_for("show_event", event_id=event_id))

    @app.post("/events/<int:event_id>/capacity")
    @login_required("organiser")
    def change_capacity(event_id):
        def message(promoted):
            moved = f" {len(promoted)} confirmed from the waitlist." if promoted else ""
            return "Capacity updated." + moved
        return attempt(lambda: svc.change_capacity(g.user.id, event_id, _parse_int(request.form.get("capacity"))),
                       message, url_for("show_event", event_id=event_id))

    @app.post("/events/<int:event_id>/cancel")
    @login_required("organiser")
    def cancel_event(event_id):
        return attempt(lambda: svc.cancel_event(g.user.id, event_id, request.form.get("reason")),
                       lambda people: f"Event cancelled. {len(people)} people need to be told.",
                       url_for("show_event", event_id=event_id))

    @app.route("/venues", methods=["GET", "POST"])
    @login_required("organiser")
    def venues():
        if request.method == "POST":
            return attempt(lambda: svc.create_venue(g.user.id, request.form.get("name", ""),
                                                    _parse_int(request.form.get("capacity")),
                                                    request.form.get("address", "")),
                           "Venue added.", url_for("venues"))
        return render_template("venues.html", venues=svc.venues())

    # ---- Door staff -----------------------------------------------------------------

    @app.route("/check-in", methods=["GET", "POST"])
    @login_required("staff", "organiser")
    def check_in():
        result = None
        if request.method == "POST":
            try:
                done = svc.check_in(g.user.id, request.form.get("code", ""))
                result = {"ok": True, "message": done.message, "event": done.registration.event.title}
            except RuleViolation as e:
                result = {"ok": False, "message": str(e)}
        open_events = svc.check_in_open_events()
        return render_template("check_in.html", result=result, events=open_events,
                               seats={e.id: svc.seats(e.id) for e in open_events})

    # ---- Errors ---------------------------------------------------------------------

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def error_page(e):
        messages = {403: "Your account can't open that page.", 404: "That page doesn't exist.",
                    413: "That form was too large."}
        return render_template("error.html", code=e.code, message=messages.get(e.code, e.description)), e.code

    return app


# ---- Helpers ------------------------------------------------------------------------

def _found(getter, obj_id):
    try:
        return getter(obj_id)
    except NotFound:
        abort(404)


def _csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def _parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RuleViolation("Please enter a whole number") from None


def _parse_date(value):
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        raise RuleViolation("Please choose a date") from None


def _combine(day, value, label):
    try:
        return datetime.combine(day, time.fromisoformat(value or ""))
    except ValueError:
        raise RuleViolation(f"Please enter a {label}") from None


def _parse_datetime(value):
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise RuleViolation("Registration closing time isn't a valid date and time") from None


def _when(d):
    """'Tue 10 Nov 2026, 10:00'"""
    return d.strftime("%a %d %b %Y, %H:%M") if d else ""


def _day(d):
    return d.strftime("%a %d %b") if d else ""


