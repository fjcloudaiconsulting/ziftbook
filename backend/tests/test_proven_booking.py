"""ZIF-146: a booking made while signed in links the business's client record to the account and
records the account that placed it.

Proven by whichever live session (account or business) holds the typed email as its verified
account email; re-confirmed FOR SHARE inside the booking transaction; never by email alone.
Docs: CONTRIBUTING.md "Client records", app.clients.find_or_create, app.bookings.accounts."""

import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import auth, bookings, clients
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    PASSWORD,
    People,
    add_password,
    add_user,
    email_of,
    end_membership,
    member_id,
    new_client,
    save_setting,
    signed_in,
    wait_until_blocked,
)
from tests.test_account_sessions import account_client
from tests.test_availability_api import assign, new_service, weekdays
from tests.test_bookings_api import at, booking_url, guest_body
from tests.test_openapi import CONTRACT
from tests.test_working_hours import seed

APP_DIR = Path(__file__).parent.parent / "app"


@pytest.fixture
def app(people: People) -> FastAPI:
    save_setting(people.a, "published", True)
    return create_app()


@pytest.fixture
def ready(app: FastAPI, people: People) -> str:
    """A 30-minute service performed by A's owner, who works 09:00-17:00 every day."""
    service_id = new_service(signed_in(app, people.a, people.both))
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    return service_id


def book(client: TestClient, people: People, service: str, **overrides: Any) -> Any:
    return client.post(booking_url(people.a, service), json=guest_body(**overrides))


def client_row(tenant_id: uuid.UUID, email: str) -> Any:
    with tenant_context(tenant_id) as session:
        return session.execute(
            text("SELECT name, phone, user_id FROM clients WHERE email = :e"), {"e": email}
        ).first()


def booked_as(migrate_engine: Engine, tenant_id: uuid.UUID) -> list[uuid.UUID | None]:
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        return list(conn.scalars(text("SELECT booked_as FROM bookings ORDER BY created_at")))


@pytest.fixture(autouse=True)
def passwords_for(people: People, migrate_engine: Engine) -> None:
    add_password(migrate_engine, people.only_b, PASSWORD)


@pytest.fixture
def member_less(migrate_engine: Engine) -> uuid.UUID:
    return add_user(migrate_engine, name="Cora Client")


# FENCE (the blocker). Wrong impl: today's bookings.account(), which reads the user through the
# users policy under the session's tenant: a person with no business has no session there at all.
def test_a_person_with_no_business_proves_their_own_booking(
    app: FastAPI,
    people: People,
    ready: str,
    migrate_engine: Engine,
    member_less: uuid.UUID,
) -> None:
    client = account_client(app, migrate_engine, member_less)
    email = email_of(member_less)

    response = book(client, people, ready, email=email, name="Cora Typed", phone="+31611111111")

    assert response.status_code == 201, response.json()
    row = client_row(people.a, email)
    assert (row.name, row.phone, row.user_id) == ("Cora Typed", "+31611111111", member_less)
    assert booked_as(migrate_engine, people.a) == [member_less]


# FENCE (the blocker, 0055). Wrong impl: reading the account under the users policy, which hides a
# removed member from their former business.
def test_a_removed_member_proves_their_booking_at_their_former_business(
    app: FastAPI, people: People, ready: str, migrate_engine: Engine
) -> None:
    end_membership(people.a, people.only_a)
    client = account_client(app, migrate_engine, people.only_a)
    email = email_of(people.only_a)

    response = book(client, people, ready, email=email, name="Former Worker")

    assert response.status_code == 201
    assert client_row(people.a, email).user_id == people.only_a


# FENCE. Wrong impl: "the account session first, the business one only if there is none": one
# browser holding a client's account session and a staff member's business session.
@pytest.mark.parametrize("who", ["account holder", "business holder"])
def test_either_live_session_proves_its_own_holders_booking(
    app: FastAPI,
    people: People,
    ready: str,
    migrate_engine: Engine,
    member_less: uuid.UUID,
    who: str,
) -> None:
    client = account_client(app, migrate_engine, member_less)
    business = signed_in(app, people.b, people.only_b)
    business_token = business.cookies.get(auth.COOKIE)
    assert business_token is not None
    client.cookies.set(auth.COOKIE, business_token)
    holder = member_less if who == "account holder" else people.only_b

    response = book(client, people, ready, email=email_of(holder))

    assert response.status_code == 201
    assert client_row(people.a, email_of(holder)).user_id == holder
    assert booked_as(migrate_engine, people.a) == [holder]


