"""The erase core (ZIF-58, migration 0064): erase_client_core blanks one client of the current
business and what its bookings hold about the person, and nothing else; erase_client lets only a
live owner session of that business run it; the contact hold is written only when the client
must still be told something.

The core is called here as ziftbook_migrate, its owner and the only role that may run it."""

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import Connection, Engine, TextClause, text
from sqlalchemy.exc import DBAPIError

from app import auth
from app.db import tenant_context
from tests.conftest import People, fresh_email, member_id, migrate_tenant_context, set_role
from tests.test_bookings_db import seed_service

# Every column of the six tables that hold a client's personal data or point at it, classified
# (spec 1). A column added to any of them fails test_every_column_is_classified until it is
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
    # C-DEL (owner ruling 5): a consent on a blank tombstone proves nothing about any person.
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
CORE = text("SELECT erase_client_core(:id)")
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
                              fee_mode, status, settled_at)
VALUES (current_setting('app.tenant_id')::uuid, :booking, 'pay', :intent, 2500, 'EUR',
        'business_pays', :status, :settled)
""")


def seed_person(
    migrate_engine: Engine,
    tenant_id: uuid.UUID,
    worker: uuid.UUID,
    actor: uuid.UUID,
    *,
    days_ago: int,
    email: str | None = "fresh",
    payment: tuple[str, bool] = ("paid", True),
) -> Seeded:
    """A client with every personal field set, a declined and a completed booking in the past
    (each with a client's and a team member's event and a manage link), two consents, and a payment
    on the completed booking: (status, settled)."""
    service_id = seed_service(tenant_id)
    address = fresh_email() if email == "fresh" else email
    starts = datetime.now().astimezone() - timedelta(days=days_ago)
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        client_id = conn.scalar(INSERT_CLIENT, {"email": address, "user_id": uuid.uuid7()})
        bookings = []
        for offset, status, decline in ((0, "declined", "Fully booked"), (1, "completed", None)):
            bookings.append(
                conn.scalar(
                    INSERT_BOOKING,
                    {
                        "client": client_id,
                        "worker": worker,
                        "service": service_id,
                        "starts": starts + timedelta(hours=offset),
                        "status": status,
                        "expires": None,
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
        status, settled = payment
        conn.execute(
            INSERT_PAYMENT,
            {
                "booking": bookings[1],
                "intent": f"pi_{uuid.uuid4().hex}",
                "status": status,
                "settled": datetime.now().astimezone() if settled else None,
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


def erase(migrate_engine: Engine, tenant_id: uuid.UUID, client_id: uuid.UUID) -> Any:
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
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
# the test's own transaction on clients and on each table in turn (the dependent rows are reached
# only through a client of this business, composite foreign keys, so clients is the gate), and B
# must still be untouched when A erases B's client id.
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


XMIN = text("SELECT xmin::text FROM clients WHERE id = :id")


# ZIF-58 F19 FENCE. Wrong impl: the clients UPDATE without its erased_at IS NULL qualifier (the
# final trigger then refuses the repeat). A repeat returns the first erasure's time and writes
# nothing.
def test_a_repeat_returns_the_first_erasure_and_writes_nothing(
    world: World, migrate_engine: Engine
) -> None:
    first = erase(migrate_engine, world.a.tenant_id, world.a.client_id)
    with migrate_tenant_context(migrate_engine, world.a.tenant_id) as conn:
        version = conn.scalar(XMIN, {"id": world.a.client_id})
    state = take(migrate_engine, world.a.tenant_id)

    assert erase(migrate_engine, world.a.tenant_id, world.a.client_id) == first

    with migrate_tenant_context(migrate_engine, world.a.tenant_id) as conn:
        assert conn.scalar(XMIN, {"id": world.a.client_id}) == version
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
            session.execute(ERASE_CLIENT, {"h": id_hash, "id": world.a.client_id})
        assert isinstance(error.value.orig, InsufficientPrivilege)
        assert erased_at(people.a, world.a.client_id) is None

    # Just inside the idle limit, a live owner session of A erases: the limit is auth.IDLE.
    owner = session_hash(people.a, people.both)
    age(migrate_engine, owner, "last_seen_at", auth.IDLE - timedelta(minutes=1))
    with tenant_context(people.a) as session:
        at = session.scalar(ERASE_CLIENT, {"h": owner, "id": world.a.client_id})
    assert at is not None
    assert erased_at(people.a, world.a.client_id) == at


POSTURE = text("""
SELECT p.proname, p.prosecdef, p.proconfig, p.proowner::regrole::text,
       has_function_privilege('public', p.oid, 'EXECUTE'),
       has_function_privilege('ziftbook_app', p.oid, 'EXECUTE')
FROM pg_proc p
WHERE p.oid IN ('erase_client_core(uuid)'::regprocedure, 'erase_client(bytea,uuid)'::regprocedure)
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
    migrate_engine: Engine, seeded: Seeded, actor: uuid.UUID, *, reason: str | None
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
        assert conn.scalar(CORE, {"id": seeded.client_id}) is not None
        return [tuple(row) for row in conn.execute(HOLDS)]


# ZIF-58 F22 FENCE (H1). Wrong impl: a core that writes a hold for every erased client with an
# address. A hold is written only when this erasure ended a booking of the client (an event with
# an erasure's reason in its own transaction) or money is still owed an answer on one of its
# bookings, and holds the address and locale only, for at most a month.
@pytest.mark.parametrize(
    ("case", "held"),
    [
        ("ended_now", True),
        ("money_owed", True),
        ("settled_long_ago", False),  # a paid completed booking: nothing owed
        ("paid_unsettled_but_kept", False),  # paid and kept: settle never touches it
        ("ended_earlier", False),  # an erasure reason, but not this transaction's
        ("reapplied", False),  # a re-applied erasure's reason: the contact window has passed
        ("no_address", False),
    ],
)
def test_the_contact_hold_is_written_only_when_the_client_must_be_told(
    people: People, migrate_engine: Engine, case: str, held: bool
) -> None:
    worker = member_id(people.a, people.only_a)
    payment = {"money_owed": ("open", False), "paid_unsettled_but_kept": ("paid", False)}
    seeded = seed_person(
        migrate_engine,
        people.a,
        worker,
        people.both,
        days_ago=10,
        email=None if case == "no_address" else "fresh",
        payment=payment.get(case, ("paid", True)),
    )
    if case == "money_owed":  # an open checkout on a booking that has ended
        with migrate_tenant_context(migrate_engine, people.a) as conn:
            conn.execute(
                text("UPDATE bookings SET status = 'cancelled_by_merchant' WHERE id = :id"),
                {"id": seeded.bookings[1]},
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
    reason = {"ended_now": "client_erased", "no_address": "client_erased"}.get(case)
    if case == "reapplied":
        reason = "erasure_reapplied"
    holds = hold_after(migrate_engine, seeded, people.both, reason=reason)

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
