"""The "ask AI" box: Claude suggests a slot, and only the person books it."""

from datetime import timedelta

import app as appmod
from conftest import login, register


def booking_count(db):
    return db.execute("SELECT count(*) FROM bookings").fetchone()[0]


def ask(client, text, week=None):
    data = {"request": text}
    if week:
        data["week"] = week.isoformat()
    return client.post("/suggest", data=data, follow_redirects=True)


def logged_in(client, name="alice"):
    register(client, name, f"{name}-password")
    login(client, name, f"{name}-password")


def test_box_is_shown_only_with_an_api_key(client, monkeypatch):
    logged_in(client)
    assert b"Ask AI to find a slot" in client.get("/sheet").data
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert b"Ask AI to find a slot" not in client.get("/sheet").data


def test_suggestion_has_a_book_button_and_books_nothing(client, claude, db, next_monday):
    logged_in(client)
    thursday = next_monday + timedelta(days=3)
    claude.answer = {"slot_id": f"{thursday.isoformat()} 14:30", "reason": "Thursday at 2:30 is free."}

    page = ask(client, "next Thursday afternoon?").data

    assert b"Suggested: Thursday" in page
    assert b"Thursday at 2:30 is free." in page
    assert b"Book this slot" in page
    assert f'name="day" value="{thursday.isoformat()}"'.encode() in page
    assert b'class="free suggested"' in page
    assert b"Messages to and from Claude" in page
    assert booking_count(db) == 0  # never booked automatically

    # The Book button posts the ordinary book form.
    client.post("/book", data={"day": thursday.isoformat(), "start": "14:30"})
    assert booking_count(db) == 1


def test_claude_gets_the_request_and_only_free_future_slots(client, claude, next_monday):
    register(client, "bob", "bob-password")
    login(client, "bob", "bob-password")
    client.post("/book", data={"day": next_monday.isoformat(), "start": "14:00"})
    client.post("/logout")
    logged_in(client)

    ask(client, "the earliest slot next week")

    message = claude.requests[0]["messages"][0]["content"]
    assert "the earliest slot next week" in message
    assert f"{next_monday.isoformat()} 14:30" in message
    assert f"{next_monday.isoformat()} 14:00" not in message  # taken by Bob
    yesterday = appmod.now().date() - timedelta(days=1)
    assert yesterday.isoformat() not in message  # past
    assert claude.requests[0]["model"] == "claude-opus-5-5"


def test_no_slot_fits(client, claude):
    logged_in(client)
    claude.answer = {"slot_id": "none", "reason": "Office hours are only in the afternoon."}
    page = ask(client, "something at 9 am").data
    assert b"No free slot fits." in page
    assert b"Office hours are only in the afternoon." in page
    assert b"Book this slot" not in page


def test_a_slot_that_is_not_free_is_never_offered(client, claude, db, next_monday):
    register(client, "bob", "bob-password")
    login(client, "bob", "bob-password")
    client.post("/book", data={"day": next_monday.isoformat(), "start": "14:00"})
    client.post("/logout")
    logged_in(client)
    claude.answer = {"slot_id": f"{next_monday.isoformat()} 14:00", "reason": "Monday at 2."}

    page = ask(client, "Monday at 2").data
    assert b"not free" in page
    assert b"Book this slot" not in page


def test_refusal_and_bad_answers_show_an_error(client, claude):
    logged_in(client)
    claude.stop_reason = "refusal"
    assert b"Claude declined" in ask(client, "anything").data

    claude.stop_reason = "end_turn"
    claude.answer = {"unexpected": True}
    assert b"could not be read" in ask(client, "anything").data


def test_empty_request_and_login_are_required(client, claude):
    assert b"Please log in first." in ask(client, "any time").data
    logged_in(client)
    assert b"Type what you are looking for" in ask(client, "   ").data
    assert claude.requests == []
