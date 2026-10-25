"""ZIF-146: a signed-in person sees every booking their account placed, at every business (owner
ruling 2, 2026-10-25). Never a guest booking made with their address, never a merchant's entry;
never reachable with a business session; paginated with no cap."""

import secrets
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import auth
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    People,
    add_user,
    email_of,
    member_id,
    new_client,
    save_setting,
    set_role,
    signed_in,
)
from tests.test_account_sessions import account_client
from tests.test_availability_api import assign, new_service, weekdays
from tests.test_bookings_api import at, booking_url, guest_body
from tests.test_clients_db import seed_client
from tests.test_working_hours import seed

APP_DIR = Path(__file__).parent.parent / "app"


@pytest.fixture
def app(people: People) -> FastAPI:
    save_setting(people.a, "published", True)
    save_setting(people.b, "published", True)
    return create_app()


@pytest.fixture
def services(app: FastAPI, people: People) -> dict[uuid.UUID, str]:
    """A service at A (performed by its owner) and one at B (by only_b, made its owner)."""
    set_role(people.b, people.only_b, "owner")
    found = {}
    for tenant, performer in ((people.a, people.both), (people.b, people.only_b)):
        service_id = new_service(signed_in(app, tenant, performer))
        seed(tenant, performer, weekdays("09:00", "17:00"))
        assign(tenant, service_id, member_id(tenant, performer))
        found[tenant] = service_id
    return found


@pytest.fixture
def person(migrate_engine: Engine) -> uuid.UUID:
    return add_user(migrate_engine, name="Cora Client")


def book(
    client: TestClient, tenant: uuid.UUID, service: str, clock: str, email: str
) -> dict[str, Any]:
    response = client.post(
        booking_url(tenant, service), json=guest_body(starts_at=at(clock), email=email)
    )
    assert response.status_code == 201, response.json()
    body: dict[str, Any] = response.json()
    return body


def history(client: TestClient, **params: Any) -> Any:
    return client.get("/api/account/history", params=params)


def starts(page: dict[str, Any]) -> list[tuple[str, str]]:
    return [(b["business"]["name"], b["starts_at"]) for b in page["bookings"]]


# FENCE (security B1, owner ruling 2). Wrong impl: history over every booking of the person's linked
# client rows: a guest booking anyone made with their address, or a merchant's entry, would show.
def test_only_the_bookings_this_account_placed_are_listed(
    app: FastAPI,
    people: People,
    services: dict[uuid.UUID, str],
    migrate_engine: Engine,
    person: uuid.UUID,
) -> None:
    mine = account_client(app, migrate_engine, person)
    email = email_of(person)
    placed_a = book(mine, people.a, services[people.a], "09:00", email)
    placed_b = book(mine, people.b, services[people.b], "11:00", email)
    # The same address, typed by a guest: the client row is the person's, the booking is not.
    book(new_client(app), people.a, services[people.a], "10:00", email)
    # A merchant's own entry for that client: the same linked client row, not placed by them.
    owner = signed_in(app, people.a, people.both)
    with tenant_context(people.a) as session:
        client_id = session.scalar(text("SELECT id FROM clients WHERE email = :e"), {"e": email})
    entered = owner.post(
        "/api/bookings",
        json={
            "service_id": services[people.a],
            "client_id": str(client_id),
            "starts_at": at("12:00"),
        },
    )
    assert entered.status_code == 201, entered.json()

    page = history(mine).json()

    assert starts(page) == [("b", placed_b["starts_at"]), ("a", placed_a["starts_at"])]
    assert page["next"] is None
    assert page["bookings"][0] == {
        "business": {"name": "b", "slug": page["bookings"][0]["business"]["slug"]},
        "starts_at": placed_b["starts_at"],
        "ends_at": placed_b["ends_at"],
        "status": placed_b["status"],
        "service_name": placed_b["service_name"],
        "price": placed_b["price"],
        "worker_display_name": placed_b["worker_display_name"],
    }


# FENCE. Wrong impl: a per-business cap without the merge (each business's newest `limit`, pasted
# together), which repeats or drops bookings across pages.
def test_every_booking_appears_exactly_once_across_pages(
    app: FastAPI,
    people: People,
    services: dict[uuid.UUID, str],
    migrate_engine: Engine,
    person: uuid.UUID,
) -> None:
    mine = account_client(app, migrate_engine, person)
    email = email_of(person)
    placed = [
        book(mine, tenant, services[tenant], clock, email)["starts_at"]
        for tenant, clock in (
            (people.a, "09:00"),
            (people.b, "09:30"),
            (people.a, "10:00"),
            (people.b, "10:30"),
            (people.a, "11:00"),
        )
    ]

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = history(mine, **params).json()
        seen += [b["starts_at"] for b in page["bookings"]]
        pages += 1
        cursor = page["next"]
        if cursor is None:
            break
        assert len(page["bookings"]) == 2

    assert seen == sorted(placed, reverse=True)
    assert pages == 3


