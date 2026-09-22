"""PATCH /api/bookings/{booking_id} and GET /api/bookings/pending (ZIF-52): merchants accept or
decline pending bookings. See docs/specs/2026-09-22-zif-52-accept-decline.md for the properties
each fence protects."""

import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg.errors import ExclusionViolation
from sqlalchemy import Engine, event, text
from sqlalchemy.exc import IntegrityError

from app import bookings, members
from app.db import tenant_context
from tests.conftest import (
    People,
    add_membership,
    add_user,
    events,
    fresh_email,
    member_id,
    new_client,
    save_setting,
    signed_in,
    wait_until_blocked,
)
from tests.test_availability_api import assign, new_service, seed_booking, weekdays
from tests.test_bookings_api import at, post_booking
from tests.test_working_hours import seed

TODAY = date.today()
DAY = TODAY + timedelta(days=2)


@pytest.fixture
def app(people: People) -> FastAPI:
    from app.main import create_app

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


def transition_url(booking_id: object) -> str:
    return f"/api/bookings/{booking_id}"


def patch(client: TestClient, booking_id: object, status: str) -> Response:
    return client.patch(transition_url(booking_id), json={"status": status})


def make_pending(app: FastAPI, tenant_id: object, service_id: str, **overrides: Any) -> str:
    response = post_booking(new_client(app), tenant_id, service_id, **overrides)
    assert response.status_code == 201, response.json()
    assert response.json()["status"] == "pending"
    id_: str = response.json()["id"]
    return id_


def expire(tenant_id: uuid.UUID, booking_id: str) -> None:
    with tenant_context(tenant_id) as session:
        session.execute(
            text("UPDATE bookings SET expires_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": booking_id},
        )


def shift(tenant_id: uuid.UUID, booking_id: str, delta: timedelta) -> None:
    """Back-date a booking, keeping its duration. No route can produce a booking that has already
    started: make_pending books through the public POST, which will not offer a past slot. Moving
    BOTH ends together is load-bearing - the "started but not ended" leg needs ends_at ahead.

    Shifting two bookings for the same worker can collide them under ex_bookings_worker_overlap,
    so callers that move a pair move both by the SAME delta and keep their start times apart."""
    with tenant_context(tenant_id) as session:
        session.execute(
            text(
                "UPDATE bookings SET starts_at = starts_at + :d, ends_at = ends_at + :d "
                "WHERE id = :id"
            ),
            {"id": booking_id, "d": delta},
        )


PAST = timedelta(days=-3)  # well clear of the two-day-out DAY the public POST offers


def ago(starts_at: str, delta: timedelta) -> timedelta:
    """The shift that lands `starts_at` (as at() spelled it) exactly `delta` before now."""
    return datetime.now(UTC) - delta - datetime.fromisoformat(starts_at)


def confirmed_in_the_past(
    app: FastAPI,
    owner: TestClient,
    tenant_id: uuid.UUID,
    service_id: str,
    clock_time: str = "09:00",
) -> str:
    """A confirmed booking whose appointment has already started - the state completed and no_show
    both require, and the one no route can reach on its own."""
    booking_id = make_pending(app, tenant_id, service_id, starts_at=at(clock_time))
    assert patch(owner, booking_id, "confirmed").status_code == 200
    shift(tenant_id, booking_id, PAST)
    return booking_id


