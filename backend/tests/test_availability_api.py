"""GET /api/public/businesses/{tenant_id}/services/{service_id}/availability (ZIF-48): public, no
session. The clock is Monday 2026-03-30 06:00Z (08:00 in Amsterdam, summer time)."""

import uuid
from datetime import UTC, date, datetime, time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session

from app import availability
from app.db import tenant_context
from app.main import create_app
from app.schedule import to_utc
from tests.conftest import (
    People,
    email_of,
    fresh_address,
    member_id,
    new_client,
    put_settings,
    save_setting,
    signed_in,
)
from tests.test_opening_hours import seed_opening
from tests.test_services import service
from tests.test_working_hours import seed

NOW = datetime(2026, 3, 30, 6, tzinfo=UTC)
MONDAY = "2026-03-30"
ZONE = "Europe/Amsterdam"


@pytest.fixture
def app(people: People, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(availability, "now", lambda: NOW)
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


def new_service(owner: TestClient, **overrides: Any) -> str:
    response = service(owner, **overrides)
    assert response.status_code == 201
    service_id: str = response.json()["id"]
    return service_id


def assign(tenant_id: uuid.UUID, service_id: str, *members: uuid.UUID) -> None:
    with tenant_context(tenant_id) as session:
        session.execute(
            text(
                "INSERT INTO service_workers (tenant_id, service_id, member_id) VALUES (:t, :s, :m)"
            ),
            [{"t": tenant_id, "s": service_id, "m": m} for m in members],
        )


def weekdays(start: str, end: str) -> list[tuple[int, str, str]]:
    return [(d, start, end) for d in range(1, 8)]


def local(day: str, at: str) -> str:
    """A local time in Amsterdam as the API writes it, in UTC."""
    instant = to_utc(date.fromisoformat(day), time.fromisoformat(at), ZONE)
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def get(
    client: TestClient,
    tenant_id: object,
    service_id: object,
    start: str = MONDAY,
    end: str = MONDAY,
    **params: Any,
) -> Response:
    return client.get(
        f"/api/public/businesses/{tenant_id}/services/{service_id}/availability",
        params={"from": start, "to": end, **params},
    )


@pytest.fixture
def ready(people: People, owner: TestClient) -> str:
    """A 30-minute service performed by the owner, who works 09:00-12:00 every day."""
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "12:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    return service_id


# 1. the happy path


def test_anyone_reads_a_service_s_free_slots_without_a_session(
    people: People, app: FastAPI, ready: str
) -> None:
    response = get(new_client(app), people.a, ready)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"timezone", "duration_minutes", "workers", "slots"}
    assert body["timezone"] == ZONE
    assert body["duration_minutes"] == 30
    assert body["workers"] == [{"id": str(member_id(people.a, people.both)), "display_name": None}]
    # From 09:00 (an hour's notice from 08:00) to 11:30, every 15 minutes.
    assert body["slots"][0] == "2026-03-30T07:00:00Z"
    assert body["slots"][-1] == "2026-03-30T09:30:00Z"
    assert len(body["slots"]) == 11
    assert email_of(people.both) not in response.text
    assert str(people.both) not in response.text
    assert response.headers["cache-control"] == "no-store"


# 2. time off


def test_time_off_blocks_slots_and_its_reason_stays_private(
    people: People, app: FastAPI, ready: str
) -> None:
    marker = f"dentist-{uuid.uuid4()}"
    with tenant_context(people.a) as session:
        session.execute(
            text("""
            INSERT INTO time_off (tenant_id, member_id, starts_at, ends_at, reason)
            VALUES (:t, :m, :s, :e, :r)
            """),
            {
                "t": people.a,
                "m": member_id(people.a, people.both),
                "s": local(MONDAY, "10:00"),
                "e": local(MONDAY, "10:30"),
                "r": marker,
            },
        )

    response = get(new_client(app), people.a, ready)

    assert response.status_code == 200
    assert marker not in response.text
    slots = response.json()["slots"]
    for blocked in ("09:45", "10:00", "10:15"):
        assert local(MONDAY, blocked) not in slots
    assert local(MONDAY, "09:30") in slots
    assert local(MONDAY, "10:30") in slots


# 3. who is listed


