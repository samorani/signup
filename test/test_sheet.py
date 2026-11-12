"""The sign-up sheet: booking and cancelling slots."""

from datetime import timedelta

from conftest import login, register


def booking_count(db):
    return db.execute("SELECT count(*) FROM bookings").fetchone()[0]


def post_slot(client, action, day, start="14:00"):
    return client.post(
        f"/{action}", data={"day": day.isoformat(), "start": start}, follow_redirects=True
    )


def test_booking_and_cancelling(client, next_monday, db):
    register(client, "alice", "alice-password")
    register(client, "bob", "bob-password")

    login(client, "alice", "alice-password")
    assert b"Booked" in post_slot(client, "book", next_monday).data
    client.post("/logout")

    # Bob can neither take nor cancel Alice's slot.
    login(client, "bob", "bob-password")
    assert b"already been taken" in post_slot(client, "book", next_monday).data
    assert b"You have no booking in that slot." in post_slot(client, "cancel", next_monday).data
    assert booking_count(db) == 1
    client.post("/logout")

    # Alice can cancel her own.
    login(client, "alice", "alice-password")
    assert b"Cancelled" in post_slot(client, "cancel", next_monday).data
    assert booking_count(db) == 0


def test_cannot_book_past_or_invalid_slots(client, next_monday, last_monday, db):
    register(client, "alice", "alice-password")
    login(client, "alice", "alice-password")

    assert b"That slot is in the past." in post_slot(client, "book", last_monday).data
    assert b"That is not a valid slot." in client.post(
        "/book", data={"day": "not-a-date", "start": "14:00"}, follow_redirects=True
    ).data
    assert b"That is not a valid slot." in post_slot(client, "book", next_monday, "09:00").data
    assert booking_count(db) == 0


def test_sheet_shows_monday_to_sunday(client, next_monday):
    register(client, "alice", "alice-password")
    login(client, "alice", "alice-password")

    page = client.get(f"/sheet?week={next_monday.isoformat()}").data
    for name in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
        assert f"<th>{name}<small>".encode() in page
    next_sunday = next_monday + timedelta(days=6)
    assert f'name="day" value="{next_sunday.isoformat()}"'.encode() in page
    # The following Monday belongs to the next week, not this one.
    assert f'name="day" value="{(next_monday + timedelta(days=7)).isoformat()}"'.encode() not in page


def test_booking_and_cancelling_on_sunday(client, next_monday, db):
    next_sunday = next_monday + timedelta(days=6)
    assert next_sunday.weekday() == 6
    register(client, "alice", "alice-password")
    register(client, "bob", "bob-password")

    login(client, "alice", "alice-password")
    page = post_slot(client, "book", next_sunday, "15:30").data
    assert b"Booked Sunday" in page
    assert db.execute(
        "SELECT day, start FROM bookings"
    ).fetchall() == [(next_sunday, "15:30")]
    client.post("/logout")

    # Bob sees Sunday's slot as taken and cannot book it.
    login(client, "bob", "bob-password")
    assert b"already been taken" in post_slot(client, "book", next_sunday, "15:30").data
    client.post("/logout")

    # Alice can cancel her Sunday booking.
    login(client, "alice", "alice-password")
    assert b"Cancelled Sunday" in post_slot(client, "cancel", next_sunday, "15:30").data
    assert booking_count(db) == 0


def test_sunday_redirects_to_its_own_week(client, next_monday):
    """A Sunday booking sends you back to the week that starts the Monday before it."""
    next_sunday = next_monday + timedelta(days=6)
    register(client, "alice", "alice-password")
    login(client, "alice", "alice-password")

    response = client.post("/book", data={"day": next_sunday.isoformat(), "start": "14:30"})
    assert response.status_code == 302
    assert f"week={next_monday.isoformat()}" in response.headers["Location"]
