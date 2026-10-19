"""ZIF-152: online bookings need a phone, read in the business's country and stored as E.164.

Both public paths (the signed-in and the guest POST .../bookings) share
bookings.book_online, and both console paths (POST /api/clients and the calendar's new client)
share clients.insert; a fence per path proves each one parses.
"""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    People,
    fresh_email,
    member_id,
    new_client,
    put_settings,
    save_setting,
    signed_in,
)
from tests.test_availability_api import assign, new_service, weekdays
from tests.test_booking_page_api import slug_of
from tests.test_bookings_api import post_booking, post_guest
from tests.test_merchant_booking_api import book
from tests.test_working_hours import seed


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture(autouse=True)
def _published(people: People) -> None:
    save_setting(people.a, "published", True)


@pytest.fixture
def ready(people: People, owner: TestClient) -> str:
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    return service_id


def set_country(migrate_engine: Engine, tenant_id: uuid.UUID, country: str) -> None:
    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET country = :c WHERE id = :t"), {"c": country, "t": tenant_id}
        )


def phone_of(tenant_id: uuid.UUID, email: str) -> Any:
    with tenant_context(tenant_id) as session:
        return session.scalar(text("SELECT phone FROM clients WHERE email = :e"), {"e": email})


def counts(tenant_id: uuid.UUID) -> tuple[int, int]:
    with tenant_context(tenant_id) as session:
        return (
            session.scalar(text("SELECT count(*) FROM bookings")) or 0,
            session.scalar(text("SELECT count(*) FROM clients")) or 0,
        )


# F1: FENCE. Wrong impl: the guest booking's phone left optional.
def test_a_guest_booking_without_a_phone_books_nothing(
    people: People, app: FastAPI, ready: str
) -> None:
    answer = post_guest(new_client(app), people.a, ready, phone=None)

    assert (answer.status_code, answer.json()) == (422, {"code": "invalid_request"})
    assert counts(people.a) == (0, 0)


# F2: FENCE. Wrong impl: the signed-in booking's phone left optional.
def test_a_signed_in_booking_without_a_phone_books_nothing(
    people: People, app: FastAPI, ready: str
) -> None:
    answer = post_booking(new_client(app), people.a, ready, phone=None)

    assert (answer.status_code, answer.json()) == (422, {"code": "invalid_request"})
    assert counts(people.a) == (0, 0)


# F3, F7: FENCE. Wrong impl: the phone stored as typed, or the business's region forced onto a
# number that names its own country.
@pytest.mark.parametrize(
    ("typed", "stored"),
    [("06 1234 5678", "+31612345678"), ("+44 7911 123456", "+447911123456")],
)
def test_a_guest_booking_stores_the_phone_in_one_international_format(
    people: People, app: FastAPI, ready: str, typed: str, stored: str
) -> None:
    email = fresh_email()

    assert post_guest(new_client(app), people.a, ready, email=email, phone=typed).status_code == 201
    assert phone_of(people.a, email) == stored


# F4: FENCE. Wrong impl: a hardcoded region ("NL" reads these digits as +31912345678).
def test_a_local_number_is_read_in_the_business_s_country(
    people: People, app: FastAPI, ready: str, migrate_engine: Engine
) -> None:
    set_country(migrate_engine, people.a, "PT")
    answer = post_booking(new_client(app), people.a, ready, phone="912 345 678")

    assert answer.status_code == 201
    with tenant_context(people.a) as session:
        email = session.scalar(text("SELECT email FROM clients"))
    assert phone_of(people.a, email) == "+351912345678"


# F5, F6: FENCE. Wrong impl: only a parse failure refused (91 234 parses, but is no number), the
# parse error left uncaught (a 500), letters read as a keypad's digits (06 1234 ABCD would be
# +31612342223), or the generic invalid_request the page can't place.
@pytest.mark.parametrize("typed", ["91 234", "abc", "06 1234 ABCD"])
def test_an_unreadable_phone_is_refused_by_code_on_both_public_paths(
    people: People, app: FastAPI, ready: str, typed: str
) -> None:
    guest = post_guest(new_client(app), people.a, ready, phone=typed)
    posted = post_booking(new_client(app), people.a, ready, phone=typed)

    for answer in (guest, posted):
        assert (answer.status_code, answer.json()) == (422, {"code": "phone_invalid"})
    assert counts(people.a) == (0, 0)


# F8: FENCE. Wrong impl: the phone parsed before the published check, so a 422 tells a prober an
# unpublished business exists.
def test_an_unpublished_business_answers_not_found_before_the_phone(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    assert put_settings(owner, {"published": False}).status_code == 200

    answer = post_booking(new_client(app), people.a, ready, phone="abc")

    assert (answer.status_code, answer.json()) == (404, {"code": "not_found"})


# F9: FENCE. Wrong impl: the console's client form left unparsed (or made to need a phone).
def test_the_console_checks_a_phone_it_is_given_and_needs_none(
    people: People, owner: TestClient
) -> None:
    bad = owner.post("/api/clients", json={"name": "Bad", "phone": "91 234"})
    good = owner.post("/api/clients", json={"name": "Good", "phone": "06 1234 5678"})
    none = owner.post("/api/clients", json={"name": "None"})

    assert (bad.status_code, bad.json()) == (422, {"code": "phone_invalid"})
    assert (good.status_code, good.json()["phone"]) == (201, "+31612345678")
    assert (none.status_code, none.json()["phone"]) == (201, None)


# F10: FENCE. Wrong impl: the parse only in POST /api/clients, not in the shared insert.
def test_the_calendar_s_new_client_phone_is_checked(
    people: People, owner: TestClient, ready: str
) -> None:
    answer = book(owner, ready, new_client={"name": "Walk-in", "phone": "abc"})

    assert (answer.status_code, answer.json()) == (422, {"code": "phone_invalid"})
    assert counts(people.a) == (0, 0)


# F11: FENCE. Wrong impl: a hardcoded country in the public answer the hint reads.
def test_the_public_page_carries_the_business_s_country(
    people: People, app: FastAPI, ready: str, migrate_engine: Engine
) -> None:
    set_country(migrate_engine, people.a, "PT")

    page = new_client(app).get(f"/api/public/booking-pages/{slug_of(migrate_engine, people.a)}")

    assert page.json()["country"] == "PT"
