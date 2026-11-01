"""The erase core (ZIF-58, migration 0064): erase_client_core blanks one client of the current
business and what its bookings hold about the person, and nothing else; erase_client lets only a
live owner session of that business run it; the contact hold is written only when the client
must still be told something.

The core is called here as ziftbook_migrate, its owner and the only role that may run it."""

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from psycopg.errors import InsufficientPrivilege, ObjectNotInPrerequisiteState
from sqlalchemy import Connection, Engine, TextClause, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import auth, bookings
from app.db import tenant_context
from tests.conftest import (
    People,
    delete_clients,
    fresh_email,
    member_id,
    migrate_tenant_context,
    set_role,
    wait_until_blocked,
)
from tests.test_bookings_db import seed_service

# Every column of the six tables that hold a client's personal data or point at it, classified
# (ZIF-58). A column added to any of them fails test_every_column_is_classified until it is
# classified here and, if it is "blank" or "delete", the core handles it.
#   blank: the core sets it to NULL ('' for clients.name); on booking_events only on the
#          client's own events (no actor), the team's keep the member's.
#   delete: the core deletes the client's rows.
#   keep: the core leaves it byte-identical (the financial and evidence line, and ids).
#   status: the erasure changes it: clients.erased_at (the core), bookings.status (the route ends
#           the live bookings before the core runs; the core changes no status).
# ZIF-146 adds bookings.booked_as, a key back to the account: "blank", and whichever of ZIF-58's
# 0064 and its migration merges second makes the core NULL it.
CLASSIFICATION: dict[str, dict[str, str]] = {
    "clients": {
        "id": "keep",
        "tenant_id": "keep",
        "user_id": "blank",
        "name": "blank",
        "email": "blank",
        "phone": "blank",
        "locale": "blank",
        "client_note": "blank",
        "internal_note": "blank",
        "erased_at": "status",
    },
    "bookings": {
        "id": "keep",
        "tenant_id": "keep",
        "client_id": "keep",
        "worker_id": "keep",
        "service_id": "keep",
        "starts_at": "keep",
        "ends_at": "keep",
        "status": "status",
        "expires_at": "keep",
        "source": "keep",
        "service_name": "keep",
        "price_amount_minor": "keep",
        "price_currency": "keep",
        "duration_minutes": "keep",
        "cancellation_policy_text": "keep",
        "auto_confirm_at_booking": "keep",
        "worker_display_name": "keep",
        "created_at": "keep",
        "free_cancellation_hours": "keep",
        "reschedule_cutoff_hours": "keep",
        "original_starts_at": "keep",
        "earliest_starts_at": "keep",
        "max_reschedules": "keep",
        "reschedule_count": "keep",
        "decline_message": "blank",
        "client_name": "blank",
        "client_phone": "blank",
        "client_message": "blank",
        "checkout_hash": "blank",
    },
    "booking_events": {
        "id": "keep",
        "tenant_id": "keep",
        "booking_id": "keep",
        "event": "keep",
        "ip": "blank",
        "user_agent": "blank",
        "policy_version": "keep",
        "consent_purposes": "keep",
        "created_at": "keep",
        "actor_user_id": "keep",
        "details": "keep",
    },
    # Deleted (ZIF-58): a consent on a blank tombstone proves nothing about any person.
    "consents": dict.fromkeys(
        (
            "id",
            "tenant_id",
            "client_id",
            "purpose",
            "granted",
            "text_shown",
            "policy_version",
            "source",
            "ip",
            "user_agent",
            "created_at",
        ),
        "delete",
    ),
    "booking_links": dict.fromkeys(
        ("token_hash", "tenant_id", "booking_id", "created_at"), "delete"
    ),
    "booking_payments": dict.fromkeys(
        (
            "id",
            "tenant_id",
            "booking_id",
            "kind",
            "intent_id",
            "amount_minor",
            "currency",
            "fee_mode",
            "application_fee_minor",
            "stripe_fee_minor",
            "status",
            "refunded_minor",
            "refund_id",
            "refund_failed_at",
            "settled_at",
            "created_at",
        ),
        "keep",
    ),
}
KEY = {"booking_links": "token_hash"}  # every other table's key is id
CORE = text("SELECT erase_client_core(:id, true)")
ERASE_CLIENT = text("SELECT erase_client(:h, :id)")
THIS_TENANT = "current_setting('app.tenant_id')::uuid"
SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")


@dataclass(frozen=True)
class Seeded:
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    bookings: tuple[uuid.UUID, ...]
    email: str


