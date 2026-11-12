import io
import os
from datetime import date, datetime, time, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from google.api_core import exceptions as gcs_errors
from google.cloud import storage
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent

# --- Configuration ----------------------------------------------------------
# Settings come from environment variables; a .env file next to app.py is
# loaded first (it must not be committed, see .gitignore).

load_dotenv(BASE_DIR / ".env")


def require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set. Add it to .env or the environment.")
    return value


# Cloud Run sets K_SERVICE on every service, so its presence means production.
# Production reads DATABASE_URL_PROD (from Secret Manager, see deploy.sh);
# anywhere else (your laptop) reads DATABASE_URL_DEV from .env.
IS_PRODUCTION = bool(os.environ.get("K_SERVICE"))
DATABASE_URL = require_env("DATABASE_URL_PROD" if IS_PRODUCTION else "DATABASE_URL_DEV")  # Postgres (Neon)
SECRET_KEY = require_env("SECRET_KEY")        # signs the session cookie
SITE_NAME = os.environ.get("SITE_NAME", "").strip() or "Office Hours"
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip()
# IANA zone the office hours are in, e.g. "America/Los_Angeles"; defaults to the system zone.
TIMEZONE = os.environ.get("TIMEZONE", "").strip()
TZ = ZoneInfo(TIMEZONE) if TIMEZONE else None
# Private Cloud Storage bucket that holds the files attached to bookings.
ATTACHMENTS_BUCKET = require_env("ATTACHMENTS_BUCKET")

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # 5 MB

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
# Refuse oversized uploads before reading them; the margin leaves room for the
# rest of the form, and the exact 5 MB limit is checked in attach().
app.config["MAX_CONTENT_LENGTH"] = MAX_ATTACHMENT_BYTES + 64 * 1024


@app.context_processor
def inject_site_name():
    return {"site_name": SITE_NAME}


# --- Database helpers -------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    # `with` commits on success and closes the connection afterwards.
    with psycopg.connect(DATABASE_URL) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL
            )
            """
        )
        migrate_weekday_bookings(db)
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
                day DATE NOT NULL,
                start TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id),
                UNIQUE (day, start)
            )
            """
        )
        # The name of the file attached to the booking, if any; the file itself
        # is in the attachments bucket (see attachment_blob).
        db.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS attachment TEXT")


def migrate_weekday_bookings(db):
    """Earlier versions stored a weekday (0-4) instead of a date. Move those
    bookings onto the matching day of the current week."""
    old = db.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'bookings' AND column_name = 'weekday'
        """
    ).fetchone()
    if old is None:
        return
    db.execute("ALTER TABLE bookings ADD COLUMN day DATE")
    db.execute("UPDATE bookings SET day = %s + weekday", (week_start(now().date()),))
    db.execute("ALTER TABLE bookings ALTER COLUMN day SET NOT NULL")
    db.execute("ALTER TABLE bookings DROP COLUMN weekday")  # also drops UNIQUE (weekday, start)
    db.execute("ALTER TABLE bookings ADD UNIQUE (day, start)")


# --- Sign-up sheet ----------------------------------------------------------

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
# Half-hour slots from 2:00 pm to 4:00 pm, stored as 24-hour "HH:MM" strings.
SLOT_TIMES = ["14:00", "14:30", "15:00", "15:30"]


def now():
    """The current time in the office's zone, as a naive datetime."""
    return datetime.now(TZ).replace(tzinfo=None)


def week_start(day):
    """The Monday of the week containing `day`."""
    return day - timedelta(days=day.weekday())


def parse_day(text):
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def format_time(hhmm):
    hour, minute = (int(p) for p in hhmm.split(":"))
    suffix = "pm" if hour >= 12 else "am"
    return f"{(hour - 1) % 12 + 1}:{minute:02d} {suffix}"


def format_day(day):
    return f"{day:%A}, {day:%b} {day.day}"


app.jinja_env.filters["ampm"] = format_time
app.jinja_env.filters["dayname"] = format_day


def slot_end(hhmm):
    hour, minute = (int(p) for p in hhmm.split(":"))
    minute += 30
    if minute >= 60:
        hour, minute = hour + 1, minute - 60
    return f"{hour:02d}:{minute:02d}"


def slot_datetime(day, start):
    hour, minute = (int(p) for p in start.split(":"))
    return datetime.combine(day, time(hour, minute))


def is_past(day, start):
    return slot_datetime(day, start) < now()


