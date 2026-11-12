"""Attaching a PDF or an image to a booking, and who may open it."""

import io

from conftest import login, register

PDF = b"%PDF-1.7\n%test file\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
FIVE_MB = 5 * 1024 * 1024


def book(client, day, start="14:00"):
    client.post("/book", data={"day": day.isoformat(), "start": start})


def booking_id(db, day, start="14:00"):
    return db.execute(
        "SELECT id FROM bookings WHERE day = %s AND start = %s", (day, start)
    ).fetchone()[0]


def stored_name(db, bid):
    return db.execute("SELECT attachment FROM bookings WHERE id = %s", (bid,)).fetchone()[0]


def attach(client, bid, data, filename):
    return client.post(
        f"/bookings/{bid}/attachment",
        data={"file": (io.BytesIO(data), filename)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def alice_with_booking(client, db, day):
    register(client, "alice", "alice-password")
    login(client, "alice", "alice-password")
    book(client, day)
    return booking_id(db, day)


def test_owner_attaches_a_pdf_and_opens_it(client, next_monday, db, bucket):
    bid = alice_with_booking(client, db, next_monday)

    page = attach(client, bid, PDF, "my notes.pdf").data
    assert b"Attached my_notes.pdf" in page
    assert stored_name(db, bid) == "my_notes.pdf"
    assert bucket.files == {f"bookings/{bid}": (PDF, "application/pdf")}

    sheet = client.get(f"/sheet?week={next_monday.isoformat()}").data
    assert f'href="/bookings/{bid}/attachment"'.encode() in sheet
    assert b"my_notes.pdf" in sheet

    response = client.get(f"/bookings/{bid}/attachment")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.data == PDF
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_a_new_image_replaces_the_old_file(client, next_monday, db, bucket):
    bid = alice_with_booking(client, db, next_monday)
    attach(client, bid, PDF, "notes.pdf")

    assert b"Attached photo.png" in attach(client, bid, PNG, "photo.png").data
    assert stored_name(db, bid) == "photo.png"
    assert bucket.files == {f"bookings/{bid}": (PNG, "image/png")}
    assert client.get(f"/bookings/{bid}/attachment").mimetype == "image/png"


def test_only_pdfs_and_images_up_to_5_mb(client, next_monday, db, bucket):
    bid = alice_with_booking(client, db, next_monday)

    # The type is read from the content, so renaming a file does not help.
    assert b"Only PDF files and images" in attach(client, bid, b"just some text", "fake.pdf").data
    assert b"Only PDF files and images" in attach(client, bid, b"<svg></svg>", "x.svg").data
    # One byte over 5 MB is refused; exactly 5 MB is fine.
    over = PDF + b"0" * (FIVE_MB + 1 - len(PDF))
    assert b"larger than 5 MB" in attach(client, bid, over, "big.pdf").data
    # Far too large: refused before it is read.
    huge = PDF + b"0" * (7 * 1024 * 1024)
    assert b"larger than 5 MB" in attach(client, bid, huge, "huge.pdf").data
    assert b"Choose a file" in attach(client, bid, b"", "").data
    assert stored_name(db, bid) is None
    assert bucket.files == {}

    exactly = PDF + b"0" * (FIVE_MB - len(PDF))
    assert b"Attached exact.pdf" in attach(client, bid, exactly, "exact.pdf").data
    assert len(bucket.files[f"bookings/{bid}"][0]) == FIVE_MB


def test_only_owner_and_admin_can_open_the_file(client, next_monday, db, bucket):
    bid = alice_with_booking(client, db, next_monday)
    attach(client, bid, PDF, "notes.pdf")
    client.post("/logout")

    # Not logged in: sent to the login page.
    response = client.get(f"/bookings/{bid}/attachment")
    assert response.status_code == 302 and "/login" in response.headers["Location"]

    # Another user can neither open nor replace it, and does not see the link.
    register(client, "bob", "bob-password")
    login(client, "bob", "bob-password")
    assert client.get(f"/bookings/{bid}/attachment").status_code == 404
    assert b"You have no such booking." in attach(client, bid, PNG, "evil.png").data
    assert bucket.files[f"bookings/{bid}"] == (PDF, "application/pdf")
    sheet = client.get(f"/sheet?week={next_monday.isoformat()}").data
    assert b"notes.pdf" not in sheet
    client.post("/logout")

    # The administrator can open it, and sees the link.
    register(client, "admin", "admin-password")
    login(client, "admin", "admin-password")
    sheet = client.get(f"/sheet?week={next_monday.isoformat()}").data
    assert f'href="/bookings/{bid}/attachment"'.encode() in sheet
    response = client.get(f"/bookings/{bid}/attachment")
    assert response.status_code == 200 and response.data == PDF
    # ...but cannot attach to someone else's booking.
    assert b"You have no such booking." in attach(client, bid, PNG, "admin.png").data


def test_booking_without_a_file_has_nothing_to_open(client, next_monday, db):
    bid = alice_with_booking(client, db, next_monday)
    assert client.get(f"/bookings/{bid}/attachment").status_code == 404
    assert client.get("/bookings/999999/attachment").status_code == 404


def test_cancelling_deletes_the_file(client, next_monday, db, bucket):
    bid = alice_with_booking(client, db, next_monday)
    attach(client, bid, PDF, "notes.pdf")

    client.post("/cancel", data={"day": next_monday.isoformat(), "start": "14:00"})
    assert bucket.files == {}

    # Booking the slot again starts without a file.
    book(client, next_monday)
    new_bid = booking_id(db, next_monday)
    assert new_bid != bid
    assert stored_name(db, new_bid) is None


def test_cannot_attach_to_a_past_booking(client, last_monday, db, bucket):
    register(client, "alice", "alice-password")
    login(client, "alice", "alice-password")
    db.execute(
        "INSERT INTO bookings (day, start, user_id) VALUES (%s, '14:00', 1)", (last_monday,)
    )
    bid = booking_id(db, last_monday)

    assert b"That slot is in the past." in attach(client, bid, PDF, "late.pdf").data
    assert bucket.files == {}