INSERT_CLIENT = text("""
INSERT INTO clients (tenant_id, name, email, phone, locale, client_note, internal_note, user_id)
VALUES (current_setting('app.tenant_id')::uuid, 'Seeded Person', :email, '+31600000000', 'nl',
        'Their note', 'Our note', :user_id)
RETURNING id
""")
INSERT_BOOKING = text("""
INSERT INTO bookings (tenant_id, client_id, worker_id, service_id, starts_at, ends_at, status,
                      expires_at, source, service_name, price_amount_minor, price_currency,
                      duration_minutes, auto_confirm_at_booking, free_cancellation_hours,
                      reschedule_cutoff_hours, worker_display_name, cancellation_policy_text,
                      client_name, client_phone, client_message, decline_message, checkout_hash)
VALUES (current_setting('app.tenant_id')::uuid, :client, :worker, :service, :starts,
        :starts + interval '30 minutes', :status, :expires, 'booking_page',
        '{"en": "Cut"}'::jsonb, 2500, 'EUR', 30, false, 48, 24, 'Worker Name', 'Policy text',
        'Typed Name', '+31611111111', 'Typed message', :decline, :checkout)
RETURNING id
""")
INSERT_EVENT = text("""
INSERT INTO booking_events (tenant_id, booking_id, event, ip, user_agent, policy_version,
                            consent_purposes, actor_user_id, details)
VALUES (current_setting('app.tenant_id')::uuid, :booking, :event, CAST(:ip AS inet), :ua,
        :policy, CAST(:purposes AS jsonb), :actor, CAST(:details AS jsonb))
""")
INSERT_CONSENT = text("""
INSERT INTO consents (tenant_id, client_id, purpose, granted, text_shown, policy_version, source,
                      ip, user_agent)
VALUES (current_setting('app.tenant_id')::uuid, :client, :purpose, true, 'Shown text', 'v1',
        'booking_page', '192.0.2.9', 'Client Browser/2')
""")
INSERT_LINK = text("""
INSERT INTO booking_links (token_hash, tenant_id, booking_id)
VALUES (:hash, current_setting('app.tenant_id')::uuid, :booking)
""")
INSERT_PAYMENT = text("""
INSERT INTO booking_payments (tenant_id, booking_id, kind, intent_id, amount_minor, currency,
                              fee_mode, status, settled_at, refund_failed_at)
VALUES (current_setting('app.tenant_id')::uuid, :booking, :kind, :intent, 2500, 'EUR',
        'business_pays', :status, :settled, :refund_failed)
""")


@dataclass(frozen=True)
class Pay:
    """The payment on the seeded client's second booking, and that booking's status."""

    kind: str = "pay"
    status: str = "paid"
    settled: bool = True
    refund_failed: bool = False
    booking: str = "completed"
    live_checkout: bool = False  # an awaiting_payment booking whose checkout is still running


PAID_AND_KEPT = Pay()


def seed_person(
    migrate_engine: Engine,
    tenant_id: uuid.UUID,
    worker: uuid.UUID,
    actor: uuid.UUID,
    *,
    days_ago: int,
    email: str | None = "fresh",
    payment: Pay = PAID_AND_KEPT,
) -> Seeded:
    """A client with every personal field set, a declined and a second booking in the past (each
    with a client's and a team member's event and a manage link), two consents, and a payment on
    the second booking, which is completed and paid and settled unless `payment` says otherwise."""
    service_id = seed_service(tenant_id)
    address = fresh_email() if email == "fresh" else email
    starts = datetime.now().astimezone() - timedelta(days=days_ago)
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        client_id = conn.scalar(INSERT_CLIENT, {"email": address, "user_id": uuid.uuid7()})
        bookings = []
        for offset, status, decline in (
            (0, "declined", "Fully booked"),
            (1, payment.booking, None),
        ):
            expiring = status in ("pending", "awaiting_payment")
            live = timedelta(minutes=30 if payment.live_checkout else -30)
            bookings.append(
                conn.scalar(
                    INSERT_BOOKING,
                    {
                        "client": client_id,
                        "worker": worker,
                        "service": service_id,
                        "starts": starts + timedelta(hours=offset),
                        "status": status,
                        "expires": datetime.now().astimezone() + live if expiring else None,
                        "decline": decline,
                        "checkout": os.urandom(32),
                    },
                )
            )
        for booking in bookings:
            conn.execute(
                INSERT_EVENT,
                {
                    "booking": booking,
                    "event": "created",
                    "ip": "192.0.2.1",
                    "ua": "Client Browser/1",
                    "policy": "v1",
                    "purposes": json.dumps({"marketing_email": True}),
                    "actor": None,
                    "details": None,
                },
            )
            conn.execute(
                INSERT_EVENT,
                {
                    "booking": booking,
                    "event": "note",
                    "ip": "198.51.100.7",
                    "ua": "Team Browser/1",
                    "policy": None,
                    "purposes": None,
                    "actor": actor,
                    "details": json.dumps({"kept": True}),
                },
            )
            conn.execute(INSERT_LINK, {"hash": os.urandom(32), "booking": booking})
        for purpose in ("marketing_email", "sms"):
            conn.execute(INSERT_CONSENT, {"client": client_id, "purpose": purpose})
        now = datetime.now().astimezone()
        conn.execute(
            INSERT_PAYMENT,
            {
                "booking": bookings[1],
                "kind": payment.kind,
                "intent": f"pi_{uuid.uuid4().hex}",
                "status": payment.status,
                "settled": now if payment.settled else None,
                "refund_failed": now if payment.refund_failed else None,
            },
        )
    return Seeded(tenant_id, client_id, tuple(bookings), address or "")


