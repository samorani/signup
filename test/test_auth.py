"""Registration and login."""

from conftest import login, register


def user_count(db, username):
    return db.execute("SELECT count(*) FROM users WHERE username = %s", (username,)).fetchone()[0]


def test_register_creates_user_with_hashed_password(client, db):
    response = register(client, "alice", "longenough1")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    stored = db.execute("SELECT password_hash FROM users WHERE username = 'alice'").fetchone()[0]
    assert "longenough1" not in stored


def test_password_must_be_at_least_8_characters(client, db):
    response = register(client, "alice", "seven77")  # 7 characters
    assert response.status_code == 200
    assert b"at least 8 characters" in response.data
    assert user_count(db, "alice") == 0

    response = register(client, "alice", "eight888")  # 8 characters
    assert response.status_code == 302
    assert user_count(db, "alice") == 1


def test_register_rejects_duplicate_username(client, db):
    register(client, "alice", "longenough1")
    response = register(client, "alice", "another-password")
    assert b"already taken" in response.data
    assert user_count(db, "alice") == 1


def test_login_checks_the_password(client):
    register(client, "alice", "longenough1")

    response = login(client, "alice", "wrong-password")
    assert b"Incorrect username or password." in response.data
    with client.session_transaction() as session:
        assert "user_id" not in session

    response = login(client, "alice", "longenough1")
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session.get("user_id") is not None