# FENCE (architecture finding 9; ZIF-58's relink race depends on it). Wrong impl: no re-check
# inside the booking transaction: an erasure, reset or sign-out that commits after step 0 would
# still let the booking link and record the identity that is gone.
def test_a_session_ended_after_step_0_writes_nothing_under_it(
    app: FastAPI,
    people: People,
    ready: str,
    migrate_engine: Engine,
    member_less: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = account_client(app, migrate_engine, member_less)
    email = email_of(member_less)
    book_online = bookings.book_online

    def ended_meanwhile(*args: Any, **kwargs: Any) -> Any:
        with migrate_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM account_sessions WHERE user_id = :u"), {"u": member_less}
            )
        return book_online(*args, **kwargs)

    monkeypatch.setattr(bookings, "book_online", ended_meanwhile)

    response = book(client, people, ready, email=email)

    assert (response.status_code, response.json()) == (401, {"code": "unauthenticated"})
    assert client_row(people.a, email) is None
    assert booked_as(migrate_engine, people.a) == []


# FENCE. Wrong impl: proving by "has a session" rather than by the typed email.
def test_a_signed_in_person_typing_another_address_books_as_a_guest(
    app: FastAPI,
    people: People,
    ready: str,
    migrate_engine: Engine,
    member_less: uuid.UUID,
) -> None:
    client = account_client(app, migrate_engine, member_less)
    other = email_of(people.only_b)
    with tenant_context(people.a) as session:
        session.execute(
            text("""
            INSERT INTO clients (tenant_id, name, email)
            VALUES (current_setting('app.tenant_id')::uuid, 'Kept Name', :e)
            """),
            {"e": other},
        )

    response = book(client, people, ready, email=other, name="Typed Name")

    assert response.status_code == 201
    row = client_row(people.a, other)
    assert (row.name, row.user_id) == ("Kept Name", None)
    assert booked_as(migrate_engine, people.a) == [None]


def test_a_guest_and_a_merchant_booking_record_no_account(
    app: FastAPI, people: People, ready: str, migrate_engine: Engine
) -> None:
    guest = book(new_client(app), people, ready)
    assert guest.status_code == 201
    owner = signed_in(app, people.a, people.both)
    merchant = owner.post(
        "/api/bookings",
        json={
            "service_id": ready,
            "new_client": {"name": "Walk In", "email": email_of(people.only_b)},
            "starts_at": at("10:00"),
        },
    )
    assert merchant.status_code == 201, merchant.json()

    assert booked_as(migrate_engine, people.a) == [None, None]


# FENCE. Wrong impl: a response schema carrying booked_as (it maps a booking to a person).
def test_no_schema_carries_the_placing_account() -> None:
    spec = json.loads(CONTRACT.read_text())
    carrying = [
        name
        for name, schema in spec["components"]["schemas"].items()
        if "booked_as" in schema.get("properties", {})
    ]
    assert carrying == []


# FENCE. Wrong impl: a merchant query (the calendar, the client card, a CSV export) selecting
# booked_as: it is personal data, written by the booking page's one insert and read only by the
# person's own history (own_bookings, in the database).
def test_only_the_booking_insert_names_the_placing_account() -> None:
    naming = {
        path.name: len(re.findall(r"\bbooked_as\b", path.read_text()))
        for path in APP_DIR.rglob("*.py")
        if "booked_as" in path.read_text()
    }
    source = (APP_DIR / "bookings.py").read_text()
    insert = source[source.index("INSERT_BOOKING = text(") : source.index("INSERT_EVENT = text(")]
    assert set(naming) == {"bookings.py"}
    # The INSERT's column and value; place()'s parameter and its bind (key and value); the one
    # argument book_online passes. Nothing that reads it.
    assert len(re.findall(r"\bbooked_as\b", insert)) == 2
    assert naming["bookings.py"] == 2 + 4