def test_only_assigned_workers_with_hours_are_listed(
    people: People, app: FastAPI, owner: TestClient
) -> None:
    both, only_a = member_id(people.a, people.both), member_id(people.a, people.only_a)
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "10:00"))
    assign(people.a, service_id, both, only_a)  # only_a has no hours

    response = get(new_client(app), people.a, service_id)

    assert response.json()["workers"] == [{"id": str(both), "display_name": None}]


def test_an_unassigned_worker_is_neither_listed_nor_bookable(
    people: People, app: FastAPI, owner: TestClient
) -> None:
    both, only_a = member_id(people.a, people.both), member_id(people.a, people.only_a)
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "10:00"))
    seed(people.a, people.only_a, weekdays("11:00", "12:00"))
    assign(people.a, service_id, both)
    client = new_client(app)

    anyone = get(client, people.a, service_id)
    unassigned = get(client, people.a, service_id, member_id=only_a)
    unknown = get(client, people.a, service_id, member_id=uuid.uuid4())

    assert anyone.json()["workers"] == [{"id": str(both), "display_name": None}]
    assert local(MONDAY, "11:00") not in anyone.json()["slots"]
    assert (unassigned.status_code, unassigned.json()["slots"]) == (200, [])
    assert (unknown.status_code, unknown.json()["slots"]) == (200, [])


def test_one_worker_or_anyone(people: People, app: FastAPI, owner: TestClient) -> None:
    both, only_a = member_id(people.a, people.both), member_id(people.a, people.only_a)
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "10:00"))
    seed(people.a, people.only_a, weekdays("11:00", "12:00"))
    assign(people.a, service_id, both, only_a)
    client = new_client(app)
    mornings = [local(MONDAY, t) for t in ("09:00", "09:15", "09:30")]
    lates = [local(MONDAY, t) for t in ("11:00", "11:15", "11:30")]

    assert get(client, people.a, service_id, member_id=both).json()["slots"] == mornings
    assert get(client, people.a, service_id, member_id=only_a).json()["slots"] == lates
    anyone = get(client, people.a, service_id).json()
    assert anyone["slots"] == mornings + lates
    assert anyone["workers"] == sorted(
        [
            {"id": str(both), "display_name": None},
            {"id": str(only_a), "display_name": None},
        ],
        key=lambda w: str(w["id"]),
    )


def test_anyone_lists_each_start_once(people: People, app: FastAPI, owner: TestClient) -> None:
    both, only_a = member_id(people.a, people.both), member_id(people.a, people.only_a)
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "10:00"))
    seed(people.a, people.only_a, weekdays("09:00", "10:00"))
    assign(people.a, service_id, both, only_a)

    slots = get(new_client(app), people.a, service_id).json()["slots"]

    assert slots == [local(MONDAY, t) for t in ("09:00", "09:15", "09:30")]


# 4. not found


def test_an_archived_service_is_not_found(people: People, app: FastAPI, owner: TestClient) -> None:
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "12:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    assert owner.patch(f"/api/services/{service_id}", json={"archived": True}).status_code == 200

    response = get(new_client(app), people.a, service_id)

    assert (response.status_code, response.json()) == (404, {"code": "not_found"})


def test_an_unknown_or_foreign_service_or_business_is_not_found(
    people: People, app: FastAPI, ready: str
) -> None:
    client = new_client(app)
    for tenant_id, service_id in (
        (people.a, uuid.uuid4()),
        (people.b, ready),
        (uuid.uuid4(), ready),
    ):
        response = get(client, tenant_id, service_id)
        assert (response.status_code, response.json()) == (404, {"code": "not_found"})


# 5. the range


@pytest.mark.parametrize(
    ("start", "end", "code"),
    [
        ("2026-03-30", "2026-03-29", "invalid_range"),
        ("2026-03-30", "2026-04-13", "invalid_range"),  # 15 days
        ("1789000000", "2026-03-30", "invalid_request"),
        ("2026-3-1", "2026-03-30", "invalid_request"),
        ("2026-03-01T00:00", "2026-03-30", "invalid_request"),
        ("2026-03-30", "", "invalid_request"),
    ],
)
def test_a_bad_range_is_refused(
    people: People, app: FastAPI, ready: str, start: str, end: str, code: str
) -> None:
    response = get(new_client(app), people.a, ready, start, end)

    assert (response.status_code, response.json()) == (422, {"code": code})


