"""bookings and booking_events (ZIF-51): the tables' own constraints and grants, and the constraint
that makes a double-booking impossible, driven by raw inserts that bypass the advisory lock on
purpose - the lock is the route's concern (tests/test_bookings_api.py), the constraint is the
database's control for a writer that forgets it."""

import importlib.util
import json
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from psycopg.errors import (
    CheckViolation,
    ExclusionViolation,
    ForeignKeyViolation,
    InsufficientPrivilege,
)
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app import availability
from app.db import tenant_context
from tests.conftest import People, fresh_email, member_id, wait_until_blocked
from tests.test_clients_db import seed_client

T1 = (datetime(2026, 6, 1, 10, 0, tzinfo=UTC), datetime(2026, 6, 1, 11, 0, tzinfo=UTC))
T2 = (datetime(2026, 6, 1, 11, 0, tzinfo=UTC), datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
T3 = (datetime(2026, 6, 2, 10, 0, tzinfo=UTC), datetime(2026, 6, 2, 11, 0, tzinfo=UTC))
T4 = (datetime(2026, 6, 3, 10, 0, tzinfo=UTC), datetime(2026, 6, 3, 11, 0, tzinfo=UTC))
T5 = (datetime(2026, 6, 4, 10, 0, tzinfo=UTC), datetime(2026, 6, 4, 11, 0, tzinfo=UTC))
T6 = (datetime(2026, 6, 5, 10, 0, tzinfo=UTC), datetime(2026, 6, 5, 11, 0, tzinfo=UTC))
T7 = (datetime(2026, 6, 6, 10, 0, tzinfo=UTC), datetime(2026, 6, 6, 11, 0, tzinfo=UTC))
T8 = (datetime(2026, 6, 7, 10, 0, tzinfo=UTC), datetime(2026, 6, 7, 11, 0, tzinfo=UTC))
T_CANCEL_COMMITS = (
    datetime(2026, 6, 8, 10, 0, tzinfo=UTC),
    datetime(2026, 6, 8, 11, 0, tzinfo=UTC),
)
T_CANCEL_ROLLS_BACK = (
    datetime(2026, 6, 9, 10, 0, tzinfo=UTC),
    datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
)


def seed_service(
    tenant_id: uuid.UUID, *, duration_minutes: int = 30, currency: str = "EUR"
) -> uuid.UUID:
    """A minimal real service, inserted directly, for a booking's foreign key."""
    with tenant_context(tenant_id) as session:
        service_id: uuid.UUID = session.scalar(
            text("""
            INSERT INTO services (tenant_id, name, price_amount_minor, price_currency,
                                   duration_minutes)
            VALUES (current_setting('app.tenant_id')::uuid, '{"en": "Cut"}'::jsonb, 2500,
                    :currency, :duration_minutes)
            RETURNING id
            """),
            {"currency": currency, "duration_minutes": duration_minutes},
        )
    return service_id


INSERT_BOOKING = text("""
INSERT INTO bookings (tenant_id, client_id, worker_id, service_id, starts_at, ends_at, status,
                       expires_at, source, service_name, price_amount_minor, price_currency,
                       duration_minutes, auto_confirm_at_booking, worker_display_name)
VALUES (current_setting('app.tenant_id')::uuid, :client_id, :worker_id, :service_id, :starts_at,
        :ends_at, :status, :expires_at, :source, CAST(:service_name AS jsonb),
        :price_amount_minor, :price_currency, :duration_minutes, :auto_confirm_at_booking,
        :display_name)
RETURNING id
""")


def insert_booking(
    session: Session,
    *,
    client_id: uuid.UUID,
    worker_id: uuid.UUID,
    service_id: uuid.UUID,
    starts_at: datetime,
    ends_at: datetime,
    status: str = "confirmed",
    expires_at: datetime | None = None,
    source: str = "merchant",
    price_amount_minor: int = 2500,
    price_currency: str = "EUR",
    duration_minutes: int = 30,
    auto_confirm: bool = True,
    display_name: str | None = None,
) -> uuid.UUID:
    booking_id: uuid.UUID = session.scalar(
        INSERT_BOOKING,
        {
            "client_id": client_id,
            "worker_id": worker_id,
            "service_id": service_id,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "status": status,
            "expires_at": expires_at,
            "source": source,
            "service_name": json.dumps({"en": "Cut"}),
            "price_amount_minor": price_amount_minor,
            "price_currency": price_currency,
            "duration_minutes": duration_minutes,
            "auto_confirm_at_booking": auto_confirm,
            "display_name": display_name,
        },
    )
    return booking_id


def book(tenant_id: uuid.UUID, **kwargs: Any) -> uuid.UUID:
    """A raw booking insert, deliberately taking NO advisory lock: this file proves the
    constraint's own behaviour, which is exactly what must still hold for a writer that forgets
    the lock ZIF-51's route takes (tests/test_bookings_api.py)."""
    with tenant_context(tenant_id) as session:
        return insert_booking(session, **kwargs)


# 1: FENCE. Wrong impl: in 0026 upgrade(), drop the ALTER TABLE ... ADD CONSTRAINT statement.
# (downgrade to 0025 first - §15.4's full cycle.)
def test_two_occupying_bookings_of_one_worker_and_interval_collide(people: People) -> None:
    service_id = seed_service(people.a)
    client_id = seed_client(people.a, email=fresh_email())
    worker_id = member_id(people.a, people.both)
    book(
        people.a,
        client_id=client_id,
        worker_id=worker_id,
        service_id=service_id,
        starts_at=T1[0],
        ends_at=T1[1],
        status="confirmed",
    )

    with pytest.raises(IntegrityError) as error:
        book(
            people.a,
            client_id=client_id,
            worker_id=worker_id,
            service_id=service_id,
            starts_at=T1[0],
            ends_at=T1[1],
            status="pending",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    assert isinstance(error.value.orig, ExclusionViolation)
    assert error.value.orig.diag.constraint_name == "ex_bookings_worker_overlap"


# 2: GUARD. Pins the half-open [) default.
def test_touching_intervals_do_not_collide(people: People) -> None:
    service_id = seed_service(people.a)
    client_id = seed_client(people.a, email=fresh_email())
    worker_id = member_id(people.a, people.both)

    book(
        people.a,
        client_id=client_id,
        worker_id=worker_id,
        service_id=service_id,
        starts_at=T2[0],
        ends_at=T2[1],
    )
    book(
        people.a,
        client_id=client_id,
        worker_id=worker_id,
        service_id=service_id,
        starts_at=T2[1],
        ends_at=T2[1] + timedelta(hours=1),
    )


# 3: FENCE, half demoted (the tenant_id half is unfenceable - see the spec's table row 3). Wrong
# impl: remove `worker_id WITH =` from the constraint. (§15.4's full cycle.)
def test_two_workers_of_one_business_never_collide(people: People) -> None:
    service_id = seed_service(people.a)
    client_id = seed_client(people.a, email=fresh_email())
    w1, w2 = member_id(people.a, people.only_a), member_id(people.a, people.both)

    book(
        people.a,
        client_id=client_id,
        worker_id=w1,
        service_id=service_id,
        starts_at=T1[0],
        ends_at=T1[1],
    )
    book(
        people.a,
        client_id=client_id,
        worker_id=w2,
        service_id=service_id,
        starts_at=T1[0],
        ends_at=T1[1],
    )


CONSTRAINTDEF = text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name")


def statuses_in(definition: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", definition))


# 4: FENCE, keystone. Wrong impl: add 'no_show' to OCCUPYING in 0026. (§15.4's full cycle.)
# The regclass form of pg_get_constraintdef is wrong for both constraints (F-a): it raises
# UndefinedTable for the CHECK and silently resolves the same-named INDEX for the EXCLUDE.
def test_the_predicate_is_exactly_the_four_occupying_statuses(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        exclude_def = conn.scalar(CONSTRAINTDEF, {"name": "ex_bookings_worker_overlap"})
        check_def = conn.scalar(CONSTRAINTDEF, {"name": "ck_bookings_status"})
    assert exclude_def is not None
    assert check_def is not None
    exclude_statuses = statuses_in(exclude_def.split("WHERE", 1)[1])
    check_statuses = statuses_in(check_def)
    assert exclude_statuses == set(availability.OCCUPYING)
    assert set(availability.EXPIRING) < set(availability.OCCUPYING)  # strict subset
    assert set(availability.OCCUPYING) <= check_statuses


# 5: CHARACTERISATION. No injection exists: this is a behaviour of Postgres, not of our code.
def test_a_cancelled_booking_frees_its_slot(people: People) -> None:
    service_id = seed_service(people.a)
    client_id = seed_client(people.a, email=fresh_email())
    worker_id = member_id(people.a, people.both)
    booking_id = book(
        people.a,
        client_id=client_id,
        worker_id=worker_id,
        service_id=service_id,
        starts_at=T3[0],
        ends_at=T3[1],
        status="confirmed",
    )
    with tenant_context(people.a) as session:
        session.execute(
            text("UPDATE bookings SET status = 'cancelled_by_client' WHERE id = :id"),
            {"id": booking_id},
        )

    book(
        people.a,
        client_id=client_id,
        worker_id=worker_id,
        service_id=service_id,
        starts_at=T3[0],
        ends_at=T3[1],
        status="confirmed",
    )  # must succeed


# 6: FENCE - F1 concurrent. The row that actually reaches XactLockTableWait + recheck; row 5 does
# not race anything.
def test_an_insert_racing_a_cancel_waits_and_then_succeeds(
    people: People, app_engine: Engine
) -> None:
    service_id = seed_service(people.a)
    client_id = seed_client(people.a, email=fresh_email())
    worker_id = member_id(people.a, people.both)

    # Half A: the cancel commits, so the blocked insert must succeed.
    booking_id = book(
        people.a,
        client_id=client_id,
        worker_id=worker_id,
        service_id=service_id,
        starts_at=T_CANCEL_COMMITS[0],
        ends_at=T_CANCEL_COMMITS[1],
    )
    holding = threading.Event()
    release = threading.Event()

    def holder_commits() -> None:
        with app_engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
            conn.execute(
                text("UPDATE bookings SET status = 'cancelled_by_client' WHERE id = :id"),
                {"id": booking_id},
            )
            holding.set()
            release.wait(timeout=10)
            conn.commit()

    results: dict[str, Any] = {}

    def inserter() -> None:
        try:
            results["id"] = book(
                people.a,
                client_id=client_id,
                worker_id=worker_id,
                service_id=service_id,
                starts_at=T_CANCEL_COMMITS[0],
                ends_at=T_CANCEL_COMMITS[1],
            )
        except Exception as error:  # would be a bug, not the expected outcome
            results["error"] = error

    holder_thread = threading.Thread(target=holder_commits)
    holder_thread.start()
    assert holding.wait(timeout=10)
    insert_thread = threading.Thread(target=inserter)
    insert_thread.start()
    wait_until_blocked(app_engine, 1)
    release.set()
    holder_thread.join(timeout=10)
    insert_thread.join(timeout=10)
    assert not holder_thread.is_alive() and not insert_thread.is_alive()
    assert "id" in results, results.get("error")

    # Half B: the mirror - the cancel rolls back, so the blocked insert must get 23P01.
    booking_id = book(
        people.a,
        client_id=client_id,
        worker_id=worker_id,
        service_id=service_id,
        starts_at=T_CANCEL_ROLLS_BACK[0],
        ends_at=T_CANCEL_ROLLS_BACK[1],
    )
    holding_2 = threading.Event()
    release_2 = threading.Event()

    def holder_rolls_back() -> None:
        with app_engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
            conn.execute(
                text("UPDATE bookings SET status = 'cancelled_by_client' WHERE id = :id"),
                {"id": booking_id},
            )
            holding_2.set()
            release_2.wait(timeout=10)
            conn.rollback()

    results_2: dict[str, Any] = {}

    def inserter_2() -> None:
        try:
            results_2["id"] = book(
                people.a,
                client_id=client_id,
                worker_id=worker_id,
                service_id=service_id,
                starts_at=T_CANCEL_ROLLS_BACK[0],
                ends_at=T_CANCEL_ROLLS_BACK[1],
            )
        except Exception as error:
            results_2["error"] = error

    holder_thread_2 = threading.Thread(target=holder_rolls_back)
    holder_thread_2.start()
    assert holding_2.wait(timeout=10)
    insert_thread_2 = threading.Thread(target=inserter_2)
    insert_thread_2.start()
    wait_until_blocked(app_engine, 1)
    release_2.set()
    holder_thread_2.join(timeout=10)
    insert_thread_2.join(timeout=10)
    assert not holder_thread_2.is_alive() and not insert_thread_2.is_alive()
    error = results_2.get("error")
    assert isinstance(error, IntegrityError), results_2.get("id")
    assert isinstance(error.orig, ExclusionViolation)
    assert error.orig.diag.constraint_name == "ex_bookings_worker_overlap"


# 7: FENCE. Wrong impl: drop REVOKE DELETE ON bookings (and, for events, REVOKE ALL).
# (§15.4's full cycle.)
def test_the_app_role_cannot_delete_a_booking_or_a_booking_event(
    people: People, app_engine: Engine
) -> None:
    service_id = seed_service(people.a)
    client_id = seed_client(people.a, email=fresh_email())
    worker_id = member_id(people.a, people.both)
    booking_id = book(
        people.a,
        client_id=client_id,
        worker_id=worker_id,
        service_id=service_id,
        starts_at=T4[0],
        ends_at=T4[1],
    )

    with pytest.raises(DBAPIError) as booking_error, app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(text("DELETE FROM bookings WHERE id = :id"), {"id": booking_id})
    assert isinstance(booking_error.value.orig, InsufficientPrivilege)

    with pytest.raises(DBAPIError) as event_error, app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(text("DELETE FROM booking_events WHERE booking_id = :id"), {"id": booking_id})
    assert isinstance(event_error.value.orig, InsufficientPrivilege)


# 8: FENCE. Wrong impl: GRANT INSERT ON booking_events TO ziftbook_app, table-wide, no column
# list. (§15.4's full cycle.)
def test_the_app_role_cannot_backdate_or_reid_a_booking_event(
    people: People, app_engine: Engine
) -> None:
    service_id = seed_service(people.a)
    client_id = seed_client(people.a, email=fresh_email())
    worker_id = member_id(people.a, people.both)
    booking_id = book(
        people.a,
        client_id=client_id,
        worker_id=worker_id,
        service_id=service_id,
        starts_at=T5[0],
        ends_at=T5[1],
    )

    with pytest.raises(DBAPIError) as created_at_error, app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text("""
            INSERT INTO booking_events (tenant_id, booking_id, event, created_at)
            VALUES (current_setting('app.tenant_id')::uuid, :booking_id, 'created',
                    now() - interval '1 year')
            """),
            {"booking_id": booking_id},
        )
    assert isinstance(created_at_error.value.orig, InsufficientPrivilege)

    with pytest.raises(DBAPIError) as id_error, app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text("""
            INSERT INTO booking_events (id, tenant_id, booking_id, event)
            VALUES (:id, current_setting('app.tenant_id')::uuid, :booking_id, 'created')
            """),
            {"id": uuid.uuid7(), "booking_id": booking_id},
        )
    assert isinstance(id_error.value.orig, InsufficientPrivilege)

    with app_engine.begin() as conn:  # positive control: the same insert, minus id/created_at
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text("""
            INSERT INTO booking_events (tenant_id, booking_id, event)
            VALUES (current_setting('app.tenant_id')::uuid, :booking_id, 'created')
            """),
            {"booking_id": booking_id},
        )


def load_migration(name: str) -> Any:
    """A migration module, loaded by path: its filename starts with a digit, so it cannot be
    `import`ed as a normal package member."""
    path = Path(__file__).parent.parent / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 9: FENCE, rewritten (F-b - revision 2's version was tautological and measured GREEN against its
# own injection). Wrong impl: shorten 0026's EVENT_COLUMNS to "tenant_id, booking_id, event".
# (§15.4's full cycle.)
def test_every_booking_event_column_but_id_and_created_at_is_insertable(
    migrate_engine: Engine,
) -> None:
    with migrate_engine.connect() as conn:
        all_columns = set(
            conn.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'booking_events'"
                )
            )
        )
        insertable = {
            column
            for column in all_columns
            if conn.scalar(
                text(
                    "SELECT has_column_privilege('ziftbook_app', 'booking_events', :col, 'INSERT')"
                ),
                {"col": column},
            )
        }
    # (a) An explicit literal, never the imported constant - the injection shrinks the grant and
    # the expectation together if this compares against EVENT_COLUMNS.
    assert insertable == {
        "tenant_id",
        "booking_id",
        "event",
        "ip",
        "user_agent",
        "policy_version",
        "consent_purposes",
    }
    # (b) Catches a NEW column silently gaining or missing a grant.
    assert all_columns - insertable == {"id", "created_at"}
    # (c) The migration constant is still honest.
    migration = load_migration("0026_bookings")
    assert set(migration.EVENT_COLUMNS.split(", ")) == insertable


