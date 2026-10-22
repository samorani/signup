import hashlib
import hmac
import os
import sqlite3
from datetime import date, datetime, time, timedelta

from flask import Flask, flash, g, redirect, render_template, request, session, url_for

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signup.db")
PBKDF2_ITERATIONS = 600_000

# Office hours: Monday-Friday, 2:00-4:00 pm, in half-hour slots.
SLOT_TIMES = [time(14, 0), time(14, 30), time(15, 0), time(15, 30)]
SLOT_LENGTH = timedelta(minutes=30)

app = Flask(__name__)

# Set SECRET_KEY in the environment for anything beyond local development.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.urandom(32)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DATABASE) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                slot_start TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users (id)
            )
            """
        )


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)


def current_user():
    """The logged-in user, with is_admin set for the first user who registered."""
    username = session.get("username")
    if username is None:
        return None
    user = get_db().execute(
        "SELECT id, username, id = (SELECT MIN(id) FROM users) AS is_admin FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if user is None:
        session.clear()
    return user


def week_start(day):
    return day - timedelta(days=day.weekday())


def parse_slot(value):
    """Return the slot's start datetime, or None if value is not a valid slot."""
    try:
        start = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if start.weekday() >= 5 or start.time() not in SLOT_TIMES:
        return None
    return start


def week_url(start):
    return url_for("index", week=week_start(start.date()).isoformat())


@app.route("/")
def index():
    user = current_user()
    if user is None:
        return render_template("index.html", user=None)

    try:
        monday = week_start(date.fromisoformat(request.args["week"]))
    except (KeyError, ValueError):
        monday = week_start(date.today())
    friday = monday + timedelta(days=4)

    rows = get_db().execute(
        """
        SELECT b.slot_start, b.user_id, u.username
        FROM bookings b JOIN users u ON u.id = b.user_id
        WHERE b.slot_start BETWEEN ? AND ?
        """,
        (monday.isoformat(), (friday + timedelta(days=1)).isoformat()),
    ).fetchall()
    bookings = {row["slot_start"]: row for row in rows}

    now = datetime.now()
    days = []
    for offset in range(5):
        day = monday + timedelta(days=offset)
        slots = []
        for slot_time in SLOT_TIMES:
            start = datetime.combine(day, slot_time)
            key = start.isoformat()
            slots.append({
                "key": key,
                "start": start,
                "end": start + SLOT_LENGTH,
                "booking": bookings.get(key),
                "past": start <= now,
            })
        days.append({"date": day, "slots": slots})

    return render_template(
        "index.html",
        user=user,
        days=days,
        monday=monday,
        friday=friday,
        prev_week=(monday - timedelta(weeks=1)).isoformat(),
        next_week=(monday + timedelta(weeks=1)).isoformat(),
        this_week=week_start(date.today()),
    )


@app.route("/book", methods=["POST"])
def book():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    start = parse_slot(request.form.get("slot"))
    if start is None:
        flash("That is not a valid slot.", "error")
        return redirect(url_for("index"))
    if start <= datetime.now():
        flash("That slot has already started.", "error")
        return redirect(week_url(start))

    try:
        db = get_db()
        db.execute(
            "INSERT INTO bookings (slot_start, user_id) VALUES (?, ?)",
            (start.isoformat(), user["id"]),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash("Sorry, that slot was just taken.", "error")
        return redirect(week_url(start))

    flash(f"Booked {start:%A, %B} {start.day} at {start.strftime('%I:%M %p').lstrip('0')}.")
    return redirect(week_url(start))


@app.route("/cancel", methods=["POST"])
def cancel():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    start = parse_slot(request.form.get("slot"))
    if start is None:
        flash("That is not a valid slot.", "error")
        return redirect(url_for("index"))
    if start <= datetime.now():
        flash("That slot has already started.", "error")
        return redirect(week_url(start))

    db = get_db()
    deleted = db.execute(
        "DELETE FROM bookings WHERE slot_start = ? AND user_id = ?",
        (start.isoformat(), user["id"]),
    ).rowcount
    db.commit()
    if deleted:
        flash("Your booking was cancelled.")
    else:
        flash("You don't have a booking in that slot.", "error")
    return redirect(week_url(start))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            flash("Username and password are required.", "error")
            return render_template("register.html", username=username)

        salt = os.urandom(16)
        try:
            db = get_db()
            db.execute(
                "INSERT INTO users (username, password_salt, password_hash) VALUES (?, ?, ?)",
                (username, salt, hash_password(password, salt)),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("That username is already taken.", "error")
            return render_template("register.html", username=username)

        flash("Account created. Please log in.")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT username, password_salt, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if user is None or not hmac.compare_digest(
            hash_password(password, user["password_salt"]), user["password_hash"]
        ):
            flash("Invalid username or password.", "error")
            return render_template("login.html", username=username)

        session.clear()
        session["username"] = user["username"]
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


init_db()

if __name__ == "__main__":
    app.run(debug=True)
