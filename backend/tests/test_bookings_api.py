"""POST /api/public/businesses/{tenant_id}/services/{service_id}/bookings (ZIF-51): the route end
to end, concurrency, and anti-abuse.

READ FIRST (spec SS14.0). This file must NOT monkeypatch availability.now: the write path takes
its clock from Postgres (row.now), so pinning the app clock and posting a date computed from it
would post a date months in the past as far as the transaction is concerned, and every candidate
would be refused. Every day here is computed from the REAL clock, two days out, with working hours
seeded on every weekday so the test never cares which weekday it lands on.
"""

import json
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from datetime import time as time_cls
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, event, text
from sqlalchemy.exc import OperationalError

from app import availability, bookings, members, schedule, turnstile
from app.db import tenant_context
from app.main import create_app
from app.schedule import to_utc
from tests.conftest import (
    People,
    add_membership,
    add_user,
    delete_bookings,
    email_of,
    events,
    fresh_address,
    fresh_email,
    member_id,
    new_client,
    put_settings,
    save_setting,
    set_role,
    signed_in,
    wait_until_blocked,
)
from tests.test_availability_api import assign, new_service, weekdays
from tests.test_opening_hours import seed_opening
from tests.test_working_hours import seed

ZONE = "Europe/Amsterdam"
TODAY = date.today()
DAY = TODAY + timedelta(days=2)


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture
def ready(people: People, owner: TestClient) -> str:
    """A 30-minute service performed by the owner, who works 09:00-17:00 every day."""
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    return service_id


def at(clock_time: str, day: date = DAY) -> str:
    """A local time on `day` (default: two real days out), as the API writes it, in UTC."""
    instant = to_utc(day, time_cls.fromisoformat(clock_time), ZONE)
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def booking_url(tenant_id: object, service_id: object) -> str:
    return f"/api/public/businesses/{tenant_id}/services/{service_id}/bookings"


def post_booking(
    client: TestClient, tenant_id: object, service_id: object, **overrides: Any
) -> Response:
    body: dict[str, Any] = {
        "starts_at": at("09:00"),
        "name": "Guest",
        "email": fresh_email(),
        "policy_version": "2026-09-01",
        "consents": {},
        **overrides,
    }
    return client.post(booking_url(tenant_id, service_id), json=body)


# 14: GUARD. The happy path.
def test_a_guest_books_a_slot_the_public_page_offered(
    people: People, app: FastAPI, ready: str
) -> None:
    client = new_client(app)
    availability_response = client.get(
        f"/api/public/businesses/{people.a}/services/{ready}/availability",
        params={"from": DAY.isoformat(), "to": DAY.isoformat()},
    )
    assert availability_response.status_code == 200
    slot = availability_response.json()["slots"][0]

    response = post_booking(client, people.a, ready, starts_at=slot)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "status",
        "starts_at",
        "ends_at",
        "duration_minutes",
        "service_name",
        "price",
        "worker_id",
        "worker_display_name",
        "cancellation_policy_text",
    }
    assert body["status"] == "pending"
    assert response.headers["cache-control"] == "no-store"

    second = client.get(
        f"/api/public/businesses/{people.a}/services/{ready}/availability",
        params={"from": DAY.isoformat(), "to": DAY.isoformat()},
    )
    assert slot not in second.json()["slots"]


# 15: GUARD.
def test_auto_confirm_makes_the_booking_confirmed_with_no_expiry(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    assert put_settings(owner, {"auto_confirm": True}).status_code == 200

    response = post_booking(new_client(app), people.a, ready, starts_at=at("09:00"))

    assert response.status_code == 201
    assert response.json()["status"] == "confirmed"
    with tenant_context(people.a) as session:
        row = session.execute(
            text("SELECT expires_at, auto_confirm_at_booking FROM bookings WHERE id = :id"),
            {"id": response.json()["id"]},
        ).one()
    assert row.expires_at is None
    assert row.auto_confirm_at_booking is True


# 16: FENCE - ruling 3. Wrong impl: delete step 12 (the member_slots re-derivation).
def test_a_start_inside_a_buffer_tail_is_refused(
    people: People, app: FastAPI, owner: TestClient
) -> None:
    service_id = new_service(owner, duration_minutes=30)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    save_setting(people.a, "buffer_pct", 20)
    client = new_client(app)
    first = post_booking(client, people.a, service_id, starts_at=at("10:00"))
    assert first.status_code == 201

    response = post_booking(
        client, people.a, service_id, email=fresh_email(), starts_at=at("10:30")
    )

    assert (response.status_code, response.json()) == (409, {"code": "slot_unavailable"})


# 17: FENCE (same wrong impl as 16). Split into three tests: each seeds its own worker's hours
# once, since working_hours' own exclusion constraint refuses a second overlapping seed of the
# same member (a fresh `people` fixture per test keeps them from interfering with each other).
def test_an_off_grid_start_is_refused(people: People, app: FastAPI, owner: TestClient) -> None:
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))

    response = post_booking(new_client(app), people.a, service_id, starts_at=at("09:07"))

    assert (response.status_code, response.json()) == (409, {"code": "slot_unavailable"})