@dataclass(frozen=True)
class World:
    a: Seeded  # the client erased
    bystander: Seeded  # another client of the same business
    b: Seeded  # a client of another business


@pytest.fixture
def world(people: People, migrate_engine: Engine) -> World:
    in_a = member_id(people.a, people.only_a)
    in_b = member_id(people.b, people.only_b)
    return World(
        a=seed_person(migrate_engine, people.a, in_a, people.both, days_ago=10),
        bystander=seed_person(migrate_engine, people.a, in_a, people.both, days_ago=20),
        b=seed_person(migrate_engine, people.b, in_b, people.only_b, days_ago=10),
    )


Snapshot = dict[str, dict[Any, dict[str, Any]]]


def snapshot(conn: Connection, tenant_id: uuid.UUID) -> Snapshot:
    """Every row of the six tables in that business, by key, as stored."""
    conn.execute(SET_TENANT, {"t": str(tenant_id)})
    return {
        table: {
            row[KEY.get(table, "id")]: dict(row)
            # The tenant by name too: F17b widens the policies inside the transaction.
            for row in conn.execute(
                text(f"SELECT * FROM {table} WHERE tenant_id = {THIS_TENANT}")
            ).mappings()
        }
        for table in CLASSIFICATION
    }


def take(migrate_engine: Engine, tenant_id: uuid.UUID) -> Snapshot:
    with migrate_engine.begin() as conn:
        return snapshot(conn, tenant_id)


def booking_lock(conn: Connection | Session) -> None:
    """The tenant booking lock, which every caller of the core takes first."""
    conn.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})


def erase(migrate_engine: Engine, tenant_id: uuid.UUID, client_id: uuid.UUID) -> Any:
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        booking_lock(conn)
        return conn.scalar(CORE, {"id": client_id})


def owned(table: str, row: dict[str, Any], seeded: Seeded) -> bool:
    """Whether a row is the seeded client's or one of its bookings'."""
    if table == "clients":
        return bool(row["id"] == seeded.client_id)
    if table == "consents":
        return bool(row["client_id"] == seeded.client_id)
    if table == "bookings":
        return bool(row["id"] in seeded.bookings)
    return bool(row["booking_id"] in seeded.bookings)


