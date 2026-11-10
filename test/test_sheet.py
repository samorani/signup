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
    assert b"That is not a valid slot." in post_slot(client, "book", next_monday + timedelta(days=5)).data  # Saturday
    assert b"That is not a valid slot." in post_slot(client, "book", next_monday, "09:00").data
    assert booking_count(db) == 0