# 10: FENCE + positive control. Wrong impl: any of the three composite FKs written plain,
# REFERENCES x (id). (§15.4's full cycle.)
def test_a_booking_cannot_point_at_another_businesses_client_worker_or_service(
    people: People,
) -> None:
    service_a = seed_service(people.a)
    client_a = seed_client(people.a, email=fresh_email())
    worker_a = member_id(people.a, people.both)
    service_b = seed_service(people.b)
    client_b = seed_client(people.b, email=fresh_email())
    worker_b = member_id(people.b, people.only_b)

    with pytest.raises(IntegrityError) as client_error:
        book(
            people.a,
            client_id=client_b,
            worker_id=worker_a,
            service_id=service_a,
            starts_at=T6[0],
            ends_at=T6[1],
        )
    assert isinstance(client_error.value.orig, ForeignKeyViolation)

    with pytest.raises(IntegrityError) as worker_error:
        book(
            people.a,
            client_id=client_a,
            worker_id=worker_b,
            service_id=service_a,
            starts_at=T6[0],
            ends_at=T6[1],
        )
    assert isinstance(worker_error.value.orig, ForeignKeyViolation)

    with pytest.raises(IntegrityError) as service_error:
        book(
            people.a,
            client_id=client_a,
            worker_id=worker_a,
            service_id=service_b,
            starts_at=T6[0],
            ends_at=T6[1],
        )
    assert isinstance(service_error.value.orig, ForeignKeyViolation)

    book(
        people.a,
        client_id=client_a,
        worker_id=worker_a,
        service_id=service_a,  # positive control
        starts_at=T6[0],
        ends_at=T6[1],
    )