def test_a_bad_id_is_refused(people: People, app: FastAPI, ready: str) -> None:
    client = new_client(app)
    for response in (
        get(client, "nope", ready),
        get(client, people.a, "nope"),
        get(client, people.a, ready, member_id="nope"),
    ):
        assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})


def test_fourteen_days_is_the_longest_range(people: People, app: FastAPI, ready: str) -> None:
    response = get(new_client(app), people.a, ready, MONDAY, "2026-04-12")

    assert response.status_code == 200
    assert response.json()["slots"][-1] == local("2026-04-12", "11:30")


@pytest.mark.parametrize(
    ("start", "end"), [("0001-01-01", "0001-01-02"), ("9999-12-30", "9999-12-31")]
)
def test_dates_far_from_today_have_no_slots(
    people: People, app: FastAPI, ready: str, start: str, end: str
) -> None:
    response = get(new_client(app), people.a, ready, start, end)

    assert (response.status_code, response.json()["slots"]) == (200, [])


# 6. settings


def test_the_horizon_counts_days_after_today(people: People, app: FastAPI, ready: str) -> None:
    save_setting(people.a, "booking_horizon_days", 1)

    slots = get(new_client(app), people.a, ready, "2026-03-29", "2026-04-05").json()["slots"]

    assert {s[:10] for s in slots} == {"2026-03-30", "2026-03-31"}


def test_minimum_notice_moves_the_first_slot(people: People, app: FastAPI, ready: str) -> None:
    save_setting(people.a, "min_notice_minutes", 150)  # 08:00 + 2.5 hours

    slots = get(new_client(app), people.a, ready).json()["slots"]

    assert slots[0] == local(MONDAY, "10:30")


def test_the_step_sets_the_grid(people: People, app: FastAPI, ready: str) -> None:
    save_setting(people.a, "slot_step_minutes", 30)

    slots = get(new_client(app), people.a, ready).json()["slots"]

    assert slots == [
        local(MONDAY, t) for t in ("09:00", "09:30", "10:00", "10:30", "11:00", "11:30")
    ]


def patch_booked(
    monkeypatch: pytest.MonkeyPatch, bookings: list[tuple[uuid.UUID, str, str, int | None]]
) -> list[tuple[list[uuid.UUID], datetime, datetime]]:
    calls: list[tuple[list[uuid.UUID], datetime, datetime]] = []

    def fake(
        db: Session, members: list[uuid.UUID], start: datetime, end: datetime
    ) -> list[tuple[uuid.UUID, datetime, datetime, int | None]]:
        calls.append((members, start, end))
        return [
            (m, datetime.fromisoformat(s), datetime.fromisoformat(e), o) for m, s, e, o in bookings
        ]

    monkeypatch.setattr(availability, "booked", fake)
    return calls