# ZIF-58 F14 FENCE (catalog). Wrong impl: a column added to one of the six tables with no
# classification here, so the core may leave it holding the person.
def test_every_column_is_classified(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        rows = conn.execute(
            text("""
            SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ANY(:tables)
            """),
            {"tables": list(CLASSIFICATION)},
        ).all()
    found: dict[str, set[str]] = {}
    for table, column in rows:
        found.setdefault(table, set()).add(column)
    assert found == {table: set(columns) for table, columns in CLASSIFICATION.items()}


# Tables that reach a client or a booking by foreign key, directly or through another such table,
# but are not in CLASSIFICATION, each with its reason.
CHILDREN_ALLOWED = {
    # The erased client's address for at most a month, the only key back by design; the worker's
    # purge deletes it.
    "client_erasure_holds": "the bounded contact hold",
}
# Columns that name a client or a booking by id, keyed or not, each with its reason.
ID_COLUMNS_ALLOWED = {
    ("bookings", "client_id"): "classified",
    ("consents", "client_id"): "classified",
    ("booking_events", "booking_id"): "classified",
    ("booking_links", "booking_id"): "classified",
    ("booking_payments", "booking_id"): "classified",
    ("client_erasure_holds", "client_id"): "the bounded contact hold",
    # The client or member a mail went to: an id, with a template and a subject that never name
    # anyone. Kept as the record of what was sent.
    ("email_outbox", "recipient_id"): "an id only",
}
# clients and bookings, and every table with a foreign key into one of them or into a table found
# so far: a grandchild (a table keyed to booking_payments, say) is reached too.
REACHABLE_TABLES = text("""
WITH RECURSIVE reached(t) AS (
  SELECT 'clients'::regclass UNION SELECT 'bookings'::regclass
  UNION
  SELECT c.conrelid::regclass FROM pg_constraint c JOIN reached r ON c.confrelid = r.t
  WHERE c.contype = 'f'
)
SELECT t::text FROM reached
""")
ID_COLUMNS = text("""
SELECT table_name, column_name FROM information_schema.columns
WHERE table_schema = 'public' AND column_name IN ('client_id', 'booking_id', 'recipient_id')
""")


# ZIF-58 F14 FENCE (catalog, tables). Wrong impl: a new table that holds a client's data and reaches
# a client or a booking by foreign key, directly or through another table (a child of
# booking_payments, say), or names one by id (client_id, booking_id, recipient_id) without a key:
# the core would never touch it, so erasure would be silently incomplete. Every such table is
# classified above or allowed here with its reason.
def test_every_table_that_points_at_a_client_is_classified(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        reached = set(conn.scalars(REACHABLE_TABLES))
        columns = {(table, column) for table, column in conn.execute(ID_COLUMNS)}
    assert reached == set(CLASSIFICATION) | set(CHILDREN_ALLOWED)
    assert columns == set(ID_COLUMNS_ALLOWED)


# ZIF-58 F15 FENCE (and F17c). Wrong impl: a core that forgets decline_message, checkout_hash, the
# client's events or the consents rows, that blanks the team's events too, or that touches another
# client of the business. Parametrised by CLASSIFICATION: each "blank" column was set before and is
# NULL after, each "delete" row is gone, each "keep" column is byte-identical, and every row that
# is not the erased client's is untouched.
def test_one_call_erases_the_client_and_keeps_the_evidence(
    world: World, migrate_engine: Engine
) -> None:
    before = take(migrate_engine, world.a.tenant_id)
    other = take(migrate_engine, world.b.tenant_id)

    erased_at = erase(migrate_engine, world.a.tenant_id, world.a.client_id)

    after = take(migrate_engine, world.a.tenant_id)
    assert erased_at is not None
    assert take(migrate_engine, world.b.tenant_id) == other
    blanked: set[tuple[str, str]] = set()
    for table, columns in CLASSIFICATION.items():
        for key, row in before[table].items():
            if not owned(table, row, world.a):
                assert after[table].get(key) == row, (table, key)  # the bystander: untouched
                continue
            if "delete" in columns.values():
                assert key not in after[table], (table, key)
                continue
            for column, action in columns.items():
                was, now = row[column], after[table][key][column]
                team = table == "booking_events" and row["actor_user_id"] is not None
                if action == "blank" and not team:
                    if was is not None:
                        blanked.add((table, column))
                    assert now == ("" if (table, column) == ("clients", "name") else None), (
                        table,
                        column,
                    )
                elif (table, column) == ("clients", "erased_at"):
                    assert (was, now) == (None, erased_at)
                else:  # keep, a team event's ip and browser, and bookings.status
                    assert now == was, (table, column)
    # Not vacuous: every "blank" column held something before the call.
    assert blanked == {
        (table, column)
        for table, columns in CLASSIFICATION.items()
        for column, action in columns.items()
        if action == "blank"
    }
    for table in ("consents", "booking_links"):
        assert any(owned(table, row, world.a) for row in before[table].values()), table


POLICIES = text("""
SELECT tablename, policyname, roles::text, cmd FROM pg_policies
WHERE schemaname = 'public' AND tablename = ANY(:tables)
ORDER BY tablename, policyname
""")
SCOPED = (
    "booking_events",
    "booking_links",
    "bookings",
    "client_erasure_holds",
    "clients",
    "consents",
)


# ZIF-58 F16 FENCE. Wrong impl: any policy beyond tenant_isolation on a table the core writes, a
# TO ziftbook_migrate one above all: FORCE row-level security is half of what keeps the core inside
# one business, the explicit predicates the other.
def test_the_tables_the_core_writes_have_only_tenant_isolation(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        rows = conn.execute(POLICIES, {"tables": list(SCOPED)}).all()
    assert [tuple(row) for row in rows] == [
        (table, "tenant_isolation", "{public}", "ALL") for table in SCOPED
    ]


# ZIF-58 F17a FENCE. Wrong impl: a core that finds the client's business by walking the
# businesses (set_config per tenant until the id is found), so business A erases B's client.
def test_another_businesss_client_is_unknown(world: World, migrate_engine: Engine) -> None:
    a = take(migrate_engine, world.a.tenant_id)
    b = take(migrate_engine, world.b.tenant_id)

    assert erase(migrate_engine, world.a.tenant_id, world.b.client_id) is None

    assert take(migrate_engine, world.a.tenant_id) == a
    assert take(migrate_engine, world.b.tenant_id) == b


# ZIF-58 F17b FENCE. Wrong impl: the core without its explicit tenant predicates, relying on
# row-level security alone. A policy for ziftbook_migrate that sees every business is created in
# the test's own transaction, on clients and on each table in turn, and B must still be untouched
# when A erases B's client id. What this can and cannot tell apart: a client id is a global
# primary key, every dependent row reaches its client through a composite foreign key, and the
# core returns at the clients gate when the id is not this business's. So the clients
# statements' predicates are the guarantee, and the five cases go red only without them; the
# predicates on the dependent statements are defence in depth that no test can tell apart, and
# removing one of them alone stays green.
@pytest.mark.parametrize(
    "table", ["clients", "bookings", "booking_events", "consents", "booking_links"]
)
def test_a_widened_policy_does_not_reach_another_business(
    world: World, migrate_engine: Engine, table: str
) -> None:
    with migrate_engine.connect() as conn, conn.begin() as transaction:
        before = snapshot(conn, world.b.tenant_id)
        for widened in {"clients", table}:
            conn.execute(
                text(f"CREATE POLICY probe ON {widened} FOR ALL TO ziftbook_migrate USING (true)")
            )
        conn.execute(SET_TENANT, {"t": str(world.a.tenant_id)})
        booking_lock(conn)
        result = conn.scalar(CORE, {"id": world.b.client_id})
        after = snapshot(conn, world.b.tenant_id)
        transaction.rollback()
    assert result is None
    assert after == before


# ZIF-58 F18 FENCE. Wrong impl: no guard, so a call with no business fails on the policy's cast
# (22P02) instead of a refusal naming what is missing.
@pytest.mark.parametrize("tenant", [None, ""])
def test_no_business_is_refused(
    world: World, migrate_engine: Engine, app_engine: Engine, tenant: str | None
) -> None:
    calls: list[tuple[Engine, TextClause, dict[str, Any]]] = [
        (migrate_engine, CORE, {"id": world.a.client_id}),
        (app_engine, ERASE_CLIENT, {"h": os.urandom(32), "id": world.a.client_id}),
    ]
    for engine, call, params in calls:
        with pytest.raises(DBAPIError) as error, engine.begin() as conn:
            if tenant is not None:
                conn.execute(SET_TENANT, {"t": tenant})
            conn.execute(call, params)
        assert isinstance(error.value.orig, InsufficientPrivilege)
        assert "erase_client needs a business" in str(error.value.orig)


# The row versions of a client, its bookings and their events: a repeat that rewrote any of them
# with the same NULLs would leave every value equal and every version new.
XMINS = text("""
SELECT 'clients', id::text, xmin::text FROM clients WHERE id = :id
UNION ALL
SELECT 'bookings', id::text, xmin::text FROM bookings WHERE client_id = :id
UNION ALL
SELECT 'booking_events', e.id::text, e.xmin::text
FROM booking_events e JOIN bookings b ON b.id = e.booking_id WHERE b.client_id = :id
ORDER BY 1, 2
""")


# ZIF-58 F19 FENCE. Wrong impl: the clients UPDATE without its erased_at IS NULL qualifier (the
# final trigger then refuses the repeat), or the bookings or events UPDATE without its "something
# left to blank" qualifier (a new version of every row, the same values). A repeat returns the
# first erasure's time and writes nothing.
def test_a_repeat_returns_the_first_erasure_and_writes_nothing(
    world: World, migrate_engine: Engine
) -> None:
    first = erase(migrate_engine, world.a.tenant_id, world.a.client_id)
    with migrate_tenant_context(migrate_engine, world.a.tenant_id) as conn:
        versions = conn.execute(XMINS, {"id": world.a.client_id}).all()
    state = take(migrate_engine, world.a.tenant_id)

    assert erase(migrate_engine, world.a.tenant_id, world.a.client_id) == first

    with migrate_tenant_context(migrate_engine, world.a.tenant_id) as conn:
        assert conn.execute(XMINS, {"id": world.a.client_id}).all() == versions
    assert {table for table, _, _ in versions} == {"clients", "bookings", "booking_events"}
    assert take(migrate_engine, world.a.tenant_id) == state


def session_hash(tenant_id: uuid.UUID, user_id: uuid.UUID) -> bytes:
    with tenant_context(tenant_id) as session:
        token = auth.create(session, user_id, ip=None, user_agent=None)
    return auth.hash_token(token)


def age(migrate_engine: Engine, id_hash: bytes, column: str, by: timedelta) -> None:
    with migrate_engine.begin() as conn:
        conn.execute(
            text(f"UPDATE sessions SET {column} = now() - :by WHERE id_hash = :h"),
            {"by": by, "h": id_hash},
        )


def erased_at(tenant_id: uuid.UUID, client_id: uuid.UUID) -> Any:
    with tenant_context(tenant_id) as session:
        return session.scalar(
            text("SELECT erased_at FROM clients WHERE id = :id"), {"id": client_id}
        )


# ZIF-58 F20 FENCE. Wrong impl: a wrapper without the session check, or one that ignores the
# session's business or its role. As the app role, which runs erase_client and not the core.
def test_only_a_live_owner_session_of_this_business_erases(
    people: People, world: World, migrate_engine: Engine
) -> None:
    worker = session_hash(people.a, people.only_a)
    set_role(people.b, people.only_b, "owner")
    other_owner = session_hash(people.b, people.only_b)
    expired = session_hash(people.a, people.both)
    age(migrate_engine, expired, "expires_at", timedelta(seconds=1))
    idle = session_hash(people.a, people.both)
    age(migrate_engine, idle, "last_seen_at", auth.IDLE + timedelta(minutes=1))
    refused = [worker, other_owner, expired, idle, os.urandom(32)]

    for id_hash in refused:
        with pytest.raises(DBAPIError) as error, tenant_context(people.a) as session:
            booking_lock(session)
            session.execute(ERASE_CLIENT, {"h": id_hash, "id": world.a.client_id})
        assert isinstance(error.value.orig, InsufficientPrivilege)
        assert erased_at(people.a, world.a.client_id) is None

    # Just inside the idle limit, a live owner session of A erases: the limit is auth.IDLE.
    owner = session_hash(people.a, people.both)
    age(migrate_engine, owner, "last_seen_at", auth.IDLE - timedelta(minutes=1))
    with tenant_context(people.a) as session:
        booking_lock(session)
        at = session.scalar(ERASE_CLIENT, {"h": owner, "id": world.a.client_id})
    assert at is not None
    assert erased_at(people.a, world.a.client_id) == at


POSTURE = text("""
SELECT p.proname, p.prosecdef, p.proconfig, p.proowner::regrole::text,
       has_function_privilege('public', p.oid, 'EXECUTE'),
       has_function_privilege('ziftbook_app', p.oid, 'EXECUTE')
FROM pg_proc p
WHERE p.oid IN ('erase_client_core(uuid,boolean)'::regprocedure,
                'erase_client(bytea,uuid)'::regprocedure)
ORDER BY 1
""")
APPEND_ONLY = text("""
SELECT t, has_any_column_privilege('ziftbook_app', t, 'UPDATE'),
       has_table_privilege('ziftbook_app', t, 'DELETE')
FROM unnest(ARRAY['booking_events', 'consents']) t ORDER BY 1
""")
HOLD_GRANTS = text("""
SELECT r, has_any_column_privilege(r, 'client_erasure_holds', 'SELECT'),
       has_any_column_privilege(r, 'client_erasure_holds', 'INSERT'),
       has_any_column_privilege(r, 'client_erasure_holds', 'UPDATE'),
       has_table_privilege(r, 'client_erasure_holds', 'DELETE')
FROM unnest(ARRAY['ziftbook_app', 'ziftbook_worker']) r ORDER BY 1
""")


# ZIF-58 F21 FENCE. Wrong impl: the grants route (UPDATE on booking_events or consents for the app
# role instead of a definer), a definer without SET search_path, or the core executable by the
# app role. The DEFINERS pin (tests/test_directory_ban.py) gains erase_client alone.
def test_the_erase_functions_posture(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        assert [tuple(row) for row in conn.execute(POSTURE)] == [
            (
                "erase_client",
                True,
                ["search_path=pg_catalog, public, pg_temp"],
                "ziftbook_migrate",
                False,
                True,
            ),
            (
                "erase_client_core",
                True,
                ["search_path=pg_catalog, public, pg_temp"],
                "ziftbook_migrate",
                False,
                False,
            ),
        ]
        assert [tuple(row) for row in conn.execute(APPEND_ONLY)] == [
            ("booking_events", False, False),
            ("consents", False, False),
        ]
        # The hold: the API neither reads nor writes it; the worker reads and deletes it.
        assert [tuple(row) for row in conn.execute(HOLD_GRANTS)] == [
            ("ziftbook_app", False, False, False, False),
            ("ziftbook_worker", True, False, False, True),
        ]


HOLDS = text("""
SELECT client_id, email, locale, purge_after - created_at <= interval '31 days' AS bounded,
       purge_after > now() + interval '29 days' AS month
FROM client_erasure_holds
""")
HOLD_COLUMNS = text("""
SELECT array_agg(column_name::text ORDER BY column_name) FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'client_erasure_holds'
""")
ENDED_BY_ERASURE = text("""
INSERT INTO booking_events (tenant_id, booking_id, event, actor_user_id, details)
VALUES (current_setting('app.tenant_id')::uuid, :booking, 'cancelled_by_merchant', :actor,
        CAST(:details AS jsonb))
""")


def hold_after(
    migrate_engine: Engine,
    seeded: Seeded,
    actor: uuid.UUID,
    *,
    reason: str | None,
    announce: bool,
) -> list[Any]:
    """Erase the client, first writing (in the same transaction) an event that ends one of its
    bookings with this reason, when one is given; the holds of that business after."""
    with migrate_tenant_context(migrate_engine, seeded.tenant_id) as conn:
        if reason is not None:
            conn.execute(
                ENDED_BY_ERASURE,
                {
                    "booking": seeded.bookings[0],
                    "actor": actor,
                    "details": json.dumps({"reason": reason}),
                },
            )
        booking_lock(conn)
        erased = conn.scalar(
            text("SELECT erase_client_core(:id, :announce)"),
            {"id": seeded.client_id, "announce": announce},
        )
        assert erased is not None
        return [tuple(row) for row in conn.execute(HOLDS)]


# ZIF-58 F22 FENCE. The hold is written when the client has an address and either this
# erasure ended a booking the client is to be told about (p_announce, and an event with an
# erasure's reason in its own transaction) or money is still owed an answer on one of its bookings,
# whatever p_announce says: a re-applied erasure (p_announce false) announces no cancellation
# again, but a refund that fails later must still reach the client. It holds the address and
# locale only, for at most a month. Wrong impl: a core that writes a hold for every erased client
# with an address; one that ignores p_announce; one that lets p_announce gate the money too.
@pytest.mark.parametrize(
    ("case", "announce", "held"),
    [
        ("ended_now", True, True),
        ("ended_now", False, False),  # a re-apply: the cancellation was announced the first time
        ("money_owed", True, True),
        ("money_owed", False, True),  # a re-apply with money owed: a failed refund needs a channel
        ("nothing_owed", False, False),
        ("settled_long_ago", True, False),  # a paid completed booking: nothing owed
        ("paid_unsettled_but_kept", True, False),  # paid and kept: settle never touches it
        ("ended_earlier", True, False),  # an erasure reason, but not this transaction's
        ("no_address", True, False),
    ],
)
def test_the_contact_hold_is_written_only_when_the_client_must_be_told(
    people: People, migrate_engine: Engine, case: str, announce: bool, held: bool
) -> None:
    worker = member_id(people.a, people.only_a)
    payment = {
        "money_owed": Pay(status="open", settled=False, booking="cancelled_by_merchant"),
        "paid_unsettled_but_kept": Pay(settled=False),
    }
    seeded = seed_person(
        migrate_engine,
        people.a,
        worker,
        people.both,
        days_ago=10,
        email=None if case == "no_address" else "fresh",
        payment=payment.get(case, PAID_AND_KEPT),
    )
    if case == "ended_earlier":
        with migrate_tenant_context(migrate_engine, people.a) as conn:
            conn.execute(
                ENDED_BY_ERASURE,
                {
                    "booking": seeded.bookings[0],
                    "actor": people.both,
                    "details": json.dumps({"reason": "client_erased"}),
                },
            )
    reason = "client_erased" if case in ("ended_now", "no_address") else None
    holds = hold_after(migrate_engine, seeded, people.both, reason=reason, announce=announce)

    assert holds == ([(seeded.client_id, seeded.email, "nl", True, True)] if held else [])
    with migrate_engine.connect() as conn:
        assert conn.scalar(HOLD_COLUMNS) == [
            "client_id",
            "created_at",
            "email",
            "locale",
            "purge_after",
            "tenant_id",
        ]


OWED = text("SELECT client_money_owed(CAST(:t AS uuid), :c)")


# ZIF-58 F22 FENCE, money still owed an answer: one rule, the SQL function client_money_owed, which
# the core's hold uses and the hold's purge will use, so the two cannot drift. A payment is owed an
# answer while it is unsettled and the settle pass still has work on it (payments.UNSETTLED: an
# open checkout that is no longer running, a card hold, a paid booking that ended with a refund),
# or when its refund failed. A paid booking that is kept is never owed. With p_announce false, so
# only the money decides the hold. Wrong impl: any arm left out (paid on each refunding ending,
# checked, open, a failed refund), or an open checkout still running counted as owed.
@pytest.mark.parametrize(
    ("payment", "owed"),
    [
        *[(Pay(settled=False, booking=ending), True) for ending in bookings.REFUNDING],
        (Pay(kind="check", status="checked", settled=False), True),
        (Pay(status="open", settled=False, booking="awaiting_payment"), True),
        (Pay(status="open", settled=False, booking="awaiting_payment", live_checkout=True), False),
        (Pay(booking="cancelled_by_merchant", refund_failed=True), True),
        (Pay(booking="cancelled_by_merchant"), False),  # refunded and settled
        (Pay(settled=False), False),  # paid and kept
    ],
    ids=lambda value: repr(value) if isinstance(value, Pay) else str(value),
)
def test_money_still_owed_is_one_rule_and_decides_the_hold(
    people: People, migrate_engine: Engine, payment: Pay, owed: bool
) -> None:
    seeded = seed_person(
        migrate_engine,
        people.a,
        member_id(people.a, people.only_a),
        people.both,
        days_ago=10,
        payment=payment,
    )
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        assert conn.scalar(OWED, {"t": str(people.a), "c": seeded.client_id}) is owed

    if payment.live_checkout:
        return  # a live booking: the core refuses to erase until the caller ends it
    holds = hold_after(migrate_engine, seeded, people.both, reason=None, announce=False)

    assert holds == ([(seeded.client_id, seeded.email, "nl", True, True)] if owed else [])


LOCKED_TENANT = text("""
INSERT INTO tenants (id, name) VALUES (:t, 'Hashed') RETURNING hashtext(id::text) < 0
""")


def tenant_hashing(migrate_engine: Engine, negative: bool) -> uuid.UUID:
    """A new business whose id hashes (hashtext, as the booking lock keys it) to that sign."""
    while True:
        candidate = uuid.uuid7()
        with migrate_engine.begin() as conn:
            if conn.scalar(text("SELECT hashtext(:t) < 0"), {"t": str(candidate)}) is negative:
                conn.execute(LOCKED_TENANT, {"t": candidate})
                return candidate


# ZIF-58 FENCE. Wrong impl: a core that trusts its caller to hold the tenant booking lock. Every
# booking writer takes it first, and the console's booking of a client relies on it to see an
# erasure whole; a caller that forgot it would let a booking or a client's link action interleave
# with the erasure. The core reads pg_locks for this backend's (51, hashtext(tenant)) lock: the
# second key is signed, so a business whose id hashes negative is tried as well as one positive.
@pytest.mark.parametrize("negative", [True, False], ids=["negative hash", "positive hash"])
def test_the_core_needs_the_booking_lock(migrate_engine: Engine, negative: bool) -> None:
    tenant_id = tenant_hashing(migrate_engine, negative)
    try:
        with migrate_tenant_context(migrate_engine, tenant_id) as conn:
            client_id = conn.scalar(INSERT_CLIENT, {"email": fresh_email(), "user_id": None})
        with (
            pytest.raises(DBAPIError) as error,
            migrate_tenant_context(migrate_engine, tenant_id) as conn,
        ):
            conn.scalar(CORE, {"id": client_id})
        assert isinstance(error.value.orig, ObjectNotInPrerequisiteState)
        assert "needs the booking lock" in str(error.value.orig)
        assert erase(migrate_engine, tenant_id, client_id) is not None
    finally:
        with migrate_engine.begin() as conn:
            delete_clients(conn, [tenant_id])
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})


LIVE = {
    "confirmed, to come": ("confirmed", timedelta(days=2), None),
    "pending, waiting": ("pending", timedelta(days=2), timedelta(hours=1)),
    "checkout, running": ("awaiting_payment", timedelta(days=2), timedelta(minutes=10)),
}
NOT_LIVE = {
    "confirmed, under way": ("confirmed", timedelta(minutes=-10), None),
    "pending, lapsed": ("pending", timedelta(days=2), timedelta(hours=-1)),
}


# ZIF-58 FENCE. Wrong impl: a core that trusts its caller to end the client's live bookings first.
# A booking still to come on a tombstone has no address to be told at, no manage link, and no
# later erasure can end it. A booking already under way, or a pending one whose hold has lapsed,
# is not live: an erasure keeps the first and the sweep ends the second.
@pytest.mark.parametrize("kind", [*LIVE, *NOT_LIVE])
def test_the_core_refuses_a_client_with_a_live_booking(
    people: People, migrate_engine: Engine, kind: str
) -> None:
    status, starts_in, expires_in = {**LIVE, **NOT_LIVE}[kind]
    worker = member_id(people.a, people.only_a)
    service_id = seed_service(people.a)
    now = datetime.now().astimezone()
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        client_id = conn.scalar(INSERT_CLIENT, {"email": fresh_email(), "user_id": None})
        conn.execute(
            INSERT_BOOKING,
            {
                "client": client_id,
                "worker": worker,
                "service": service_id,
                "starts": now + starts_in,
                "status": status,
                "expires": now + expires_in if expires_in is not None else None,
                "decline": None,
                "checkout": None,
            },
        )
    before = take(migrate_engine, people.a)

    if kind in LIVE:
        with pytest.raises(DBAPIError) as error:
            erase(migrate_engine, people.a, client_id)
        assert isinstance(error.value.orig, ObjectNotInPrerequisiteState)
        assert "live bookings" in str(error.value.orig)
        assert take(migrate_engine, people.a) == before
    else:
        assert erase(migrate_engine, people.a, client_id) is not None


# ZIF-58 FENCE. Wrong impl: erase_client reads the owner's session without locking it. A role
# change or a removal deletes the member's sessions first, then changes the membership, and takes
# no booking lock; an erasure that read the session just before that commit would proceed for
# someone no longer an owner. With FOR KEY SHARE it waits for the delete and then finds nothing.
def test_an_erasure_waits_for_its_session_being_ended(
    people: People, world: World, migrate_engine: Engine, app_engine: Engine
) -> None:
    owner = session_hash(people.a, people.both)
    refused: dict[str, BaseException] = {}

    def erase_as_owner() -> None:
        try:
            with tenant_context(people.a) as session:
                booking_lock(session)
                session.execute(ERASE_CLIENT, {"h": owner, "id": world.a.client_id})
        except DBAPIError as error:
            refused["error"] = error

    eraser = threading.Thread(target=erase_as_owner)
    with migrate_engine.begin() as conn:
        conn.execute(text("DELETE FROM sessions WHERE id_hash = :h"), {"h": owner})
        eraser.start()
        wait_until_blocked(app_engine, 1)
    eraser.join(timeout=10)
    assert not eraser.is_alive()

    error = refused["error"]
    assert isinstance(error, DBAPIError)
    assert isinstance(error.orig, InsufficientPrivilege)
    assert erased_at(people.a, world.a.client_id) is None


# ZIF-58 FENCE. Wrong impl: erase_client passes p_announce false to the core, so an owner's
# erasure that cancels a client's booking and owes nothing writes no hold, and the client is
# never told of the cancellation. Through erase_client as the app role, with an owner's session.
def test_an_owners_erasure_announces_the_cancellations(
    people: People, migrate_engine: Engine
) -> None:
    seeded = seed_person(
        migrate_engine, people.a, member_id(people.a, people.only_a), people.both, days_ago=10
    )
    owner = session_hash(people.a, people.both)
    with tenant_context(people.a) as session:
        booking_lock(session)
        # The ending the erase route writes for a booking it cancels, in the same transaction.
        session.execute(
            ENDED_BY_ERASURE,
            {
                "booking": seeded.bookings[0],
                "actor": people.both,
                "details": json.dumps({"reason": "client_erased"}),
            },
        )
        assert session.scalar(ERASE_CLIENT, {"h": owner, "id": seeded.client_id}) is not None
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        assert conn.scalar(OWED, {"t": str(people.a), "c": seeded.client_id}) is False
        holds = [tuple(row) for row in conn.execute(HOLDS)]
    assert holds == [(seeded.client_id, seeded.email, "nl", True, True)]