# FENCE (code review N-1, N-2). Wrong impl: a re-check with laxer rules than the resolver: an
# account session whose console session ran out, or whose holder became an admin, after step 0
# would still prove, link and record the booking.
@pytest.mark.parametrize("meanwhile", ["console session expired", "made a platform admin"])
def test_the_re_check_applies_the_account_session_rules(
    app: FastAPI,
    people: People,
    ready: str,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    meanwhile: str,
) -> None:
    client = new_client(app)
    signed = client.post(
        "/api/session", json={"email": email_of(people.only_b), "password": PASSWORD}
    )
    assert signed.status_code == 200
    business = client.cookies.get(auth.COOKIE)
    assert business is not None
    client.cookies.delete(auth.COOKIE)  # the account session alone proves the booking
    book_online = bookings.book_online

    def changed_meanwhile(*args: Any, **kwargs: Any) -> Any:
        with migrate_engine.begin() as conn:
            if meanwhile == "console session expired":
                conn.execute(
                    # Before the booking transaction's own now(), which started first.
                    text(
                        "UPDATE sessions SET expires_at = now() - interval '1 minute'"
                        " WHERE id_hash = :h"
                    ),
                    {"h": auth.hash_token(business)},
                )
            else:
                conn.execute(
                    text("INSERT INTO platform_admins (user_id) VALUES (:u)"), {"u": people.only_b}
                )
        return book_online(*args, **kwargs)

    monkeypatch.setattr(bookings, "book_online", changed_meanwhile)
    try:
        response = book(client, people, ready, email=email_of(people.only_b))
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM platform_admins WHERE user_id = :u"), {"u": people.only_b}
            )

    assert (response.status_code, response.json()) == (401, {"code": "unauthenticated"})
    assert client_row(people.a, email_of(people.only_b)) is None


# FENCE (code review N2; ZIF-58's relink fence depends on it). Wrong impl: a re-check that reads
# the session without locking it: a sign-out, reset or erasure committing between the re-check and
# the booking's commit would leave a link and booked_as under an identity that is gone. And the
# lock is FOR KEY SHARE, not FOR SHARE: the person's other requests, which touch last_seen_at, must
# not wait on the booking.
@pytest.mark.parametrize(
    ("kind", "table", "cookie"),
    [
        ("account", "account_sessions", auth.ACCOUNT_COOKIE),
        ("business", "sessions", auth.COOKIE),
    ],
)
def test_a_session_delete_waits_for_the_booking_its_session_proves(
    app: FastAPI,
    people: People,
    ready: str,
    migrate_engine: Engine,
    member_less: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    table: str,
    cookie: str,
) -> None:
    person = member_less if kind == "account" else people.only_b
    if kind == "account":
        client = account_client(app, migrate_engine, person)
    else:
        client = signed_in(app, people.b, person)
    token = client.cookies.get(cookie)
    assert token is not None
    held, release = threading.Event(), threading.Event()
    find_or_create = clients.find_or_create

    def pause_after_the_re_check(*args: Any, **kwargs: Any) -> Any:
        held.set()
        assert release.wait(timeout=20)
        return find_or_create(*args, **kwargs)

    monkeypatch.setattr(clients, "find_or_create", pause_after_the_re_check)
    answers: list[Any] = []
    booking = threading.Thread(
        target=lambda: answers.append(book(client, people, ready, email=email_of(person)))
    )
    booking.start()
    assert held.wait(timeout=20)

    erased: list[int] = []

    def erase() -> None:
        with migrate_engine.begin() as conn:
            erased.append(
                conn.execute(
                    text(f"DELETE FROM {table} WHERE user_id = :u"), {"u": person}
                ).rowcount
            )

    eraser = threading.Thread(target=erase)
    eraser.start()
    try:
        wait_until_blocked(migrate_engine, 1)  # the delete waits on the booking's lock
        # The person's other tab is not held up: touching last_seen_at takes no conflicting lock.
        with migrate_engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '1s'"))
            conn.execute(
                text(f"UPDATE {table} SET last_seen_at = now() WHERE id_hash = :h"),
                {"h": auth.hash_token(token)},
            )
    finally:
        release.set()
        booking.join(timeout=20)
        eraser.join(timeout=20)

    assert answers[0].status_code == 201, answers[0].json()
    assert erased == [1]
    assert booked_as(migrate_engine, people.a) == [person]
