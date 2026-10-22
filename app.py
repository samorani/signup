import hashlib
import hmac
import os
import sqlite3

from flask import Flask, flash, g, redirect, render_template, request, session, url_for

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signup.db")
PBKDF2_ITERATIONS = 600_000

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


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)


@app.route("/")
def index():
    return render_template("index.html", username=session.get("username"))


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