def test_the_buffer_percentage_sets_the_gap_after_a_booking(
    people: People, app: FastAPI, owner: TestClient, ready: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    both = member_id(people.a, people.both)
    patch_booked(monkeypatch, [(both, local(MONDAY, "10:00"), local(MONDAY, "10:30"), None)])
    client = new_client(app)
    save_setting(people.a, "buffer_pct", 0)

    none = get(client, people.a, ready).json()["slots"]
    assert put_settings(owner, {"buffer_pct": 50}).status_code == 200
    half = get(client, people.a, ready).json()["slots"]

    assert local(MONDAY, "10:30") in none
    assert local(MONDAY, "10:30") not in half  # 15 minutes after a 30-minute booking
    assert local(MONDAY, "10:45") in half


# 7. the bookings seam


def test_a_booking_removes_its_slot_and_only_chosen_workers_are_asked(
    people: People, app: FastAPI, owner: TestClient, ready: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    both, only_a = member_id(people.a, people.both), member_id(people.a, people.only_a)
    seed(people.a, people.only_a, weekdays("09:00", "12:00"))
    assign(people.a, ready, only_a)
    calls = patch_booked(monkeypatch, [(both, local(MONDAY, "10:00"), local(MONDAY, "10:30"), 0)])

    response = get(new_client(app), people.a, ready, member_id=both)

    assert response.status_code == 200
    assert local(MONDAY, "10:00") not in response.json()["slots"]
    assert local(MONDAY, "10:30") in response.json()["slots"]
    # from - 1 day .. to + 2 days, local midnights.
    assert calls == [([both], local_dt("2026-03-29"), local_dt("2026-04-01"))]


def local_dt(day: str) -> datetime:
    return to_utc(date.fromisoformat(day), time(), ZONE)


# 8. a fixed number of queries


def test_a_long_range_runs_as_many_queries_as_a_short_one(
    people: People, app: FastAPI, ready: str, app_engine: Engine
) -> None:
    statements: list[str] = []

    def count(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(app_engine, "before_cursor_execute", count)
    try:
        client = new_client(app)
        short = get(client, people.a, ready)
        one_day = len(statements)
        long = get(client, people.a, ready, MONDAY, "2026-04-12")
        many_days = len(statements) - one_day
    finally:
        event.remove(app_engine, "before_cursor_execute", count)

    assert (short.status_code, long.status_code) == (200, 200)
    assert len(long.json()["slots"]) > len(short.json()["slots"])
    assert one_day == many_days
    assert sum("FROM time_off" in s for s in statements) == 2


# 9. the rate limit


def test_sixty_requests_a_minute_per_address(people: People, app: FastAPI, ready: str) -> None:
    client = new_client(app, fresh_address())

    for _ in range(60):
        assert get(client, people.a, ready).status_code == 200
    over = get(client, people.a, ready)

    assert (over.status_code, over.json()) == (429, {"code": "rate_limited"})
    assert get(new_client(app), people.a, ready).status_code == 200


# 10. the display name (ZIF-97)


def set_display_name(tenant_id: uuid.UUID, member: uuid.UUID, name: str | None) -> None:
    with tenant_context(tenant_id) as session:
        session.execute(
            text("UPDATE memberships SET display_name = :name WHERE id = :id"),
            {"id": member, "name": name},
        )


def test_the_public_page_shows_the_display_name_never_the_email(
    people: People, app: FastAPI, ready: str
) -> None:
    both = member_id(people.a, people.both)
    set_display_name(people.a, both, "Ada Lovelace")

    response = get(new_client(app), people.a, ready)

    assert response.status_code == 200
    assert response.json()["workers"] == [{"id": str(both), "display_name": "Ada Lovelace"}]
    assert email_of(people.both) not in response.text


def test_with_no_display_name_the_public_answer_is_null_and_carries_no_email(
    people: People, app: FastAPI, ready: str
) -> None:
    both = member_id(people.a, people.both)

    response = get(new_client(app), people.a, ready)

    assert response.status_code == 200
    body = response.json()
    assert body["workers"] == [{"id": str(both), "display_name": None}]
    assert email_of(people.both) not in response.text
    assert str(people.both) not in response.text


def test_the_display_name_comes_from_this_business_s_membership(
    people: People, app: FastAPI, ready: str
) -> None:
    # Not a test of the join's tenant pair: row-level security already scopes memberships, so
    # dropping `m.tenant_id = w.tenant_id` is unreachable from here. It kills keying the name by
    # user_id instead of membership id, and resolving it outside the request's tenant_context.
    both_in_a = member_id(people.a, people.both)
    both_in_b = member_id(people.b, people.both)
    set_display_name(people.b, both_in_b, "Wrong Business")

    response = get(new_client(app), people.a, ready)

    assert response.status_code == 200
    workers = response.json()["workers"]
    assert workers == [{"id": str(both_in_a), "display_name": None}]
    assert "Wrong Business" not in response.text


def test_the_display_name_costs_no_extra_query(
    people: People, app: FastAPI, ready: str, app_engine: Engine
) -> None:
    statements: list[str] = []

    def count(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(app_engine, "before_cursor_execute", count)
    try:
        response = get(new_client(app), people.a, ready)
    finally:
        event.remove(app_engine, "before_cursor_execute", count)

    assert response.status_code == 200
    naming_memberships = [s for s in statements if "memberships" in s]
    assert len(naming_memberships) == 1
    assert "working_hours" in naming_memberships[0]


# 11. the opening-hours envelope (ZIF-105)


def test_a_business_with_no_opening_hours_returns_exactly_todays_slots(
    people: People, app: FastAPI, ready: str
) -> None:
    # F1: the catastrophic inversion. No opening_hours rows must behave exactly as before ZIF-105.
    response = get(new_client(app), people.a, ready)

    assert response.status_code == 200
    body = response.json()
    assert body["slots"][0] == "2026-03-30T07:00:00Z"
    assert len(body["slots"]) == 11


def test_a_closed_weekday_sells_nothing_though_the_worker_still_has_hours(
    people: People, app: FastAPI, ready: str
) -> None:
    # F2: the other direction. Only Tuesday is open; the worker's Monday hours must not sell.
    seed_opening(people.a, [(2, "09:00", "12:00")])
    client = new_client(app)

    monday = get(client, people.a, ready, MONDAY, MONDAY)
    tuesday = get(client, people.a, ready, "2026-03-31", "2026-03-31")

    assert monday.json()["slots"] == []
    assert len(tuesday.json()["slots"]) == 11


def test_the_opening_hours_envelope_is_read_once_per_request(
    people: People, app: FastAPI, owner: TestClient, app_engine: Engine
) -> None:
    # F7: one statement, whatever the worker count or the day count.
    both, only_a = member_id(people.a, people.both), member_id(people.a, people.only_a)
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "12:00"))
    seed(people.a, people.only_a, weekdays("09:00", "12:00"))
    assign(people.a, service_id, both, only_a)
    seed_opening(people.a, [(1, "09:00", "17:00")])
    statements: list[str] = []

    def count(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(app_engine, "before_cursor_execute", count)
    try:
        response = get(new_client(app), people.a, service_id, MONDAY, "2026-04-12")
    finally:
        event.remove(app_engine, "before_cursor_execute", count)

    assert response.status_code == 200
    assert sum("opening_hours" in s for s in statements) == 1


def test_the_envelope_is_read_even_when_no_worker_is_assigned(
    people: People, app: FastAPI, owner: TestClient, app_engine: Engine
) -> None:
    # F21: the envelope read belongs outside the `if chosen and first <= last:` guard, so its
    # statement count never depends on whether anyone is assigned (which F7 would not notice).
    service_id = new_service(owner)  # nobody assigned: chosen is empty
    seed_opening(people.a, [(1, "09:00", "17:00")])
    statements: list[str] = []

    def count(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(app_engine, "before_cursor_execute", count)
    try:
        response = get(new_client(app), people.a, service_id)
    finally:
        event.remove(app_engine, "before_cursor_execute", count)

    assert response.status_code == 200
    assert response.json()["slots"] == []
    assert sum("opening_hours" in s for s in statements) == 1


def test_the_envelope_narrows_a_worker_who_starts_before_opening(
    people: People, app: FastAPI, ready: str
) -> None:
    # G7: the AC end to end, through the public route.
    seed_opening(people.a, [(1, "10:00", "12:00")])

    response = get(new_client(app), people.a, ready)

    slots = response.json()["slots"]
    assert slots[0] == local(MONDAY, "10:00")
    assert local(MONDAY, "09:00") not in slots
    assert local(MONDAY, "11:30") in slots


def test_a_worker_entirely_outside_the_envelope_is_still_listed_with_no_slots(
    people: People, app: FastAPI, ready: str
) -> None:
    # G8: accept-and-narrow's first visible cost, made executable. Narrowing the envelope never
    # edits working_hours, so a worker whose whole week now falls outside it keeps their hours and
    # stays in `workers` - they simply sell nothing.
    both = member_id(people.a, people.both)
    seed_opening(people.a, [(1, "13:00", "17:00")])

    response = get(new_client(app), people.a, ready)

    body = response.json()
    assert body["workers"] == [{"id": str(both), "display_name": None}]
    assert body["slots"] == []


def test_a_worker_rostered_past_closing_sells_nothing_after_closing(
    people: People, app: FastAPI, owner: TestClient
) -> None:
    # F23: the ticket's headline scenario, end to end. Shop open 09:00-12:00, worker rostered
    # 09:00-17:00: the last 30-minute slot starts at 11:30 and nothing sells at or after noon.
    # Without the end of the clip this is 09:00-16:30, 31 slots.
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    seed_opening(people.a, [(1, "09:00", "12:00")])

    slots = get(new_client(app), people.a, service_id).json()["slots"]

    assert slots[0] == local(MONDAY, "09:00")
    assert slots[-1] == local(MONDAY, "11:30")
    assert [s for s in slots if s >= local(MONDAY, "12:00")] == []
    assert len(slots) == 11