def parse_slot(form):
    """Return (day, start) from a form, or None if it isn't a real slot."""
    day = parse_day(form.get("day", ""))
    start = form.get("start", "")
    if day is None or day.weekday() >= len(DAYS) or start not in SLOT_TIMES:
        return None
    return day, start


def is_admin(user):
    """The administrator is the user named by ADMIN_USERNAME in .env."""
    return user is not None and bool(ADMIN_USERNAME) and user["username"] == ADMIN_USERNAME


# --- Attachments --------------------------------------------------------------
# One file per booking, stored in the bucket as bookings/<booking id>; the
# bookings table keeps the file's name. A new upload replaces the old file.

# File types are recognised by their first bytes, not by the name the browser
# sends, so a renamed file is not accepted. (No SVG: it can contain scripts.)
FILE_SIGNATURES = [
    (b"%PDF-", "application/pdf", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
]


def detect_file_type(data):
    """Return (content type, extension) for a PDF or image, else None."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    for signature, content_type, extension in FILE_SIGNATURES:
        if data.startswith(signature):
            return content_type, extension
    return None


_bucket = None


def attachments_bucket():
    """The attachments bucket. Uses the service's own account on Cloud Run and
    your `gcloud auth application-default login` credentials on your laptop."""
    global _bucket
    if _bucket is None:
        _bucket = storage.Client().bucket(ATTACHMENTS_BUCKET)
    return _bucket


def attachment_blob(booking_id):
    return attachments_bucket().blob(f"bookings/{booking_id}")


def delete_attachment(booking_id):
    try:
        attachment_blob(booking_id).delete()
    except gcs_errors.NotFound:
        pass
    except gcs_errors.GoogleAPICallError:
        app.logger.exception("Could not delete the attachment of booking %s", booking_id)


# --- Auth helpers -----------------------------------------------------------

@app.before_request
def load_current_user():
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
    else:
        g.user = get_db().execute(
            "SELECT id, username FROM users WHERE id = %s", (user_id,)
        ).fetchone()
        if g.user is None:
            session.clear()
    g.is_admin = is_admin(g.user)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Please log in first.")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


# --- Routes -----------------------------------------------------------------

MIN_PASSWORD_LENGTH = 8

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username:
            flash("Username is required.")
        elif not password:
            flash("Password is required.")
        elif len(password) < MIN_PASSWORD_LENGTH:
            flash(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        else:
            db = get_db()
            try:
                db.execute(
                    "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                    (username, generate_password_hash(password)),
                )
                db.commit()
            except psycopg.IntegrityError:
                db.rollback()
                flash(f"Username '{username}' is already taken.")
            else:
                flash("Account created. You can log in now.")
                return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = get_db().execute(
            "SELECT * FROM users WHERE username = %s", (username,)
        ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect username or password.")
        else:
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Welcome, {user['username']}!")
            return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for("index"))


@app.route("/sheet")
@login_required
def sheet():
    today = now().date()
    this_week = week_start(today)
    # ?week=YYYY-MM-DD picks a week; any date in it will do. Default: this week.
    monday = week_start(parse_day(request.args.get("week")) or today)
    days = [monday + timedelta(days=i) for i in range(len(DAYS))]

    rows = get_db().execute(
        """
        SELECT b.id, b.day, b.start, b.user_id, b.attachment, u.username
        FROM bookings b JOIN users u ON u.id = b.user_id
        WHERE b.day BETWEEN %s AND %s
        """,
        (days[0], days[-1]),
    ).fetchall()
    bookings = {(r["day"], r["start"]): r for r in rows}

    # One cell per (time, day); each cell says whether it is free, yours, or taken.
    grid = []
    for start in SLOT_TIMES:
        cells = []
        for day in days:
            booking = bookings.get((day, start))
            if booking is None:
                status = "free"
            elif booking["user_id"] == g.user["id"]:
                status = "mine"
            else:
                status = "taken"
            cells.append(
                {
                    "day": day,
                    "start": start,
                    "status": status,
                    "past": is_past(day, start),
                    "username": booking["username"] if booking else None,
                    "booking_id": booking["id"] if booking else None,
                    "attachment": booking["attachment"] if booking else None,
                }
            )
        grid.append({"start": start, "end": slot_end(start), "cells": cells})

    return render_template(
        "sheet.html",
        days=days,
        grid=grid,
        today=today,
        this_week=this_week,
        prev_week=monday - timedelta(days=7),
        next_week=monday + timedelta(days=7),
    )


@app.route("/book", methods=["POST"])
@login_required
def book():
    slot = parse_slot(request.form)
    if slot is None:
        flash("That is not a valid slot.")
        return redirect(url_for("sheet"))
    day, start = slot
    back = redirect(url_for("sheet", week=week_start(day).isoformat()))
    if is_past(day, start):
        flash("That slot is in the past.")
        return back

    db = get_db()
    try:
        db.execute(
            "INSERT INTO bookings (day, start, user_id) VALUES (%s, %s, %s)",
            (day, start, g.user["id"]),
        )
        db.commit()
    except psycopg.IntegrityError:
        db.rollback()
        flash("Sorry, that slot has already been taken.")
    else:
        flash(f"Booked {format_day(day)} at {format_time(start)}.")
    return back


@app.route("/cancel", methods=["POST"])
@login_required
def cancel():
    slot = parse_slot(request.form)
    if slot is None:
        flash("That is not a valid slot.")
        return redirect(url_for("sheet"))
    day, start = slot
    back = redirect(url_for("sheet", week=week_start(day).isoformat()))
    if is_past(day, start):
        flash("That slot is in the past.")
        return back

    db = get_db()
    # Only the person who booked a slot may cancel it.
    deleted = db.execute(
        "DELETE FROM bookings WHERE day = %s AND start = %s AND user_id = %s"
        " RETURNING id, attachment",
        (day, start, g.user["id"]),
    ).fetchone()
    db.commit()
    if deleted:
        if deleted["attachment"]:
            delete_attachment(deleted["id"])
        flash(f"Cancelled {format_day(day)} at {format_time(start)}.")
    else:
        flash("You have no booking in that slot.")
    return back


@app.route("/bookings/<int:booking_id>/attachment", methods=["POST"])
@login_required
def attach(booking_id):
    db = get_db()
    booking = db.execute(
        "SELECT id, day, start, attachment FROM bookings WHERE id = %s AND user_id = %s",
        (booking_id, g.user["id"]),
    ).fetchone()
    if booking is None:
        flash("You have no such booking.")
        return redirect(url_for("sheet"))
    day, start = booking["day"], booking["start"]
    back = redirect(url_for("sheet", week=week_start(day).isoformat()))
    if is_past(day, start):
        flash("That slot is in the past.")
        return back

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash("Choose a file to attach.")
        return back
    data = upload.stream.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        flash("The file is larger than 5 MB.")
        return back
    file_type = detect_file_type(data)
    if file_type is None:
        flash("Only PDF files and images (PNG, JPEG, GIF, WebP) can be attached.")
        return back
    content_type, extension = file_type
    name = secure_filename(upload.filename)[-200:] or f"attachment{extension}"

    try:
        attachment_blob(booking_id).upload_from_string(data, content_type=content_type)
    except gcs_errors.GoogleAPICallError:
        app.logger.exception("Could not upload the attachment of booking %s", booking_id)
        flash("Sorry, the file could not be stored. Please try again.")
        return back
    db.execute("UPDATE bookings SET attachment = %s WHERE id = %s", (name, booking_id))
    db.commit()
    flash(f"Attached {name} to {format_day(day)} at {format_time(start)}.")
    return back


@app.route("/bookings/<int:booking_id>/attachment")
@login_required
def open_attachment(booking_id):
    booking = get_db().execute(
        "SELECT user_id, attachment FROM bookings WHERE id = %s", (booking_id,)
    ).fetchone()
    # Only the booking's owner and the administrator may open the file. Others
    # get the same 404 as for a missing file, so they learn nothing about it.
    if booking is None or not booking["attachment"]:
        abort(404)
    if booking["user_id"] != g.user["id"] and not g.is_admin:
        abort(404)
    try:
        data = attachment_blob(booking_id).download_as_bytes()
    except gcs_errors.NotFound:
        abort(404)
    content_type = (detect_file_type(data) or ("application/octet-stream", ""))[0]
    response = send_file(
        io.BytesIO(data),
        mimetype=content_type,
        download_name=booking["attachment"],
        as_attachment=content_type == "application/octet-stream",
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.errorhandler(413)
def too_large(_error):
    flash("The file is larger than 5 MB.")
    return redirect(url_for("sheet"))


init_db()

if __name__ == "__main__":
    app.run(debug=True)