# FENCE (probe 11). Wrong impl: a history function that also resolves business sessions, or one
# keyed by a user id the console holds.
def test_a_business_session_gets_no_history(
    app: FastAPI, people: People, app_engine: Engine
) -> None:
    business = signed_in(app, people.a, people.both)

    assert history(business).status_code == 401
    token = business.cookies.get(auth.COOKIE)
    assert token is not None
    with app_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT * FROM own_bookings(:h, NULL, NULL, 100)"),
            {"h": auth.hash_token(token)},
        ).all()
    assert rows == []


# FENCE (security N10). Wrong impl: history on any live session, however old.
def test_history_needs_a_session_started_within_a_day(
    app: FastAPI, migrate_engine: Engine, person: uuid.UUID
) -> None:
    token = secrets.token_urlsafe(32)
    with migrate_engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO account_sessions (id_hash, user_id, started_at, expires_at)
            VALUES (:h, :u, now() - interval '25 hours', now() + interval '29 days')
            """),
            {"h": auth.hash_token(token), "u": person},
        )
    client = new_client(app)
    client.cookies.set(auth.ACCOUNT_COOKIE, token)

    response = history(client)

    assert (response.status_code, response.json()) == (403, {"code": "reauthentication_required"})
    assert client.get("/api/account").status_code == 200  # the details still fill in


def copy_booking(migrate_engine: Engine, tenant: uuid.UUID, starts_at: str, **columns: Any) -> None:
    """A copy of the tenant's one booking at another time, with the given columns, as migrate."""
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
        conn.execute(
            text("""
            INSERT INTO bookings (tenant_id, client_id, worker_id, service_id, starts_at, ends_at,
              status, expires_at, source, service_name, price_amount_minor, price_currency,
              duration_minutes, auto_confirm_at_booking, free_cancellation_hours,
              reschedule_cutoff_hours, booked_as, checkout_hash)
            SELECT tenant_id, client_id, worker_id, service_id, CAST(:s AS timestamptz),
              CAST(:s AS timestamptz) + interval '30 minutes', :status, now() + interval '1 hour',
              source, service_name, price_amount_minor, price_currency, duration_minutes,
              auto_confirm_at_booking, free_cancellation_hours, reschedule_cutoff_hours, booked_as,
              :checkout_hash
            FROM bookings LIMIT 1
            """),
            {"s": starts_at, **columns},
        )


# FENCE. Wrong impl: listing a checkout that never completed as a booking.
def test_an_open_or_abandoned_checkout_is_not_listed(
    app: FastAPI,
    people: People,
    services: dict[uuid.UUID, str],
    migrate_engine: Engine,
    person: uuid.UUID,
) -> None:
    mine = account_client(app, migrate_engine, person)
    placed = book(mine, people.a, services[people.a], "09:00", email_of(person))
    secret = secrets.token_bytes(32)
    copy_booking(
        migrate_engine, people.a, at("13:00"), status="awaiting_payment", checkout_hash=secret
    )
    copy_booking(migrate_engine, people.a, at("14:00"), status="expired", checkout_hash=secret)
    copy_booking(migrate_engine, people.a, at("15:00"), status="expired", checkout_hash=None)

    listed = [b["starts_at"] for b in history(mine).json()["bookings"]]

    # The request that lapsed unanswered is a booking that expired; the checkouts are not.
    assert listed == [at("15:00"), placed["starts_at"]]


# FENCE (code review B1). Wrong impl: own_bookings leaving the caller's tenant set to the last
# business it walked. The caller's value is no business at all, so the walk can never end on it.
CALLER_TENANT = uuid.uuid4()


def test_the_walk_restores_the_callers_tenant(
    people: People, app_engine: Engine, migrate_engine: Engine, person: uuid.UUID
) -> None:
    token = secrets.token_urlsafe(32)
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO account_sessions (id_hash, user_id, expires_at)"
                " VALUES (:h, :u, now() + interval '1 day')"
            ),
            {"h": auth.hash_token(token), "u": person},
        )
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(CALLER_TENANT)}
        )
        conn.execute(
            text("SELECT * FROM own_bookings(:h, NULL, NULL, 10)"), {"h": auth.hash_token(token)}
        ).all()
        assert conn.scalar(text("SELECT current_setting('app.tenant_id')")) == str(CALLER_TENANT)