def test_a_start_before_the_opening_envelope_is_refused(
    people: People, app: FastAPI, owner: TestClient
) -> None:
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    seed_opening(people.a, [(w, "10:00", "17:00") for w in range(1, 8)])

    response = post_booking(new_client(app), people.a, service_id, starts_at=at("09:00"))

    assert (response.status_code, response.json()) == (409, {"code": "slot_unavailable"})


def test_a_start_after_the_workers_own_hours_is_refused(
    people: People, app: FastAPI, owner: TestClient
) -> None:
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "10:00"))
    assign(people.a, service_id, member_id(people.a, people.both))

    response = post_booking(new_client(app), people.a, service_id, starts_at=at("11:00"))

    assert (response.status_code, response.json()) == (409, {"code": "slot_unavailable"})


# 18: FENCE (same wrong impl as 16). Per SS14.0 rules 3-5: never a past time for min_notice.
def test_a_start_in_the_past_or_inside_min_notice_or_beyond_the_horizon_is_refused(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    client = new_client(app)

    past = post_booking(client, people.a, ready, starts_at=at("09:00", TODAY - timedelta(days=1)))
    assert (past.status_code, past.json()) == (409, {"code": "slot_unavailable"})

    assert put_settings(owner, {"min_notice_minutes": 10080}).status_code == 200  # seven days
    notice = post_booking(client, people.a, ready, email=fresh_email(), starts_at=at("09:00"))
    assert (notice.status_code, notice.json()) == (409, {"code": "slot_unavailable"})
    assert put_settings(owner, {"min_notice_minutes": 60}).status_code == 200

    assert put_settings(owner, {"booking_horizon_days": 1}).status_code == 200
    horizon = post_booking(client, people.a, ready, email=fresh_email(), starts_at=at("09:00"))
    assert (horizon.status_code, horizon.json()) == (409, {"code": "slot_unavailable"})


# 18b: FENCE, made falsifiable. Wrong impl: pass availability.now() instead of row.now.
def test_the_write_path_takes_its_clock_from_postgres(
    people: People, app: FastAPI, owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("00:00", "23:45"))
    assign(people.a, service_id, member_id(people.a, people.both))
    save_setting(people.a, "min_notice_minutes", 60)
    real_now = datetime.now(UTC)
    monkeypatch.setattr(availability, "now", lambda: real_now + timedelta(hours=2))
    local_now = real_now.astimezone(ZoneInfo(ZONE))
    # 90 minutes out: clears the REAL 60-minute notice, but is still inside the SKEWED clock's
    # 60-minute notice (skewed now is +2h, so its horizon is +3h). Only an implementation that
    # uses availability.now() instead of Postgres's row.now refuses this.
    start = local_now + timedelta(minutes=90)
    start = start.replace(minute=(start.minute // 15) * 15, second=0, microsecond=0)
    if start.date() != local_now.date():
        pytest.skip("too close to local midnight for this run to land on one calendar day")
    if start <= local_now + timedelta(minutes=60):
        start += timedelta(minutes=15)

    response = post_booking(
        new_client(app),
        people.a,
        service_id,
        starts_at=start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    assert response.status_code == 201


# 18c: FENCE - B1.
@pytest.mark.parametrize("instant", ["0001-01-01T00:00:00+00:00", "9999-12-31T23:59:59+00:00"])
def test_a_year_one_or_year_9999_start_is_a_422_not_a_500(
    people: People, app: FastAPI, ready: str, instant: str
) -> None:
    response = post_booking(new_client(app), people.a, ready, starts_at=instant)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})


# 18d: FENCE - B1b.
def test_a_string_start_and_a_string_member_id_are_accepted(
    people: People, app: FastAPI, ready: str
) -> None:
    worker = str(member_id(people.a, people.both))

    response = post_booking(
        new_client(app), people.a, ready, starts_at=at("09:00"), member_id=worker
    )

    assert response.status_code == 201


# 19: FENCE.
def test_a_worker_not_assigned_to_the_service_is_refused_without_probing(
    people: People, app: FastAPI, ready: str
) -> None:
    client = new_client(app)
    other_business_member = member_id(people.b, people.only_b)
    unassigned_member = member_id(people.a, people.only_a)
    for candidate in (other_business_member, unassigned_member, uuid.uuid4()):
        response = post_booking(
            client,
            people.a,
            ready,
            email=fresh_email(),
            starts_at=at("09:00"),
            member_id=str(candidate),
        )
        assert (response.status_code, response.json()) == (409, {"code": "slot_unavailable"})


# 20: GUARD, demoted from FENCE (F-c/F-d). The lock's proof is rows 50 and 51, not this one.
def test_three_concurrent_bookers_and_three_free_workers_all_get_a_booking(
    people: People, app: FastAPI, owner: TestClient, app_engine: Engine, migrate_engine: Engine
) -> None:
    third_user = add_user(app_engine)
    add_membership(people.a, third_user)
    try:
        service_id = new_service(owner)
        third_worker = member_id(people.a, third_user)
        workers = [member_id(people.a, u) for u in (people.only_a, people.both, third_user)]
        for user in (people.only_a, people.both, third_user):
            seed(people.a, user, weekdays("09:00", "17:00"))
        assign(people.a, service_id, *workers)
        barrier = threading.Barrier(3)
        results: list[Response] = []
        lock = threading.Lock()

        def book_one() -> None:
            barrier.wait(timeout=10)
            response = post_booking(
                new_client(app), people.a, service_id, email=fresh_email(), starts_at=at("09:00")
            )
            with lock:
                results.append(response)

        threads = [threading.Thread(target=book_one) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not any(thread.is_alive() for thread in threads)

        assert [r.status_code for r in results] == [201, 201, 201]
        booked_workers = {r.json()["worker_id"] for r in results}
        assert booked_workers == {str(w) for w in workers}
        assert third_worker in workers
    finally:
        # Bookings reference memberships with no ON DELETE (migration 0026): clear them first, as
        # the migrate role, exactly as the `people` fixture's own teardown does.
        with migrate_engine.begin() as conn:
            delete_bookings(conn, (people.a,))
        with tenant_context(people.a) as session:
            session.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": third_user})
        with migrate_engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": third_user})


# 21: GUARD, code changed. The AC's surviving half.
def test_two_concurrent_bookers_and_one_worker_give_one_201_and_one_clean_409(
    people: People, app: FastAPI, ready: str
) -> None:
    barrier = threading.Barrier(2)
    results: list[Response] = []
    lock = threading.Lock()

    def book_one() -> None:
        barrier.wait(timeout=10)
        response = post_booking(
            new_client(app), people.a, ready, email=fresh_email(), starts_at=at("09:00")
        )
        with lock:
            results.append(response)

    threads = [threading.Thread(target=book_one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)

    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409]
    losing = next(r for r in results if r.status_code == 409)
    assert losing.json() == {"code": "slot_unavailable"}
    with tenant_context(people.a) as session:
        count = session.scalar(text("SELECT count(*) FROM bookings"))
    assert count == 1


# 21c: FENCE - the AC's other half, part 2 (test_bookings_db's 21b is part 1).
def test_a_conflicting_row_committed_mid_request_is_a_409_slot_taken(
    people: People, app: FastAPI, ready: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = member_id(people.a, people.both)
    real_booked = availability.booked
    paused_once = threading.Event()
    paused = threading.Event()
    release = threading.Event()

    def paused_booked(db: Any, members_: Any, start: Any, end: Any) -> Any:
        result = real_booked(db, members_, start, end)
        if not paused_once.is_set():
            paused_once.set()
            paused.set()
            release.wait(timeout=10)
        return result

    monkeypatch.setattr(availability, "booked", paused_booked)
    results: dict[str, Response] = {}

    def request_thread() -> None:
        results["response"] = post_booking(new_client(app), people.a, ready, starts_at=at("09:00"))

    thread = threading.Thread(target=request_thread)
    thread.start()
    assert paused.wait(timeout=10)

    # A lock-bypassing writer: commits a conflicting booking directly, no advisory lock.
    with tenant_context(people.a) as session:
        client_id = session.scalar(
            text(
                "INSERT INTO clients (tenant_id, name) "
                "VALUES (current_setting('app.tenant_id')::uuid, 'Direct') RETURNING id"
            )
        )
        service_row = session.execute(
            text(
                "SELECT name, price_amount_minor, price_currency, duration_minutes "
                "FROM services WHERE id = :id"
            ),
            {"id": ready},
        ).one()
        starts_at = datetime.fromisoformat(at("09:00").replace("Z", "+00:00"))
        session.execute(
            text("""
            INSERT INTO bookings (tenant_id, client_id, worker_id, service_id, starts_at, ends_at,
                status, source, service_name, price_amount_minor, price_currency,
                duration_minutes, auto_confirm_at_booking)
            VALUES (current_setting('app.tenant_id')::uuid, :client_id, :worker_id, :service_id,
                    :starts_at, :ends_at, 'confirmed', 'merchant', CAST(:service_name AS jsonb),
                    :price_amount_minor, :price_currency, :duration_minutes, true)
            """),
            {
                "client_id": client_id,
                "worker_id": worker,
                "service_id": ready,
                "starts_at": starts_at,
                "ends_at": starts_at + timedelta(minutes=service_row.duration_minutes),
                "service_name": json.dumps(dict(service_row.name)),
                "price_amount_minor": service_row.price_amount_minor,
                "price_currency": service_row.price_currency,
                "duration_minutes": service_row.duration_minutes,
            },
        )
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    response = results["response"]
    assert (response.status_code, response.json()) == (409, {"code": "slot_taken"})


# 22: GUARD, demoted honestly (see spec SS14.3 for the invariant this leaves unfenced).
def test_the_pending_cap_holds_under_two_concurrent_bookings_of_one_address(
    people: People, app: FastAPI, owner: TestClient
) -> None:
    put_settings(owner, {"max_pending_per_email": 1})
    service_a = new_service(owner)
    service_b = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_a, member_id(people.a, people.both))
    assign(people.a, service_b, member_id(people.a, people.both))
    email = fresh_email()
    barrier = threading.Barrier(2)
    results: list[Response] = []
    lock = threading.Lock()

    def book_one(service_id: str, clock_time: str) -> None:
        barrier.wait(timeout=10)
        response = post_booking(
            new_client(app), people.a, service_id, email=email, starts_at=at(clock_time)
        )
        with lock:
            results.append(response)

    threads = [
        threading.Thread(target=book_one, args=(service_a, "09:00")),
        threading.Thread(target=book_one, args=(service_b, "11:00")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)

    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 429]
    with tenant_context(people.a) as session:
        client_id = session.scalar(text("SELECT id FROM clients WHERE email = :e"), {"e": email})
        live = session.scalar(
            text("""
            SELECT count(*) FROM bookings WHERE client_id = :c
              AND status = ANY(CAST(:expiring AS text[])) AND expires_at > now()
            """),
            {"c": client_id, "expiring": list(availability.EXPIRING)},
        )
    assert live == 1


# 23: FENCE.
def test_max_pending_per_email_answers_the_same_code_as_the_rate_limit(
    people: People, app: FastAPI, owner: TestClient
) -> None:
    put_settings(owner, {"max_pending_per_email": 1})
    service_a = new_service(owner)
    service_b = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_a, member_id(people.a, people.both))
    assign(people.a, service_b, member_id(people.a, people.both))
    client = new_client(app)
    email = fresh_email()
    assert (
        post_booking(client, people.a, service_a, email=email, starts_at=at("09:00")).status_code
        == 201
    )

    response = post_booking(client, people.a, service_b, email=email, starts_at=at("11:00"))

    assert (response.status_code, response.json()) == (429, {"code": "rate_limited"})


# 24: FENCE - ruling 8.
def test_an_unauthenticated_booking_cannot_rewrite_a_clients_name_or_phone(
    people: People, app: FastAPI, ready: str
) -> None:
    email = fresh_email()
    with tenant_context(people.a) as session:
        session.execute(
            text("""
            INSERT INTO clients (tenant_id, name, email, phone)
            VALUES (current_setting('app.tenant_id')::uuid, 'Victim', :email, '+31600000000')
            """),
            {"email": email},
        )

    response = post_booking(
        new_client(app),
        people.a,
        ready,
        starts_at=at("09:00"),
        name="Attacker",
        email=email,
        phone="+31611111111",
    )

    assert response.status_code == 201
    with tenant_context(people.a) as session:
        row = session.execute(
            text("SELECT name, phone, user_id FROM clients WHERE email = :e"), {"e": email}
        ).one()
    assert (row.name, row.phone, row.user_id) == ("Victim", "+31600000000", None)

    blank_email = fresh_email()
    with tenant_context(people.a) as session:
        session.execute(
            text("""
            INSERT INTO clients (tenant_id, name, email)
            VALUES (current_setting('app.tenant_id')::uuid, 'Blank', :email)
            """),
            {"email": blank_email},
        )
    filled = post_booking(
        new_client(app),
        people.a,
        ready,
        starts_at=at("11:00"),
        name="Anyone",
        email=blank_email,
        phone="+31699999999",
    )
    assert filled.status_code == 201
    with tenant_context(people.a) as session:
        blank_row = session.execute(
            text("SELECT phone FROM clients WHERE email = :e"), {"e": blank_email}
        ).one()
    assert blank_row.phone == "+31699999999"


# 25: FENCE.
def test_a_signed_in_stranger_is_anonymous_for_the_refresh(
    people: People, app: FastAPI, ready: str
) -> None:
    stranger_email = fresh_email()
    with tenant_context(people.a) as session:
        session.execute(
            text("""
            INSERT INTO clients (tenant_id, name, email, phone)
            VALUES (current_setting('app.tenant_id')::uuid, 'Victim', :email, '+31600000000')
            """),
            {"email": stranger_email},
        )
    signed = signed_in(app, people.a, people.both)

    response = post_booking(
        signed,
        people.a,
        ready,
        starts_at=at("09:00"),
        name="Impersonator",
        email=stranger_email,
        phone="+31611111111",
    )

    assert response.status_code == 201
    with tenant_context(people.a) as session:
        row = session.execute(
            text("SELECT name, phone, user_id FROM clients WHERE email = :e"), {"e": stranger_email}
        ).one()
    assert (row.name, row.phone, row.user_id) == ("Victim", "+31600000000", None)


# 26a: GUARD + FENCE.
def test_the_address_s_own_account_refreshes_and_links_a_new_client_row(
    people: People, app: FastAPI, ready: str
) -> None:
    signed = signed_in(app, people.a, people.both)
    own_email = email_of(people.both)

    response = post_booking(
        signed, people.a, ready, starts_at=at("09:00"), name="Self Typed", email=own_email
    )

    assert response.status_code == 201
    with tenant_context(people.a) as session:
        row = session.execute(
            text("SELECT name, user_id FROM clients WHERE email = :e"), {"e": own_email}
        ).one()
    assert row.name == "Self Typed"
    assert row.user_id == people.both


# 26b: GUARD - D3's ruling.
def test_an_existing_client_row_is_refreshed_but_never_claimed(
    people: People, app: FastAPI, ready: str
) -> None:
    own_email = email_of(people.both)
    with tenant_context(people.a) as session:
        session.execute(
            text("""
            INSERT INTO clients (tenant_id, name, email)
            VALUES (current_setting('app.tenant_id')::uuid, 'Old Name', :email)
            """),
            {"email": own_email},
        )
    signed = signed_in(app, people.a, people.both)

    response = post_booking(
        signed,
        people.a,
        ready,
        starts_at=at("09:00"),
        name="New Name",
        email=own_email,
        phone="+31611111111",
    )

    assert response.status_code == 201
    with tenant_context(people.a) as session:
        row = session.execute(
            text("SELECT name, phone, user_id FROM clients WHERE email = :e"), {"e": own_email}
        ).one()
    assert (row.name, row.phone) == ("New Name", "+31611111111")
    assert row.user_id is None


# 27: FENCE - archived-service race. Wrong impl: drop FOR SHARE from step 2.
def test_an_archived_service_loses_a_booking_that_read_it_first(
    people: People, app: FastAPI, ready: str, app_engine: Engine
) -> None:
    holding = threading.Event()
    release = threading.Event()

    def archiver() -> None:
        with app_engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
            conn.execute(
                text("UPDATE services SET archived_at = now() WHERE id = :id"), {"id": ready}
            )
            holding.set()
            release.wait(timeout=10)
            conn.commit()

    archiver_thread = threading.Thread(target=archiver)
    archiver_thread.start()
    assert holding.wait(timeout=10)
    results: dict[str, Response] = {}

    def request_thread() -> None:
        results["response"] = post_booking(new_client(app), people.a, ready, starts_at=at("09:00"))

    thread = threading.Thread(target=request_thread)
    thread.start()
    wait_until_blocked(app_engine, 1)
    release.set()
    archiver_thread.join(timeout=10)
    thread.join(timeout=10)
    assert not archiver_thread.is_alive() and not thread.is_alive()

    response = results["response"]
    assert (response.status_code, response.json()) == (404, {"code": "not_found"})
    with tenant_context(people.a) as session:
        count = session.scalar(
            text("SELECT count(*) FROM bookings WHERE service_id = :id"), {"id": ready}
        )
    assert count == 0


# 28: FENCE - ruling 13.
def test_the_rate_limits_run_in_order_and_as_two_calls(
    people: People, app: FastAPI, ready: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turnstile, "verify", lambda token, ip: False)
    client = new_client(app, fresh_address())
    email = fresh_email()

    for _ in range(6):
        response = post_booking(client, people.a, ready, email=email, starts_at=at("09:00"))
        assert response.status_code == 403

    monkeypatch.setattr(turnstile, "verify", lambda token, ip: True)
    seventh = post_booking(client, people.a, ready, email=email, starts_at=at("09:00"))

    assert seventh.status_code == 201


# 29: GUARD.
def test_thirty_bookings_an_hour_per_address_and_five_per_address_and_business(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    set_role(people.b, people.only_b, "owner")  # F-j: `b` has no owner otherwise
    b_owner = signed_in(app, people.b, people.only_b)
    b_service = new_service(b_owner)
    seed(people.b, people.only_b, weekdays("09:00", "17:00"))
    assign(people.b, b_service, member_id(people.b, people.only_b))

    address = fresh_address()
    per_address_client = new_client(app, address)
    for _ in range(30):
        response = post_booking(
            per_address_client, people.a, ready, email=fresh_email(), starts_at=at("09:00")
        )
        assert response.status_code != 429
    over = post_booking(
        per_address_client, people.a, ready, email=fresh_email(), starts_at=at("09:00")
    )
    assert (over.status_code, over.json()) == (429, {"code": "rate_limited"})
    assert (
        post_booking(
            new_client(app), people.a, ready, email=fresh_email(), starts_at=at("09:00")
        ).status_code
        != 429
    )

    email = fresh_email()
    per_email_client = new_client(app)
    for _ in range(5):
        response = post_booking(
            per_email_client, people.a, ready, email=email, starts_at=at("09:00")
        )
        assert response.status_code != 429
    sixth = post_booking(per_email_client, people.a, ready, email=email, starts_at=at("09:00"))
    assert (sixth.status_code, sixth.json()) == (429, {"code": "rate_limited"})
    same_email_business_b = post_booking(
        new_client(app), people.b, b_service, email=email, starts_at=at("09:00")
    )
    assert same_email_business_b.status_code == 201


# 29b: GUARD, adopted from the reviewer's deviation.
def test_the_per_ip_limit_trips_on_its_own(people: People, app: FastAPI, ready: str) -> None:
    address = fresh_address()
    client = new_client(app, address)
    for _ in range(30):
        response = post_booking(client, people.a, ready, email=fresh_email(), starts_at=at("09:00"))
        assert response.status_code != 429
    over = post_booking(client, people.a, ready, email=fresh_email(), starts_at=at("09:00"))
    assert (over.status_code, over.json()) == (429, {"code": "rate_limited"})
    fresh = post_booking(
        new_client(app), people.a, ready, email=fresh_email(), starts_at=at("09:00")
    )
    assert fresh.status_code != 429


# 30: FENCE.
def test_the_booking_post_writes_no_audit_event(
    people: People, app: FastAPI, ready: str, migrate_engine: Engine
) -> None:
    # `ready` itself records a `service_created` event: compare before/after, not against [].
    before = events(migrate_engine, tenant_id=people.a)

    response = post_booking(new_client(app), people.a, ready, starts_at=at("09:00"))

    assert response.status_code == 201
    assert events(migrate_engine, tenant_id=people.a) == before


# 31: GUARD.
def test_the_creation_event_carries_the_address_the_browser_and_the_consent_seam(
    people: People, app: FastAPI, ready: str
) -> None:
    client = new_client(app)
    client.headers.update({"User-Agent": "zif-booking-test/1"})

    response = post_booking(
        client,
        people.a,
        ready,
        starts_at=at("09:00"),
        consents={"marketing_email": True, "sms": False},
    )

    assert response.status_code == 201
    with tenant_context(people.a) as session:
        row = session.execute(
            text("""
            SELECT event, host(ip) AS ip, user_agent, policy_version, consent_purposes
            FROM booking_events WHERE booking_id = :id
            """),
            {"id": response.json()["id"]},
        ).one()
    assert row.event == "created"
    assert row.user_agent == "zif-booking-test/1"
    assert row.policy_version == "2026-09-01"
    assert row.consent_purposes == {"marketing_email": True, "sms": False}


# 32: FENCE - ruling 18.
def test_a_withdrawal_is_recorded_immediately_and_a_grant_is_not(
    people: People, app: FastAPI, ready: str
) -> None:
    response = post_booking(
        new_client(app),
        people.a,
        ready,
        starts_at=at("09:00"),
        consents={"marketing_email": False, "sms": True},
    )

    assert response.status_code == 201
    with tenant_context(people.a) as session:
        client_id = session.execute(
            text("SELECT client_id FROM bookings WHERE id = :id"), {"id": response.json()["id"]}
        ).scalar_one()
        rows = session.execute(
            text("SELECT purpose, granted, source FROM consents WHERE client_id = :c"),
            {"c": client_id},
        ).all()
    assert [(r.purpose, r.granted, r.source) for r in rows] == [
        ("marketing_email", False, "booking_page")
    ]


# 33: GUARD.
def test_an_unknown_policy_version_is_a_422_and_writes_nothing(
    people: People, app: FastAPI, ready: str
) -> None:
    email = fresh_email()

    response = post_booking(
        new_client(app),
        people.a,
        ready,
        starts_at=at("09:00"),
        email=email,
        policy_version="1999-01-01",
    )

    assert (response.status_code, response.json()) == (422, {"code": "unknown_policy_version"})
    with tenant_context(people.a) as session:
        booking_count = session.scalar(text("SELECT count(*) FROM bookings"))
        client_count = session.scalar(
            text("SELECT count(*) FROM clients WHERE email = :e"), {"e": email}
        )
    assert (booking_count, client_count) == (0, 0)


# 34: FENCE - ruling 9.
def test_the_snapshot_is_the_service_as_it_was_and_the_buffer_is_not_snapshotted(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    client = new_client(app)
    response = post_booking(client, people.a, ready, starts_at=at("09:00"))
    assert response.status_code == 201
    booking_id = response.json()["id"]

    patched = owner.patch(
        f"/api/services/{ready}",
        json={
            "name": {"en": "Renamed"},
            "price": {"amount_minor": 9999},
            "duration_minutes": 45,
            "buffer_minutes": 30,
        },
    )
    assert patched.status_code == 200

    with tenant_context(people.a) as session:
        row = session.execute(
            text(
                "SELECT service_name, price_amount_minor, duration_minutes FROM bookings "
                "WHERE id = :id"
            ),
            {"id": booking_id},
        ).one()
    assert row.service_name != {"en": "Renamed"}
    assert row.price_amount_minor != 9999
    assert row.duration_minutes != 45

    slots = client.get(
        f"/api/public/businesses/{people.a}/services/{ready}/availability",
        params={"from": DAY.isoformat(), "to": DAY.isoformat()},
    ).json()["slots"]
    assert at("09:30") not in slots  # blocked by the NEW 30-minute buffer, applied live
    assert at("10:00") in slots


# 35: FENCE. Wrong impl: add client_name/client_email columns to 0026 and write them.
def test_no_client_personal_data_reaches_the_booking_row(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        columns = set(
            conn.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'bookings'"
                )
            )
        )
    assert columns.isdisjoint({"name", "email", "phone", "locale", "client_name", "client_email"})


# 36: FENCE. Wrong impl: echo Found.phone into BookingOut.
def test_the_booking_answer_carries_no_contact_details(
    people: People, app: FastAPI, ready: str
) -> None:
    phone = "+31699998888"
    email = fresh_email()
    with tenant_context(people.a) as session:
        session.execute(
            text("""
            INSERT INTO clients (tenant_id, name, email, phone)
            VALUES (current_setting('app.tenant_id')::uuid, 'Has Phone', :email, :phone)
            """),
            {"email": email, "phone": phone},
        )

    response = post_booking(new_client(app), people.a, ready, starts_at=at("09:00"), email=email)

    assert response.status_code == 201
    assert phone not in response.text


# 42b: FENCE - B5.
def test_a_booking_racing_a_member_removal_is_a_409_never_a_500(
    people: People,
    app: FastAPI,
    owner: TestClient,
    app_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = member_id(people.a, people.only_a)
    service_id = new_service(owner)
    seed(people.a, people.only_a, weekdays("09:00", "17:00"))
    assign(people.a, service_id, worker)
    holding = threading.Event()
    release = threading.Event()
    real_guarded = members.guarded

    def paused_guarded(
        current: Any, statement: str, target_member: uuid.UUID, **values: object
    ) -> None:
        holding.set()
        release.wait(timeout=10)
        real_guarded(current, statement, target_member, **values)

    monkeypatch.setattr(members, "guarded", paused_guarded)
    remove_results: dict[str, Response] = {}

    def remover() -> None:
        remove_results["response"] = owner.request(
            "DELETE",
            f"/api/members/{worker}",
            headers={"content-type": "application/json"},
            json={},
        )

    remover_thread = threading.Thread(target=remover)
    remover_thread.start()
    assert holding.wait(timeout=10)
    booking_results: dict[str, Response] = {}

    def booker() -> None:
        booking_results["response"] = post_booking(
            new_client(app), people.a, service_id, starts_at=at("09:00")
        )

    booker_thread = threading.Thread(target=booker)
    booker_thread.start()
    wait_until_blocked(app_engine, 1)
    release.set()
    remover_thread.join(timeout=10)
    booker_thread.join(timeout=10)
    assert not remover_thread.is_alive() and not booker_thread.is_alive()

    assert remove_results["response"].status_code == 204
    booking_response = booking_results["response"]
    assert (booking_response.status_code, booking_response.json()) == (409, {"code": "slot_taken"})


# 42c: FENCE - B6.
def test_a_probe_with_an_unbookable_slot_cannot_tell_whether_an_address_books_here(
    people: People, app: FastAPI, owner: TestClient
) -> None:
    put_settings(owner, {"max_pending_per_email": 1})
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    holder_email = fresh_email()
    holder_response = post_booking(
        new_client(app), people.a, service_id, email=holder_email, starts_at=at("09:00")
    )
    assert holder_response.status_code == 201
    off_grid = at("09:07")  # deliberately unbookable: off the 15-minute grid

    known = post_booking(
        new_client(app), people.a, service_id, email=holder_email, starts_at=off_grid
    )
    unknown = post_booking(
        new_client(app), people.a, service_id, email=fresh_email(), starts_at=off_grid
    )

    assert (known.status_code, known.json()) == (409, {"code": "slot_unavailable"})
    assert (unknown.status_code, unknown.json()) == (409, {"code": "slot_unavailable"})


# 50: FENCE, structural. One of the two fences that prove the lock (F-c).
def test_the_booking_transaction_takes_the_tenant_lock_first(
    people: People, app: FastAPI, ready: str, app_engine: Engine
) -> None:
    executed: list[tuple[str, Any]] = []

    def count(conn: object, cursor: object, statement: str, parameters: Any, *args: Any) -> None:
        executed.append((statement, parameters))

    event.listen(app_engine, "before_cursor_execute", count)
    try:
        response = post_booking(new_client(app), people.a, ready, starts_at=at("09:00"))
    finally:
        event.remove(app_engine, "before_cursor_execute", count)

    assert response.status_code == 201
    tenant_set = next(
        i for i, (s, _) in enumerate(executed) if s.startswith("SELECT set_config('app.tenant_id'")
    )
    lock_statement, lock_params = executed[tenant_set + 1]
    assert lock_statement.startswith("SELECT pg_advisory_xact_lock(")
    assert lock_params == {"key": bookings.LOCK_KEY}


# 51: FENCE, behavioural. The other fence that proves the lock, measured RED 5/5 (F-c).
def test_a_second_booker_waits_for_the_first_rather_than_deadlocking(
    people: People, app: FastAPI, ready: str, app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_booked = availability.booked
    paused_once = threading.Event()
    holding = threading.Event()
    release = threading.Event()

    def paused_booked(db: Any, members_: Any, start: Any, end: Any) -> Any:
        result = real_booked(db, members_, start, end)
        if not paused_once.is_set():
            paused_once.set()
            holding.set()
            release.wait(timeout=10)
        return result

    monkeypatch.setattr(availability, "booked", paused_booked)
    results: dict[str, Response] = {}

    def first() -> None:
        results["a"] = post_booking(new_client(app), people.a, ready, starts_at=at("09:00"))

    thread_a = threading.Thread(target=first)
    thread_a.start()
    assert holding.wait(timeout=10)

    def second() -> None:
        results["b"] = post_booking(
            new_client(app), people.a, ready, email=fresh_email(), starts_at=at("09:00")
        )

    thread_b = threading.Thread(target=second)
    thread_b.start()
    wait_until_blocked(app_engine, 1)
    release.set()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)
    assert not thread_a.is_alive() and not thread_b.is_alive()

    assert results["a"].status_code == 201
    assert (results["b"].status_code, results["b"].json()) == (409, {"code": "slot_unavailable"})


# 52: GUARD.
def test_an_operational_error_is_a_503_not_a_500(
    people: People, app: FastAPI, ready: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(db: object) -> Any:
        raise OperationalError("boom", {}, Exception("boom"))

    monkeypatch.setattr(schedule, "envelope", boom)

    response = post_booking(new_client(app), people.a, ready, starts_at=at("09:00"))

    assert (response.status_code, response.json()) == (503, {"code": "busy"})