def status_of(tenant_id: uuid.UUID, booking_id: str) -> str:
    with tenant_context(tenant_id) as session:
        found: str = session.scalar(
            text("SELECT status FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    return found


# F1 - the transition transaction takes the tenant lock first.
# Kills: omits the advisory lock, and takes it after loading the booking.
def test_the_transition_takes_the_tenant_lock_first(
    people: People, app: FastAPI, owner: TestClient, ready: str, app_engine: Engine
) -> None:
    booking_id = make_pending(app, people.a, ready)
    executed: list[tuple[str, Any]] = []

    def record(conn: object, cursor: object, statement: str, parameters: Any, *args: Any) -> None:
        executed.append((statement, parameters))

    event.listen(app_engine, "before_cursor_execute", record)
    try:
        response = patch(owner, booking_id, "confirmed")
    finally:
        event.remove(app_engine, "before_cursor_execute", record)

    assert response.status_code == 200
    tenant_set = next(
        i for i, (s, _) in enumerate(executed) if s.startswith("SELECT set_config('app.tenant_id'")
    )
    lock_statement, lock_params = executed[tenant_set + 1]
    assert lock_statement.startswith("SELECT pg_advisory_xact_lock(")
    assert lock_params == {"key": 51}


# F2 - a TTL-passed pending can never be accepted.
# Kills: drops `AND expires_at > now()` from TRANSITION.
def test_a_ttl_passed_pending_cannot_be_accepted(
    people: People, app: FastAPI, owner: TestClient, ready: str, migrate_engine: Engine
) -> None:
    booking_id = make_pending(app, people.a, ready)
    expire(people.a, booking_id)

    response = patch(owner, booking_id, "confirmed")

    assert (response.status_code, response.json()) == (409, {"code": "invalid_transition"})
    assert status_of(people.a, booking_id) == "pending"
    with tenant_context(people.a) as session:
        rows = (
            session.execute(
                text("SELECT event FROM booking_events WHERE booking_id = :id"), {"id": booking_id}
            )
            .scalars()
            .all()
        )
    assert list(rows) == ["created"]
    assert events(migrate_engine, action="booking_confirmed", target=f"booking:{booking_id}") == []


def hold_the_tenant_lock(
    tenant_id: uuid.UUID, holding: threading.Event, release: threading.Event
) -> None:
    """Hold ZIF-51's tenant advisory lock from a second session until released, so a request that
    takes it as its first statement waits with its transaction - and its now() - already open."""
    with tenant_context(tenant_id) as session:
        session.execute(
            text("SELECT pg_advisory_xact_lock(51, hashtext(current_setting('app.tenant_id')))")
        )
        holding.set()
        release.wait(timeout=30)


def expire_in_a_second(tenant_id: uuid.UUID, booking_id: str) -> None:
    """Push expires_at to one second ahead of the DB clock NOW - after the waiting transaction
    opened, so its frozen now() is provably before the expiry - and return once wall clock has
    passed it, so a Python-clock comparison provably sees it expired."""
    with tenant_context(tenant_id) as session:
        session.execute(
            text("UPDATE bookings SET expires_at = now() + interval '1 second' WHERE id = :id"),
            {"id": booking_id},
        )
    for _ in range(100):
        with tenant_context(tenant_id) as session:
            if session.scalar(
                text("SELECT clock_timestamp() > expires_at FROM bookings WHERE id = :id"),
                {"id": booking_id},
            ):
                return
        time.sleep(0.05)
    raise AssertionError("the pending never passed its expiry")


# F3 - the expiry is compared against the DATABASE's clock, not the application process's.
# Behavioural, with no seam monkeypatched: the route's now() is transaction_timestamp(), frozen when
# the transaction opens, so a request waiting on the tenant advisory lock holds a clock behind wall
# clock. A pending whose expiry falls between that frozen now() and wall clock is therefore still
# live for the statement and already dead for Python.
# Kills: `expires_at > :now` bound to datetime.now(UTC) (or availability.now()) in the handler, and
# TRANSITION left dead with the handler running its own Python-clock UPDATE - both answer 409 here.
def test_the_expiry_check_uses_postgres_clock_not_python(
    people: People, app: FastAPI, owner: TestClient, ready: str, app_engine: Engine
) -> None:
    booking_id = make_pending(app, people.a, ready)
    merchant = signed_in(app, people.a, people.both)
    holding, release = threading.Event(), threading.Event()
    blocker = threading.Thread(target=hold_the_tenant_lock, args=(people.a, holding, release))
    blocker.start()
    result: dict[str, Response] = {}

    def confirm() -> None:
        result["response"] = patch(merchant, booking_id, "confirmed")

    assert holding.wait(timeout=10)
    patcher = threading.Thread(target=confirm)
    patcher.start()
    wait_until_blocked(app_engine, 1)  # its transaction - and its now() - is open and waiting
    expire_in_a_second(people.a, booking_id)  # ahead of that now(), behind wall clock
    release.set()
    for thread in (blocker, patcher):
        thread.join(timeout=15)
    assert not blocker.is_alive() and not patcher.is_alive()

    assert (result["response"].status_code, result["response"].json()) == (
        200,
        {"id": booking_id, "status": "confirmed"},
    )
    assert status_of(people.a, booking_id) == "confirmed"


# F4 - a settled booking cannot be transitioned again.
# Kills: drops `AND status = 'pending'` from TRANSITION.
def test_a_settled_booking_cannot_be_transitioned_again(
    people: People, app: FastAPI, owner: TestClient, ready: str, migrate_engine: Engine
) -> None:
    accepted = make_pending(app, people.a, ready)
    assert patch(owner, accepted, "confirmed").status_code == 200

    again = patch(owner, accepted, "confirmed")
    declined_after = patch(owner, accepted, "declined")

    assert (again.status_code, again.json()) == (409, {"code": "invalid_transition"})
    assert (declined_after.status_code, declined_after.json()) == (
        409,
        {"code": "invalid_transition"},
    )
    with tenant_context(people.a) as session:
        settled = (
            session.execute(
                text(
                    "SELECT event FROM booking_events WHERE booking_id = :id AND event <> 'created'"
                ),
                {"id": accepted},
            )
            .scalars()
            .all()
        )
    assert list(settled) == ["confirmed"]
    confirmed = events(migrate_engine, action="booking_confirmed", target=f"booking:{accepted}")
    assert len(confirmed) == 1


# F5 - the assigned worker may transition; another worker may not.
# Kills: CurrentOwner instead of CurrentSession + may_manage (worker A gets 403); drops the
# may_manage call (worker B gets 200).
def test_only_the_assigned_worker_or_an_owner_may_transition(
    people: People, app: FastAPI, owner: TestClient, ready: str, app_engine: Engine
) -> None:
    # people.both is an OWNER in tenant a (conftest.people), so it cannot stand in for a plain
    # assigned worker here: only_a is a genuine non-owner worker.
    worker_a_user = people.only_a
    worker_a = member_id(people.a, worker_a_user)
    worker_b_user = add_user(app_engine)
    add_membership(people.a, worker_b_user, "worker")
    service_id = new_service(owner)
    seed(people.a, worker_a_user, weekdays("09:00", "17:00"))
    seed(people.a, worker_b_user, weekdays("09:00", "17:00"))
    assign(people.a, service_id, worker_a)

    booking_1 = make_pending(
        app, people.a, service_id, member_id=str(worker_a), starts_at=at("09:00")
    )
    worker_a_client = signed_in(app, people.a, worker_a_user)
    assert patch(worker_a_client, booking_1, "confirmed").status_code == 200

    booking_2 = make_pending(
        app, people.a, service_id, member_id=str(worker_a), starts_at=at("10:00")
    )
    worker_b_client = signed_in(app, people.a, worker_b_user)
    response = patch(worker_b_client, booking_2, "confirmed")
    assert (response.status_code, response.json()) == (403, {"code": "owner_only"})
    assert status_of(people.a, booking_2) == "pending"

    booking_3 = make_pending(
        app, people.a, service_id, member_id=str(worker_a), starts_at=at("11:00")
    )
    assert patch(owner, booking_3, "confirmed").status_code == 200


# F6 - another business's booking is 404, not 403.
# Kills: authorizes before loading the booking.
def test_another_businesss_booking_is_404_not_403(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    other_worker = signed_in(app, people.b, people.only_b)  # already a worker in tenant b

    response = patch(other_worker, booking_id, "confirmed")

    assert (response.status_code, response.json()) == (404, {"code": "not_found"})
    assert status_of(people.a, booking_id) == "pending"


# F7 - the queue is live pendings, soonest first.
# Kills: drops `AND b.expires_at > now()`, orders by id/created_at instead of starts_at, or uses
# status = ANY(:expiring) instead of = 'pending'.
def test_the_queue_lists_only_live_pendings_soonest_first(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    late = make_pending(app, people.a, ready, starts_at=at("15:00"))
    early = make_pending(app, people.a, ready, starts_at=at("09:00"))
    mid = make_pending(app, people.a, ready, starts_at=at("12:00"))
    confirmed = make_pending(app, people.a, ready, starts_at=at("11:00"))
    assert patch(owner, confirmed, "confirmed").status_code == 200
    declined = make_pending(app, people.a, ready, starts_at=at("13:00"))
    assert patch(owner, declined, "declined").status_code == 200
    awaiting = make_pending(app, people.a, ready, starts_at=at("14:00"))
    with tenant_context(people.a) as session:
        session.execute(
            text("UPDATE bookings SET status = 'awaiting_payment' WHERE id = :id"),
            {"id": awaiting},
        )
    # Created and expired LAST: any post_booking call after this would flip it to 'expired' via
    # the create path's own EXPIRE step (app/bookings.py:264), masking the queue's own TTL filter.
    expired = make_pending(app, people.a, ready, starts_at=at("10:00"))
    expire(people.a, expired)

    response = owner.get("/api/bookings/pending")

    assert response.status_code == 200
    ids = [row["id"] for row in response.json()]
    assert ids == [early, mid, late]


# F8 - a worker's queue is only their own; an owner's is the whole business's.
# Kills: drops the `(:everyone OR b.worker_id = ...)` clause; scopes the owner the same way.
def test_a_workers_queue_is_only_their_own(
    people: People, app: FastAPI, owner: TestClient, ready: str, app_engine: Engine
) -> None:
    # people.both is an OWNER in tenant a (conftest.people): its GET already sees everything,
    # which would make "worker" and "owner" indistinguishable here. only_a is a genuine worker.
    worker_a_user = people.only_a
    worker_a = member_id(people.a, worker_a_user)
    worker_b_user = add_user(app_engine)
    add_membership(people.a, worker_b_user, "worker")
    worker_b = member_id(people.a, worker_b_user)
    service_id = new_service(owner)
    seed(people.a, worker_a_user, weekdays("09:00", "17:00"))
    seed(people.a, worker_b_user, weekdays("09:00", "17:00"))
    assign(people.a, service_id, worker_a, worker_b)

    booking_a = make_pending(
        app, people.a, service_id, starts_at=at("09:00"), member_id=str(worker_a)
    )
    booking_b = make_pending(
        app, people.a, service_id, starts_at=at("10:00"), member_id=str(worker_b)
    )

    worker_a_client = signed_in(app, people.a, worker_a_user)
    worker_a_queue = {row["id"] for row in worker_a_client.get("/api/bookings/pending").json()}
    owner_queue = {row["id"] for row in owner.get("/api/bookings/pending").json()}

    assert worker_a_queue == {booking_a}
    assert owner_queue == {booking_a, booking_b}


# F9 - pending_ttl_hours drives expires_at, not a constant.
# Kills: leaves PENDING_TTL in place at app/bookings.py:328.
def test_pending_ttl_hours_drives_expires_at(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    save_setting(people.a, "pending_ttl_hours", 1)

    booking_id = make_pending(app, people.a, ready)

    with tenant_context(people.a) as session:
        row = session.execute(
            text("SELECT expires_at, created_at FROM bookings WHERE id = :id"), {"id": booking_id}
        ).one()
    gap = row.expires_at - row.created_at
    assert timedelta(minutes=55) < gap < timedelta(minutes=65)


# F10 - every transition writes both logs, with the right values.
# Kills: writes the audit event but not the booking_events row (or the reverse); leaves
# INSERT_EVENT hardcoding 'created'.
def test_every_transition_writes_both_logs(
    people: People, app: FastAPI, owner: TestClient, ready: str, migrate_engine: Engine
) -> None:
    accepted = make_pending(app, people.a, ready, starts_at=at("09:00"))
    assert patch(owner, accepted, "confirmed").status_code == 200
    declined = make_pending(app, people.a, ready, starts_at=at("11:00"))
    assert patch(owner, declined, "declined").status_code == 200

    with tenant_context(people.a) as session:
        accepted_events = (
            session.execute(
                text(
                    "SELECT event, ip, user_agent, policy_version, consent_purposes "
                    "FROM booking_events WHERE booking_id = :id AND event <> 'created'"
                ),
                {"id": accepted},
            )
            .mappings()
            .all()
        )
        declined_events = (
            session.execute(
                text(
                    "SELECT event, ip, user_agent, policy_version, consent_purposes "
                    "FROM booking_events WHERE booking_id = :id AND event <> 'created'"
                ),
                {"id": declined},
            )
            .mappings()
            .all()
        )
    assert len(accepted_events) == 1
    accepted_row = accepted_events[0]
    assert accepted_row["event"] == "confirmed"
    assert accepted_row["ip"] is not None
    assert accepted_row["user_agent"] is not None
    assert accepted_row["policy_version"] is None
    assert accepted_row["consent_purposes"] is None

    assert len(declined_events) == 1
    assert declined_events[0]["event"] == "declined"

    confirmed_audit = events(
        migrate_engine, action="booking_confirmed", target=f"booking:{accepted}"
    )
    declined_audit = events(migrate_engine, action="booking_declined", target=f"booking:{declined}")
    assert len(confirmed_audit) == 1
    assert confirmed_audit[0]["actor_user_id"] == people.both
    assert len(declined_audit) == 1


# F11 - a transition does not touch expires_at.
# Kills: SET status = :status, expires_at = NULL.
def test_a_transition_does_not_touch_expires_at(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    accepted = make_pending(app, people.a, ready, starts_at=at("09:00"))
    with tenant_context(people.a) as session:
        before = session.scalar(
            text("SELECT expires_at FROM bookings WHERE id = :id"), {"id": accepted}
        )
    assert patch(owner, accepted, "confirmed").status_code == 200
    with tenant_context(people.a) as session:
        after = session.scalar(
            text("SELECT expires_at FROM bookings WHERE id = :id"), {"id": accepted}
        )
    assert before == after

    declined = make_pending(app, people.a, ready, starts_at=at("11:00"))
    with tenant_context(people.a) as session:
        before2 = session.scalar(
            text("SELECT expires_at FROM bookings WHERE id = :id"), {"id": declined}
        )
    assert patch(owner, declined, "declined").status_code == 200
    with tenant_context(people.a) as session:
        after2 = session.scalar(
            text("SELECT expires_at FROM bookings WHERE id = :id"), {"id": declined}
        )
    assert before2 == after2


# F12 (T25) - every value of the request literal transitions from a LEGAL source, answers with the
# row, and logs its own action. Parametrized over the Literal itself, so it also covers the
# write-site value set: a value added to the Literal with no TRANSITIONS entry is a KeyError here
# (and a 500 in production). Each case now builds its target's own legal source state, and reads
# the action out of TRANSITIONS' rule rather than out of a bare status->Action map (ZIF-55 D10).
# That read is DERIVED from the code under test, so it pins the WIRING (each target records its
# own action) and not the action strings. F16 below, hard-coded, is the one that pins the strings.
# Kills: adds a value to the Literal without a TRANSITIONS entry; echoes the request instead of
# RETURNING the row; answers with another booking's id; writes the wrong audit action.
@pytest.mark.parametrize(
    "status", get_args(bookings.StatusChange.model_fields["status"].annotation)
)
def test_each_status_transitions_and_logs_its_action(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    migrate_engine: Engine,
    status: str,
) -> None:
    rule = bookings.TRANSITIONS[status]
    if rule.past_only:
        booking_id = confirmed_in_the_past(app, owner, people.a, ready)
    else:
        booking_id = make_pending(app, people.a, ready)

    response = patch(owner, booking_id, status)

    assert (response.status_code, response.json()) == (200, {"id": booking_id, "status": status})
    assert status_of(people.a, booking_id) == status
    audit = events(migrate_engine, action=rule.action, target=f"booking:{booking_id}")
    assert len(audit) == 1


# F13 - `limit` truncates the queue, and the rows that survive are the soonest ones.
# Kills: hardcoded `LIMIT 50` (every call returns all three), and a truncation applied before the
# ORDER BY (limit=1 returns whichever row the scan reached first, not the earliest).
def test_limit_truncates_the_queue_keeping_the_soonest(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    late = make_pending(app, people.a, ready, starts_at=at("15:00"))
    early = make_pending(app, people.a, ready, starts_at=at("09:00"))
    mid = make_pending(app, people.a, ready, starts_at=at("12:00"))

    def queue(limit: int) -> list[str]:
        response = owner.get("/api/bookings/pending", params={"limit": limit})
        assert response.status_code == 200
        return [row["id"] for row in response.json()]

    assert queue(1) == [early]
    assert queue(2) == [early, mid]
    assert queue(3) == [early, mid, late]


# F14 - the PATCH body is exactly one field out of the Literal, strictly typed.
# Kills: a non-strict model (a bare str status, extra="ignore", an Optional status).
@pytest.mark.parametrize(
    "body",
    [
        {"status": "expired"},
        {"status": "confirmed", "reason": "x"},
        {"status": "CONFIRMED"},
        {},
        {"status": None},
    ],
    ids=["other status", "extra field", "wrong case", "empty body", "null status"],
)
def test_a_body_outside_the_literal_is_refused(
    people: People, app: FastAPI, owner: TestClient, ready: str, body: Any
) -> None:
    booking_id = make_pending(app, people.a, ready)

    response = owner.patch(transition_url(booking_id), json=body)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert status_of(people.a, booking_id) == "pending"


# F15 (was G1) - accepting keeps the slot held, and keeps holding it past the TTL.
# The TTL is what discriminates: a pending holds the slot exactly as a confirmed one does, so the
# expiry is the only state in which the two differ - an accepted booking holds its slot forever,
# while a row left pending is swept to 'expired' by the next create (app/bookings.py's EXPIRE) and
# frees it. Kills: `SET status = status` (a no-op UPDATE that still RETURNs a row).
def test_accepting_keeps_the_slot_held(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)

    response = patch(owner, booking_id, "confirmed")
    assert response.status_code == 200
    expire(people.a, booking_id)

    second = post_booking(new_client(app), people.a, ready, email=fresh_email())
    assert (second.status_code, second.json()) == (409, {"code": "slot_unavailable"})


# G2 - two concurrent transitions give one 200 and one 409.
def test_two_concurrent_transitions_give_one_200_and_one_409(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    app_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booking_id = make_pending(app, people.a, ready)
    real_may_manage = members.may_manage
    holding = threading.Event()
    release = threading.Event()
    paused_once = threading.Event()

    def paused_may_manage(current: Any, user_id: Any) -> bool:
        result: bool = real_may_manage(current, user_id)
        if not paused_once.is_set():
            paused_once.set()
            holding.set()
            release.wait(timeout=10)
        return result

    monkeypatch.setattr(members, "may_manage", paused_may_manage)
    results: dict[str, Response] = {}

    def confirm() -> None:
        results["confirm"] = patch(signed_in(app, people.a, people.both), booking_id, "confirmed")

    def decline() -> None:
        results["decline"] = patch(signed_in(app, people.a, people.both), booking_id, "declined")

    thread_a = threading.Thread(target=confirm)
    thread_a.start()
    assert holding.wait(timeout=10)
    thread_b = threading.Thread(target=decline)
    thread_b.start()
    wait_until_blocked(app_engine, 1)
    release.set()
    for thread in (thread_a, thread_b):
        thread.join(timeout=10)
    assert not thread_a.is_alive() and not thread_b.is_alive()

    statuses = sorted(r.status_code for r in results.values())
    assert statuses == [200, 409]
    with tenant_context(people.a) as session:
        settled = (
            session.execute(
                text(
                    "SELECT event FROM booking_events WHERE booking_id = :id AND event <> 'created'"
                ),
                {"id": booking_id},
            )
            .scalars()
            .all()
        )
    assert len(settled) == 1


# G3 - unauthenticated is 401.
def test_unauthenticated_is_401(people: People, app: FastAPI, ready: str) -> None:
    booking_id = make_pending(app, people.a, ready)
    client = new_client(app)

    assert client.get("/api/bookings/pending").status_code == 401
    assert (patch(client, booking_id, "confirmed")).status_code == 401


# G4 - pending_ttl_hours bounds.
@pytest.mark.parametrize("value,expected", [(0, 422), (169, 422), (1, 200), (168, 200)])
def test_pending_ttl_hours_bounds(
    people: People, app: FastAPI, owner: TestClient, value: int, expected: int
) -> None:
    response = owner.put("/api/settings", json={"pending_ttl_hours": value})
    assert response.status_code == expected


@pytest.mark.parametrize("value", [24.0, "24", True])
def test_pending_ttl_hours_rejects_non_strict_int(
    people: People, app: FastAPI, owner: TestClient, value: object
) -> None:
    response = owner.put("/api/settings", json={"pending_ttl_hours": value})
    assert response.status_code == 422


# G5 - no contact details leak into the queue row.
def test_no_contact_details_leak_into_the_queue(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    email = fresh_email()
    booking_id = make_pending(app, people.a, ready, email=email)

    response = owner.get("/api/bookings/pending")

    assert response.status_code == 200
    assert email not in response.text
    row = next(r for r in response.json() if r["id"] == booking_id)
    assert "client_name" in row
    assert "email" not in row and "phone" not in row


# G7 (partial) - Cache-Control on both routes.
def test_both_routes_set_no_store(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)

    assert owner.get("/api/bookings/pending").headers["Cache-Control"] == "no-store"
    assert patch(owner, booking_id, "confirmed").headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------------------------------
# ZIF-55: three more merchant targets on the same route (cancelled_by_merchant, completed,
# no_show). See docs/specs/2026-09-22-zif-55-spec.md SS5 for the contract each one fences.
# ---------------------------------------------------------------------------------------------


def events_of(tenant_id: uuid.UUID, booking_id: str) -> list[str]:
    with tenant_context(tenant_id) as session:
        return list(
            session.execute(
                text(
                    "SELECT event FROM booking_events WHERE booking_id = :id AND event <> 'created'"
                    " ORDER BY created_at"
                ),
                {"id": booking_id},
            ).scalars()
        )


# F16 (T13) - each new target, from each of its legal sources: the status changes, exactly one
# booking_events row carries the TARGET value, and exactly one audit row carries that target's OWN
# action.
# Kills: a TRANSITIONS map that writes one shared action or event string for all three; a missing
# TRANSITIONS entry (KeyError -> 500).
@pytest.mark.parametrize(
    "source,target,action",
    [
        ("pending", "cancelled_by_merchant", "booking_cancelled_by_merchant"),
        ("confirmed", "cancelled_by_merchant", "booking_cancelled_by_merchant"),
        ("confirmed", "completed", "booking_completed"),
        ("confirmed", "no_show", "booking_no_show_recorded"),
    ],
    ids=["pending cancel", "confirmed cancel", "completed", "no show"],
)
def test_each_new_target_records_its_own_event_and_action(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    migrate_engine: Engine,
    source: str,
    target: str,
    action: str,
) -> None:
    past = bookings.TRANSITIONS[target].past_only
    if source == "confirmed":
        booking_id = (
            confirmed_in_the_past(app, owner, people.a, ready)
            if past
            else make_pending(app, people.a, ready)
        )
        if not past:
            assert patch(owner, booking_id, "confirmed").status_code == 200
    else:
        booking_id = make_pending(app, people.a, ready)

    response = patch(owner, booking_id, target)

    assert (response.status_code, response.json()) == (200, {"id": booking_id, "status": target})
    assert status_of(people.a, booking_id) == target
    assert events_of(people.a, booking_id)[-1] == target
    assert events_of(people.a, booking_id).count(target) == 1
    assert len(events(migrate_engine, action=action, target=f"booking:{booking_id}")) == 1


# F17 (T14) - a booking the merchant never accepted cannot be marked done or no-show. The pending
# is SHIFTED into the past first: without that, the past_only predicate refuses it whatever the
# source list says, and the fence is dead against its own named wrong implementation.
# Kills: widening completed's or no_show's legal sources to include `pending`.
@pytest.mark.parametrize("target", ["completed", "no_show"])
def test_a_pending_booking_cannot_be_marked_done_or_no_show(
    people: People, app: FastAPI, owner: TestClient, ready: str, target: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    shift(people.a, booking_id, PAST)  # past, live, and still pending: only the source list refuses

    response = patch(owner, booking_id, target)

    assert (response.status_code, response.json()) == (409, {"code": "invalid_transition"})
    assert status_of(people.a, booking_id) == "pending"
    assert events_of(people.a, booking_id) == []


# F18 (T15) - the outcome targets open at starts_at, not at ends_at. A merchant knows a no-show at
# the start time; making them wait for a haircut's scheduled end is a nuisance with no upside.
# Kills: dropping `NOT :past_only OR starts_at <= now()` (the "before" leg passes); writing it as
# `ends_at <= now()` (the "started but not ended" leg 409s).
@pytest.mark.parametrize("target", ["completed", "no_show"])
def test_an_outcome_can_be_recorded_from_the_start_time_but_not_before(
    people: People, app: FastAPI, owner: TestClient, ready: str, target: str
) -> None:
    starts_at = at("09:00")
    booking_id = make_pending(app, people.a, ready, starts_at=starts_at)
    assert patch(owner, booking_id, "confirmed").status_code == 200

    early = patch(owner, booking_id, target)
    assert (early.status_code, early.json()) == (409, {"code": "invalid_transition"})
    assert status_of(people.a, booking_id) == "confirmed"

    # One minute in: a 30-minute service that has STARTED and has not ENDED.
    shift(people.a, booking_id, ago(starts_at, timedelta(minutes=1)))

    assert patch(owner, booking_id, target).status_code == 200
    assert status_of(people.a, booking_id) == target


# F19 (T16) - an AUTO-CONFIRMED booking carries expires_at IS NULL, and the merchant can still
# cancel it. The setup matters: a booking confirmed through this PATCH keeps the non-NULL, still
# future expires_at it was given as a pending (D13, and F11 fences that it keeps it), so it passes
# a bare `expires_at > now()` and fences nothing. Only auto_confirm produces the NULL.
# Kills: keeping ZIF-52's bare `expires_at > now()` in the widened UPDATE - `NULL > now()` is NULL,
# zero rows, and every merchant cancellation of an auto-confirmed booking answers 409.
def test_an_auto_confirmed_booking_can_be_cancelled_by_the_merchant(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    save_setting(people.a, "auto_confirm", True)
    response = post_booking(new_client(app), people.a, ready)
    assert (response.status_code, response.json()["status"]) == (201, "confirmed")
    booking_id = response.json()["id"]
    with tenant_context(people.a) as session:
        assert (
            session.scalar(
                text("SELECT expires_at FROM bookings WHERE id = :id"), {"id": booking_id}
            )
            is None
        )

    cancelled = patch(owner, booking_id, "cancelled_by_merchant")

    assert (cancelled.status_code, cancelled.json()) == (
        200,
        {"id": booking_id, "status": "cancelled_by_merchant"},
    )


# F20 (T17) - a TTL-passed pending is dead for every target, cancellation included.
# Kills: deleting the liveness clause outright to make F19 pass.
#
# REJECTED (reviewer, ZIF-55 R3): the merchant therefore cannot cancel an expired pending, and the
# row keeps status='pending' until some later booking POST runs the EXPIRE sweep. Left as it is,
# on purpose: it is what the spec says, and the row is already invisible in the queue (QUEUE
# filters on expires_at > now()) and inert (`pending` is in EXPIRING, so it holds no slot), which
# leaves the merchant nothing to cancel. The sweeper itself is ZIF-122's.
def test_a_ttl_passed_pending_cannot_be_cancelled_either(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    expire(people.a, booking_id)

    response = patch(owner, booking_id, "cancelled_by_merchant")

    assert (response.status_code, response.json()) == (409, {"code": "invalid_transition"})
    assert status_of(people.a, booking_id) == "pending"


# F21 (T18) - GUARD, not a fence; the original "fence" label was wrong. Its named kill - adding
# `expires_at = NULL` (or `= now()`) to the SET list - turns the pre-existing F11 red as well, and
# TRANSITION has ONE shared SET list, so the only mutation this distinguishes from F11 is a
# per-target SET list nobody would write. Kept at three lines because it says out loud that the
# hold's expiry survives a cancellation too. F11 is what actually kills the edit.
def test_cancelling_a_pending_leaves_its_expiry_exactly_as_it_was(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)

    def expiry() -> object:
        with tenant_context(people.a) as session:
            return session.scalar(
                text("SELECT expires_at FROM bookings WHERE id = :id"), {"id": booking_id}
            )

    before = expiry()
    assert patch(owner, booking_id, "cancelled_by_merchant").status_code == 200

    assert expiry() == before


# F22 (T19) - no_show FREES the slot and completed does NOT. This is the walk-in case migration
# 0026 designed the exclusion constraint's status predicate for, and the reason no_show is
# terminal: once the slot is free, an undo is a 23P01.
# Kills: 0026's EXCLUDE predicate written with no_show in it, or without completed, on a database
# migrated from the edited migration.
# Does NOT kill either of those edits made to `availability.OCCUPYING` instead: that tuple is a
# query filter, and the constraint exercised here is built from 0026's own copy of the list. The
# pre-existing test_bookings_db.py::test_the_predicate_is_exactly_the_four_occupying_statuses is
# what holds the two in lockstep and what catches a change to the tuple; this test is red only for
# the predicate form they share.
def test_no_show_frees_the_slot_and_completed_does_not(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    # Two bookings an hour apart, both moved by the SAME delta so shifting cannot collide them
    # with each other under ex_bookings_worker_overlap.
    worker_id = member_id(people.a, people.both)
    marked_no_show = confirmed_in_the_past(app, owner, people.a, ready, "09:00")
    marked_completed = confirmed_in_the_past(app, owner, people.a, ready, "10:00")
    assert patch(owner, marked_no_show, "no_show").status_code == 200
    assert patch(owner, marked_completed, "completed").status_code == 200

    def walk_in(booking_id: str) -> None:
        with tenant_context(people.a) as session:
            span = session.execute(
                text("SELECT starts_at, ends_at FROM bookings WHERE id = :id"), {"id": booking_id}
            ).one()
        seed_booking(
            people.a, ready, worker_id, span.starts_at.isoformat(), span.ends_at.isoformat()
        )

    walk_in(marked_no_show)  # the freed slot: a walk-in may legitimately take it

    with pytest.raises(IntegrityError) as raised:
        walk_in(marked_completed)  # still OCCUPYING
    assert isinstance(raised.value.orig, ExclusionViolation)
    assert raised.value.orig.diag.constraint_name == bookings.OVERLAP


# F23 (T20) - `completed` sources `confirmed` and nothing else. Both other ways back INTO it are
# the same hazard D14 describes for no_show: a status that FREED the slot would re-enter the
# exclusion predicate, which is a 23P01 -> 500 the moment a walk-in legitimately holds the slot
# the merchant gave up. The asymmetry is deliberate: completed -> no_show and
# completed -> cancelled_by_merchant are legal (F25); the reverse of each is not.
# Kills: listing no_show, or cancelled_by_merchant, among completed's legal sources.
@pytest.mark.parametrize("settled", ["no_show", "cancelled_by_merchant"])
def test_a_slot_freeing_status_cannot_be_turned_back_into_completed(
    people: People, app: FastAPI, owner: TestClient, ready: str, settled: str
) -> None:
    booking_id = confirmed_in_the_past(app, owner, people.a, ready)
    assert patch(owner, booking_id, settled).status_code == 200

    response = patch(owner, booking_id, "completed")

    assert (response.status_code, response.json()) == (409, {"code": "invalid_transition"})
    assert status_of(people.a, booking_id) == settled


# F24 (T21) - the advisory lock is still statement 1 on the new targets' path too, exactly as F1
# fences it for confirmed.
# Kills: taking the lock after loading the booking, or omitting it on the new targets' path.
def test_cancelling_takes_the_tenant_lock_first(
    people: People, app: FastAPI, owner: TestClient, ready: str, app_engine: Engine
) -> None:
    booking_id = make_pending(app, people.a, ready)
    executed: list[tuple[str, Any]] = []

    def record(conn: object, cursor: object, statement: str, parameters: Any, *args: Any) -> None:
        executed.append((statement, parameters))

    event.listen(app_engine, "before_cursor_execute", record)
    try:
        response = patch(owner, booking_id, "cancelled_by_merchant")
    finally:
        event.remove(app_engine, "before_cursor_execute", record)

    assert response.status_code == 200
    tenant_set = next(
        i for i, (s, _) in enumerate(executed) if s.startswith("SELECT set_config('app.tenant_id'")
    )
    lock_statement, lock_params = executed[tenant_set + 1]
    assert lock_statement.startswith("SELECT pg_advisory_xact_lock(")
    assert lock_params == {"key": 51}


# F25 (T26) - a mis-clicked `completed` is correctable, RESTORE included. Without the first two
# corrections a merchant who clicks "completed" one second into a twelve-hour booking has
# permanently destroyed the no-show outcome AND permanently pinned the slot inside OCCUPYING:
# completed does not free the slot and DELETE is revoked from the app role. Without the third,
# `completed` is only EXIT-able and not CORRECTABLE: the two exits write a false record (no_show)
# or a 100% refund by AC (cancelled_by_merchant), so every way out of a mis-click costs money or
# truth. `completed -> confirmed` is the one that costs neither.
# Kills: `completed` as a terminal state - all three corrections 409; dropping `completed` from
# `confirmed`'s source list - the restore 409s and the other two still pass. Also kills adding the
# sources without their event/audit entries.
#
# The action read below is derived from the code under test and pins the wiring, not the strings;
# F16's hard-coded parametrize list is what pins the strings.
@pytest.mark.parametrize("correction", ["no_show", "cancelled_by_merchant", "confirmed"])
def test_a_mis_clicked_completed_can_be_corrected(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    migrate_engine: Engine,
    correction: str,
) -> None:
    booking_id = confirmed_in_the_past(app, owner, people.a, ready)
    assert patch(owner, booking_id, "completed").status_code == 200

    response = patch(owner, booking_id, correction)

    assert (response.status_code, response.json()) == (
        200,
        {"id": booking_id, "status": correction},
    )
    assert status_of(people.a, booking_id) == correction
    assert events_of(people.a, booking_id)[-1] == correction
    # The whole audit trail of this booking, in order. `booking:<uuid>` is a unique target, so
    # this needs no tenant scoping. The restore legitimately writes a SECOND booking_confirmed -
    # it is the same act as the first accept - which is why this asserts the sequence and not a
    # count of one.
    audit = [e["action"] for e in events(migrate_engine, target=f"booking:{booking_id}")]
    assert audit == [
        "booking_confirmed",
        "booking_completed",
        bookings.TRANSITIONS[correction].action,
    ]
    with tenant_context(people.a) as session:
        span = session.execute(
            text("SELECT starts_at, ends_at FROM bookings WHERE id = :id"), {"id": booking_id}
        ).one()

    def walk_in() -> None:
        seed_booking(
            people.a,
            ready,
            member_id(people.a, people.both),
            span.starts_at.isoformat(),
            span.ends_at.isoformat(),
        )

    if correction == "confirmed":
        # The restore cannot raise 23P01, and this is the reason rather than an assumption:
        # `confirmed` and `completed` are BOTH in OCCUPYING, so the row never leaves the exclusion
        # predicate and there is no window for a walk-in to take the slot. The 200 above is half
        # the proof; that the slot is still held is the other half.
        with pytest.raises(IntegrityError) as raised:
            walk_in()
        assert isinstance(raised.value.orig, ExclusionViolation)
    else:
        walk_in()  # out of OCCUPYING: the slot the mis-click pinned is free again


# G8 (T22) - the 404/403 pair holds for a new target too. Guard: F5/F6 fence the order on the
# shared code path.
def test_the_new_targets_keep_the_404_403_pair(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    other = signed_in(app, people.b, people.only_b)
    assert patch(other, booking_id, "cancelled_by_merchant").json() == {"code": "not_found"}
    assert patch(other, booking_id, "cancelled_by_merchant").status_code == 404

    non_manager = signed_in(app, people.a, people.only_a)
    refused = patch(non_manager, booking_id, "cancelled_by_merchant")
    assert (refused.status_code, refused.json()) == (403, {"code": "owner_only"})
    assert status_of(people.a, booking_id) == "pending"


# G9 (T23) - the body is still exactly one field out of the Literal, now five values wide.
# Guard: F14 already parametrises the refusals.
@pytest.mark.parametrize(
    "status", ["expired", "awaiting_payment", "cancelled_by_client", "created"]
)
def test_a_status_outside_the_five_targets_is_422(
    people: People, app: FastAPI, owner: TestClient, ready: str, status: str
) -> None:
    booking_id = make_pending(app, people.a, ready)

    response = owner.patch(transition_url(booking_id), json={"status": status})

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert status_of(people.a, booking_id) == "pending"


@pytest.fixture
def worker_ready(people: People, owner: TestClient) -> tuple[uuid.UUID, str]:
    """A 30-minute service performed by only_a, a genuine NON-OWNER worker. `ready` assigns
    people.both, who is an owner in tenant a, which would make "worker" and "owner"
    indistinguishable on any test about the difference."""
    worker_user = people.only_a
    service_id = new_service(owner)
    seed(people.a, worker_user, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, worker_user))
    return worker_user, service_id


def worker_completed(
    app: FastAPI, people: People, worker_ready: tuple[uuid.UUID, str], clock_time: str = "09:00"
) -> tuple[TestClient, str]:
    """A past booking the assigned NON-OWNER worker has taken all the way to `completed` on their
    own - which they may, and which is the whole point: the trap starts from a state they can
    reach without an owner."""
    worker_user, service_id = worker_ready
    worker = signed_in(app, people.a, worker_user)
    booking_id = make_pending(
        app,
        people.a,
        service_id,
        member_id=str(member_id(people.a, worker_user)),
        starts_at=at(clock_time),
    )
    assert patch(worker, booking_id, "confirmed").status_code == 200
    shift(people.a, booking_id, PAST)
    assert patch(worker, booking_id, "completed").status_code == 200
    return worker, booking_id


# F26 (T27) - leaving `completed` is the OWNER's alone, whatever the target. A non-owner worker
# who mis-clicks "completed" on their own appointment must not be able to turn it into a record
# nobody can put back: `no_show` is terminal and frees the slot, `cancelled_by_merchant` is a 100%
# refund by AC, and `confirmed` un-does a settled outcome. An owner can do all three.
# Kills: dropping the OWNER_ONLY_SOURCES check in transition() - all three legs answer 200 for the
# worker, and with `cancelled_by_merchant` there is then no way back at all (DELETE is revoked,
# 0026:203, and `confirmed` would not source it); keying the check on the TARGET instead of the
# SOURCE - the worker's own `confirmed -> completed` above 403s and this test never gets started.
@pytest.mark.parametrize("target", ["cancelled_by_merchant", "no_show", "confirmed"])
def test_only_an_owner_may_transition_out_of_completed(
    people: People,
    app: FastAPI,
    owner: TestClient,
    worker_ready: tuple[uuid.UUID, str],
    target: str,
) -> None:
    worker, booking_id = worker_completed(app, people, worker_ready)

    refused = patch(worker, booking_id, target)

    assert (refused.status_code, refused.json()) == (403, {"code": "owner_only"})
    assert status_of(people.a, booking_id) == "completed"
    assert events_of(people.a, booking_id) == ["confirmed", "completed"]

    assert patch(owner, booking_id, target).status_code == 200
    assert status_of(people.a, booking_id) == target


# F27 (T28) - and the worker can still do their job. The owner-only rule is keyed on the SOURCE,
# so recording the outcome of an appointment they performed is untouched.
# Kills: making `completed` or `no_show` owner-only targets instead - both legs 403.
@pytest.mark.parametrize("target", ["completed", "no_show"])
def test_the_assigned_worker_still_records_the_outcome_of_their_own_appointment(
    people: People, app: FastAPI, worker_ready: tuple[uuid.UUID, str], target: str
) -> None:
    worker_user, service_id = worker_ready
    worker = signed_in(app, people.a, worker_user)
    booking_id = make_pending(
        app,
        people.a,
        service_id,
        member_id=str(member_id(people.a, worker_user)),
        starts_at=at("09:00"),
    )
    assert patch(worker, booking_id, "confirmed").status_code == 200
    shift(people.a, booking_id, PAST)

    assert patch(worker, booking_id, target).status_code == 200
    assert status_of(people.a, booking_id) == target