# 11: FENCE. Wrong impl: drop fk_bookings_tenant_id_price_currency_tenants. (§15.4's full cycle.)
def test_the_price_currency_must_be_the_businesss_own(people: People) -> None:
    service_id = seed_service(people.a)
    client_id = seed_client(people.a, email=fresh_email())
    worker_id = member_id(people.a, people.both)

    with pytest.raises(IntegrityError) as error:
        book(
            people.a,
            client_id=client_id,
            worker_id=worker_id,
            service_id=service_id,
            starts_at=T7[0],
            ends_at=T7[1],
            price_currency="USD",
        )
    assert isinstance(error.value.orig, ForeignKeyViolation)


# 12: GUARD. The second half catches writing the check as a biconditional.
def test_a_pending_booking_must_carry_an_expiry_and_an_expired_one_keeps_it(
    people: People,
) -> None:
    service_id = seed_service(people.a)
    client_id = seed_client(people.a, email=fresh_email())
    worker_id = member_id(people.a, people.both)

    with pytest.raises(IntegrityError) as error:
        book(
            people.a,
            client_id=client_id,
            worker_id=worker_id,
            service_id=service_id,
            starts_at=T8[0],
            ends_at=T8[1],
            status="pending",
            expires_at=None,
        )
    assert isinstance(error.value.orig, CheckViolation)

    booking_id = book(
        people.a,
        client_id=client_id,
        worker_id=worker_id,
        service_id=service_id,
        starts_at=T8[0],
        ends_at=T8[1],
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with tenant_context(people.a) as session:  # a row that HAS an expiry may move to 'expired'
        session.execute(
            text("UPDATE bookings SET status = 'expired' WHERE id = :id"), {"id": booking_id}
        )


# 13: GUARD. Pins migration 0023's promise: no NOT NULL, no default, no uniqueness.
def test_the_worker_display_name_may_be_null_forever(people: People) -> None:
    service_id = seed_service(people.a)
    client_id = seed_client(people.a, email=fresh_email())
    worker_id = member_id(people.a, people.both)

    book(
        people.a,
        client_id=client_id,
        worker_id=worker_id,
        service_id=service_id,
        starts_at=T1[1],
        ends_at=T1[1] + timedelta(hours=1),
        display_name=None,
    )


# 21b: FENCE - the AC's other half, part 1 (§1.1). A raw insert with NO advisory lock still gets a
# clean 23P01 by constraint name; part 2 (the route's 409 slot_taken mapping) is
# tests/test_bookings_api.py's test_a_conflicting_row_committed_mid_request_is_a_409_slot_taken.
def test_a_raw_insert_that_bypasses_the_lock_raises_23P01_by_constraint_name(
    people: People,
) -> None:
    service_id = seed_service(people.a)
    client_id = seed_client(people.a, email=fresh_email())
    worker_id = member_id(people.a, people.both)
    book(
        people.a,
        client_id=client_id,
        worker_id=worker_id,
        service_id=service_id,
        starts_at=T2[0],
        ends_at=T2[1],
    )

    with pytest.raises(IntegrityError) as error:
        book(
            people.a,
            client_id=client_id,
            worker_id=worker_id,
            service_id=service_id,
            starts_at=T2[0],
            ends_at=T2[1],
        )

    assert isinstance(error.value.orig, ExclusionViolation)
    assert error.value.orig.diag.constraint_name == "ex_bookings_worker_overlap"
