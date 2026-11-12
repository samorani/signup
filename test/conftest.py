"""Shared test setup.

The tests run against DATABASE_URL_TEST from .env, and never against the
production (or development) database:

* app.py picks its database when it is imported: DATABASE_URL_PROD on Cloud Run
  (K_SERVICE set), DATABASE_URL_DEV otherwise. Before importing it, this file
  removes K_SERVICE, points DATABASE_URL_DEV at the test database and blanks
  DATABASE_URL_PROD, so there is no production URL in the process at all.
  (load_dotenv in app.py does not override variables that are already set.)
* It refuses to run if DATABASE_URL_TEST is missing or equals the dev or
  production URL.
* The test database is emptied (its tables dropped) before app.py is imported;
  importing app.py then recreates empty tables, and every test starts from
  empty tables.
"""

import os
import sys
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values
from google.api_core import exceptions as gcs_errors

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_dotenv = dotenv_values(ROOT / ".env")


def _setting(name):
    return (os.environ.get(name) or _dotenv.get(name) or "").strip()


TEST_DB_URL = _setting("DATABASE_URL_TEST")
if not TEST_DB_URL:
    raise RuntimeError("DATABASE_URL_TEST is not set. Add it to .env; the tests use only that database.")
for _other in ("DATABASE_URL_PROD", "DATABASE_URL_DEV"):
    if _setting(_other) and _setting(_other) == TEST_DB_URL:
        raise RuntimeError(f"DATABASE_URL_TEST is the same as {_other}. Refusing to run the tests on it.")

os.environ.pop("K_SERVICE", None)        # never run app.py in production mode
os.environ["DATABASE_URL_PROD"] = ""     # no production URL in the test process
os.environ["DATABASE_URL_DEV"] = TEST_DB_URL
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ["ADMIN_USERNAME"] = "admin"
# Never the real bucket: the tests replace it with FakeBucket below anyway.
os.environ["ATTACHMENTS_BUCKET"] = "test-attachments-not-a-real-bucket"

# Empty the test database before app.py's init_db() recreates the tables.
with psycopg.connect(TEST_DB_URL, autocommit=True) as _conn:
    _conn.execute("DROP TABLE IF EXISTS bookings, users CASCADE")

import app as appmod  # noqa: E402  (must come after the environment is set up)

assert appmod.DATABASE_URL == TEST_DB_URL, "app.py is not using the test database"
assert not appmod.IS_PRODUCTION


@pytest.fixture(autouse=True)
def empty_tables():
    """Every test starts with no users and no bookings."""
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute("TRUNCATE bookings, users RESTART IDENTITY CASCADE")
    yield


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def upload_from_string(self, data, content_type=None):
        self.bucket.files[self.name] = (bytes(data), content_type)

    def download_as_bytes(self):
        if self.name not in self.bucket.files:
            raise gcs_errors.NotFound(self.name)
        return self.bucket.files[self.name][0]

    def delete(self):
        if self.bucket.files.pop(self.name, None) is None:
            raise gcs_errors.NotFound(self.name)


class FakeBucket:
    """An in-memory stand-in for the attachments bucket: {object name: (bytes, content type)}."""

    def __init__(self):
        self.files = {}

    def blob(self, name):
        return FakeBlob(self, name)


@pytest.fixture(autouse=True)
def bucket(monkeypatch):
    """Every test gets an empty fake bucket; no test ever reaches Cloud Storage."""
    fake = FakeBucket()
    monkeypatch.setattr(appmod, "attachments_bucket", lambda: fake)
    return fake


@pytest.fixture
def app():
    appmod.app.config.update(TESTING=True)
    return appmod.app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db():
    """A direct connection to the test database, for checking what was stored."""
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        yield conn


def register(client, username, password, follow_redirects=False):
    return client.post(
        "/register",
        data={"username": username, "password": password},
        follow_redirects=follow_redirects,
    )


def login(client, username, password, follow_redirects=False):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=follow_redirects,
    )


@pytest.fixture
def next_monday():
    """Monday of next week: all its slots are in the future."""
    return appmod.week_start(appmod.now().date()) + timedelta(days=7)


@pytest.fixture
def last_monday():
    """Monday of last week: all its slots are in the past."""
    return appmod.week_start(appmod.now().date()) - timedelta(days=7)