@pytest.mark.parametrize("cursor", ["x", "e30", "bm90LWpzb24", "eyJzIjogIjIwMjYiLCAiayI6ICIwMCJ9"])
def test_a_cursor_that_is_not_ours_is_refused(
    app: FastAPI, migrate_engine: Engine, person: uuid.UUID, cursor: str
) -> None:
    response = history(account_client(app, migrate_engine, person), cursor=cursor)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})


def test_history_is_for_our_own_pages_and_never_cached(
    app: FastAPI, migrate_engine: Engine, person: uuid.UUID
) -> None:
    client = account_client(app, migrate_engine, person)

    assert history(client).headers["cache-control"] == "no-store, private"
    cross = client.get("/api/account/history", headers={"Sec-Fetch-Site": "cross-site"})
    assert (cross.status_code, cross.json()) == (403, {"code": "cross_site"})


# FENCE (security N4). Wrong impl: no limit on a route that walks every business.
def test_history_is_rate_limited_per_account(
    app: FastAPI, migrate_engine: Engine, person: uuid.UUID
) -> None:
    client = account_client(app, migrate_engine, person)

    statuses = [history(client).status_code for _ in range(31)]

    assert statuses == [200] * 30 + [429]


# FENCE. Wrong impl: a console route (a "booked elsewhere" badge on the client card) calling it.
def test_only_the_history_route_calls_own_bookings() -> None:
    callers = sorted(
        path.name for path in APP_DIR.rglob("*.py") if "own_bookings" in path.read_text()
    )
    assert callers == ["platform_account.py"]


# FENCE (code review N6). Wrong impl: a cursor on starts_at alone: of two bookings at the same
# instant (two businesses, or two workers), the one on the next page would never show.
def test_bookings_at_the_same_instant_each_appear_once_across_pages(
    app: FastAPI,
    people: People,
    services: dict[uuid.UUID, str],
    migrate_engine: Engine,
    person: uuid.UUID,
) -> None:
    mine = account_client(app, migrate_engine, person)
    email = email_of(person)
    book(mine, people.a, services[people.a], "10:00", email)
    book(mine, people.b, services[people.b], "10:00", email)

    first = history(mine, limit=1).json()
    second = history(mine, limit=1, cursor=first["next"]).json()

    assert second["next"] is None
    names = [b["business"]["name"] for b in first["bookings"] + second["bookings"]]
    assert sorted(names) == ["a", "b"]


# The spec's PR3 list (code review N7). Wrong impl: history through the person's linked client
# rows: a merchant's booking on a row linked to them, under another address, would show.
def test_a_merchant_booking_on_a_row_linked_to_me_is_not_mine(
    app: FastAPI,
    people: People,
    services: dict[uuid.UUID, str],
    migrate_engine: Engine,
    person: uuid.UUID,
) -> None:
    other_email = f"other-{uuid.uuid4().hex[:8]}@example.com"
    client_id = seed_client(people.a, name="Linked Elsewhere", email=other_email, user_id=person)
    owner = signed_in(app, people.a, people.both)
    entered = owner.post(
        "/api/bookings",
        json={
            "service_id": services[people.a],
            "client_id": str(client_id),
            "starts_at": at("13:00"),
        },
    )
    assert entered.status_code == 201

    assert history(account_client(app, migrate_engine, person)).json()["bookings"] == []


# FENCE (code review N7). Wrong impl: own_bookings trusting the route's freshness check: the definer
# answers for any live session if called directly.
def test_the_definer_itself_needs_a_session_started_within_a_day(
    people: People,
    services: dict[uuid.UUID, str],
    app: FastAPI,
    app_engine: Engine,
    migrate_engine: Engine,
    person: uuid.UUID,
) -> None:
    mine = account_client(app, migrate_engine, person)
    book(mine, people.a, services[people.a], "09:00", email_of(person))
    token = mine.cookies.get(auth.ACCOUNT_COOKIE)
    assert token is not None
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE account_sessions SET started_at = now() - interval '25 hours'"
                " WHERE id_hash = :h"
            ),
            {"h": auth.hash_token(token)},
        )
    with app_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT * FROM own_bookings(:h, NULL, NULL, 10)"), {"h": auth.hash_token(token)}
        ).all()
    assert rows == []
