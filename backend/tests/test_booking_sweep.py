"""The expiry sweeper (ZIF-122): bookings.sweep() settles due pendings in every tenant. The xdist
database keeps other tests' tenants and due pendings, so every assertion is on the seeded booking
ids and their events, never on sweep()'s total; only a second sweep returning 0 is safe."""

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from app import bookings
from app.db import tenant_context
from tests.conftest import People, fresh_email, member_id, wait_until_blocked
from tests.test_bookings_db import T1, T2, book, seed_service
from tests.test_clients_db import seed_client

HOUR = timedelta(hours=1)


def seed(
    tenant_id: uuid.UUID, user_id: uuid.UUID, window: tuple[datetime, datetime], **kwargs: Any
) -> uuid.UUID:
    return book(
        tenant_id,
        client_id=seed_client(tenant_id, email=fresh_email()),
        worker_id=member_id(tenant_id, user_id),
        service_id=seed_service(tenant_id),
        starts_at=window[0],
        ends_at=window[1],
        **kwargs,
    )


def due(tenant_id: uuid.UUID, user_id: uuid.UUID, window: tuple[datetime, datetime]) -> uuid.UUID:
    return seed(tenant_id, user_id, window, status="pending", expires_at=datetime.now(UTC) - HOUR)


def status_of(tenant_id: uuid.UUID, booking_id: uuid.UUID) -> str:
    with tenant_context(tenant_id) as session:
        found: str = session.scalar(
            text("SELECT status FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    return found


def events_of(tenant_id: uuid.UUID, booking_id: uuid.UUID) -> list[Any]:
    with tenant_context(tenant_id) as session:
        return list(
            session.execute(
                text(
                    "SELECT event, tenant_id, ip, user_agent, policy_version FROM booking_events "
                    "WHERE booking_id = :id AND event = 'expired'"
                ),
                {"id": booking_id},
            )
        )


# 1: FENCE. Wrong impl: sweep() runs EXPIRE under one tenant's context (or none) instead of one
# per tenant - RLS hides the other tenants' rows.
def test_one_sweep_expires_the_due_pendings_of_every_tenant(people: People) -> None:
    in_a = due(people.a, people.both, T1)
    in_b = due(people.b, people.both, T1)

    bookings.sweep()

    assert status_of(people.a, in_a) == "expired"
    assert status_of(people.b, in_b) == "expired"


# 1b: the second expiring status settles too.
def test_a_due_awaiting_payment_expires_as_well(people: People) -> None:
    waiting = seed(
        people.a,
        people.both,
        T2,
        status="awaiting_payment",
        expires_at=datetime.now(UTC) - HOUR,
    )

    bookings.sweep()

    assert status_of(people.a, waiting) == "expired"


# 2: FENCE. Wrong impl: EXPIRE is a bare UPDATE with no booking_events insert.
def test_each_expired_booking_gets_one_system_event_in_its_own_tenant(people: People) -> None:
    in_a = due(people.a, people.both, T1)
    in_b = due(people.b, people.both, T1)

    bookings.sweep()

    for tenant_id, booking_id in ((people.a, in_a), (people.b, in_b)):
        [row] = events_of(tenant_id, booking_id)
        assert (row.event, row.tenant_id) == ("expired", tenant_id)
        assert (row.ip, row.user_agent, row.policy_version) == (None, None, None)


# 3: FENCE. Wrong impl: the insert is not tied to the UPDATE's RETURNING (it selects every
# booking that is 'expired'), so a second sweep writes the events again.
def test_a_second_sweep_expires_nothing_and_writes_no_events(people: People) -> None:
    booking_id = due(people.a, people.both, T1)
    bookings.sweep()

    assert bookings.sweep() == 0
    assert len(events_of(people.a, booking_id)) == 1


# 4: FENCE. Wrong impl: the qualifier drops the expires_at clause (or the status clause).
def test_a_live_pending_and_a_confirmed_booking_are_left_alone(people: People) -> None:
    live = seed(people.a, people.both, T1, status="pending", expires_at=datetime.now(UTC) + HOUR)
    confirmed = seed(
        people.a, people.both, T2, status="confirmed", expires_at=datetime.now(UTC) - HOUR
    )

    bookings.sweep()

    assert (status_of(people.a, live), status_of(people.a, confirmed)) == ("pending", "confirmed")
    assert events_of(people.a, live) == events_of(people.a, confirmed) == []


# 5: FENCE. Wrong impl: sweep() skips LOCK, so it settles under a booking transaction's feet.
def test_the_sweep_waits_for_the_tenant_lock(people: People, app_engine: Engine) -> None:
    booking_id = due(people.a, people.both, T1)
    with app_engine.begin() as blocker:
        blocker.execute(
            text("SELECT pg_advisory_xact_lock(51, hashtext(:tenant_id))"),
            {"tenant_id": str(people.a)},
        )
        sweeper = threading.Thread(target=bookings.sweep)
        sweeper.start()
        wait_until_blocked(app_engine, 1)
        assert status_of(people.a, booking_id) == "pending"
    sweeper.join(timeout=30)

    assert not sweeper.is_alive()
    assert status_of(people.a, booking_id) == "expired"
