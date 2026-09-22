"""PATCH /api/bookings/{booking_id} and GET /api/bookings/pending (ZIF-52): merchants accept or
decline pending bookings. See docs/specs/2026-09-22-zif-52-accept-decline.md for the properties
each fence protects."""

import threading
import time
import uuid
from datetime import date, timedelta
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, event, text

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
from tests.test_availability_api import assign, new_service, weekdays
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

    assert (response.status_code, response.json()) == (409, {"code": "not_pending"})
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

    assert (again.status_code, again.json()) == (409, {"code": "not_pending"})
    assert (declined_after.status_code, declined_after.json()) == (409, {"code": "not_pending"})
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


# F12 - every value of the request literal transitions, answers with the row, and logs its own
# action. Parametrized over the Literal itself, so it also covers the write-site value set: a value
# added to the Literal with no TRANSITIONS entry is a KeyError here (and a 500 in production).
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
    booking_id = make_pending(app, people.a, ready)

    response = patch(owner, booking_id, status)

    assert (response.status_code, response.json()) == (200, {"id": booking_id, "status": status})
    assert status_of(people.a, booking_id) == status
    audit = events(
        migrate_engine, action=bookings.TRANSITIONS[status], target=f"booking:{booking_id}"
    )
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
