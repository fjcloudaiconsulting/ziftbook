"""A migration's downgrade must undo exactly what its upgrade did, grants included."""

import importlib.util
import os
import re
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any
from urllib.parse import urlencode

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from psycopg.errors import CheckViolation, NotNullViolation
from sqlalchemy import Connection, Engine, create_engine, make_url, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.db import tenant_context
from tests.conftest import (
    ADMIN_DATABASE_URL,
    API_DIR,
    People,
    delete_bookings,
    end_membership,
    member_id,
)
from tests.test_bookings_db import insert_booking, seed_service

COLUMNS = text("SELECT column_name FROM information_schema.columns WHERE table_name = 'tenants'")
TABLE_PRIVILEGE = "SELECT has_table_privilege('ziftbook_app', 'tenants', :priv)"
COLUMN_PRIVILEGE = "SELECT has_column_privilege('ziftbook_app', 'tenants', 'name', 'UPDATE')"
NAME_ACL = """
SELECT attacl FROM pg_attribute WHERE attrelid = 'tenants'::regclass AND attname = 'name'
"""
FIVE_ARG = "SELECT to_regprocedure('complete_sign_up(bytea,text,text,text,text)')"
SIX_ARG = "SELECT to_regprocedure('complete_sign_up(bytea,text,text,text,text,text)')"
THREE_ARG = "SELECT to_regprocedure('complete_sign_up(bytea,text,text)')"
THREE_ARG_TO_APP = (
    "SELECT has_function_privilege('ziftbook_app', 'complete_sign_up(bytea,text,text)', 'EXECUTE')"
)
THREE_ARG_TO_PUBLIC = (
    "SELECT has_function_privilege('public', 'complete_sign_up(bytea,text,text)', 'EXECUTE')"
)
INVITES_TABLE = "SELECT to_regclass('invites')"
ACCEPT_INVITE = "SELECT to_regprocedure('accept_invite(bytea,text,text)')"
INVITES_PK = """
SELECT conname FROM pg_constraint
WHERE conrelid = 'invites'::regclass AND contype = 'p'
"""


def test_downgrading_and_upgrading_0013_restores_its_columns_and_grants(
    migrated: None, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    # A lock held elsewhere (another test, a stray session) must not hang this one forever.
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    try:
        command.downgrade(cfg, "0012")
        with migrate_engine.connect() as conn:
            assert {"country", "currency"}.isdisjoint(set(conn.scalars(COLUMNS)))
            assert conn.scalar(text(TABLE_PRIVILEGE), {"priv": "UPDATE"})
            assert conn.scalar(text(TABLE_PRIVILEGE), {"priv": "DELETE"})
            # True through the table-level grant just restored, not a column grant of its own.
            assert conn.scalar(text(COLUMN_PRIVILEGE))
            assert conn.scalar(text(NAME_ACL)) is None
            assert conn.scalar(text(FIVE_ARG)) is None
            assert conn.scalar(text(THREE_ARG)) is not None
            assert conn.scalar(text(INVITES_TABLE)) is None
            assert conn.scalar(text(ACCEPT_INVITE)) is None
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert {"country", "currency"} <= set(conn.scalars(COLUMNS))
        assert not conn.scalar(text(TABLE_PRIVILEGE), {"priv": "UPDATE"})
        assert not conn.scalar(text(TABLE_PRIVILEGE), {"priv": "DELETE"})
        assert conn.scalar(text(COLUMN_PRIVILEGE))
        assert conn.scalar(text(NAME_ACL)) is not None
        assert conn.scalar(text(SIX_ARG)) is not None
        # 0022 (above 0013 in the chain) drops it again once undone here.
        assert conn.scalar(text(THREE_ARG)) is None
        assert conn.scalar(text(INVITES_TABLE)) is not None
        assert conn.scalar(text(ACCEPT_INVITE)) is not None
        assert conn.scalar(text(INVITES_PK)) == "pk_invites"


def test_downgrading_and_upgrading_0022_restores_the_three_arg_complete_sign_up_and_its_grants(
    migrated: None, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    # A lock held elsewhere (another test, a stray session) must not hang this one forever.
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    try:
        command.downgrade(cfg, "0021")
        with migrate_engine.connect() as conn:
            assert conn.scalar(text(THREE_ARG)) is not None
            assert conn.scalar(text(FIVE_ARG)) is not None
            assert conn.scalar(text(THREE_ARG_TO_APP))
            assert not conn.scalar(text(THREE_ARG_TO_PUBLIC))
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert conn.scalar(text(THREE_ARG)) is None
        assert conn.scalar(text(SIX_ARG)) is not None


MEMBERSHIP_COLUMNS = text(
    "SELECT column_name FROM information_schema.columns WHERE table_name = 'memberships'"
)
CONSTRAINT_NAME = """
SELECT conname FROM pg_constraint
WHERE conrelid = 'memberships'::regclass AND contype = 'c'
  AND conname = 'ck_memberships_display_name_length'
"""


def test_downgrading_and_upgrading_0023_restores_the_display_name_column_and_a_check_that_bites(
    migrated: None, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    try:
        command.downgrade(cfg, "0022")
        with migrate_engine.connect() as conn:
            assert "display_name" not in set(conn.scalars(MEMBERSHIP_COLUMNS))
            assert conn.scalar(text(CONSTRAINT_NAME)) is None
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert "display_name" in set(conn.scalars(MEMBERSHIP_COLUMNS))
        # The name, not merely some check: without the downgrade's op.f() the convention is
        # applied twice and it asks to drop ck_memberships_ck_memberships_display_name_length.
        assert conn.scalar(text(CONSTRAINT_NAME)) == "ck_memberships_display_name_length"
    # And it bites: 61 characters is refused by the database, not only by the API's max_length.
    tenant, user = uuid.uuid7(), uuid.uuid7()
    with pytest.raises(IntegrityError) as error, migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
        conn.execute(text("INSERT INTO tenants (id, name) VALUES (:t, '0023')"), {"t": tenant})
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": user, "e": f"{user}@example.com"},
        )
        conn.execute(
            text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, 'owner')"),
            {"t": tenant, "u": user},
        )
        conn.execute(text("UPDATE memberships SET display_name = repeat('x', 61)"))
    assert isinstance(error.value.orig, CheckViolation)


def test_downgrading_and_upgrading_0024_restores_the_tables_and_their_grants(
    migrated: None, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    try:
        command.downgrade(cfg, "0023")
        with migrate_engine.connect() as conn:
            assert conn.scalar(text("SELECT to_regclass('clients')")) is None
            assert conn.scalar(text("SELECT to_regclass('consents')")) is None
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert not conn.scalar(
            text("SELECT has_table_privilege('ziftbook_app', 'consents', 'UPDATE')")
        )
        assert not conn.scalar(
            text("SELECT has_table_privilege('ziftbook_app', 'consents', 'DELETE')")
        )
        assert not conn.scalar(
            text("SELECT has_table_privilege('ziftbook_app', 'clients', 'DELETE')")
        )
        assert not conn.scalar(
            text("SELECT has_column_privilege('ziftbook_app', 'consents', 'created_at', 'INSERT')")
        )
        assert conn.scalar(
            text("SELECT has_column_privilege('ziftbook_app', 'consents', 'purpose', 'INSERT')")
        )
        # The re-upgrade must also restore what the app still needs: the REVOKE ALL runs before
        # the GRANTs, and a downgrade/upgrade that lost them breaks every read and every write.
        assert conn.scalar(text("SELECT has_table_privilege('ziftbook_app', 'consents', 'SELECT')"))
        assert conn.scalar(text("SELECT has_table_privilege('ziftbook_app', 'clients', 'SELECT')"))
        assert conn.scalar(text("SELECT has_table_privilege('ziftbook_app', 'clients', 'INSERT')"))
        assert conn.scalar(text("SELECT has_table_privilege('ziftbook_app', 'clients', 'UPDATE')"))


ADD_JOB = """
INSERT INTO jobs (kind, dedupe_key, payload, due_at, next_attempt_at, last_error)
VALUES ('test.zif93', :key, '{}'::jsonb, now(), now(), :last_error)
"""


def test_upgrading_0020_rewrites_last_error_to_the_class_only(
    migrated: None, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    # As above: a lock held elsewhere must not hang this test.
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    key = f"test.zif93:{uuid.uuid4()}"
    address = "someone@example.com"
    try:
        command.downgrade(cfg, "0019")
        with migrate_engine.begin() as conn:
            conn.execute(
                text(ADD_JOB),
                {"key": key, "last_error": f"SMTPRecipientsRefused({{'{address}': (550, b'no')}})"},
            )
        command.upgrade(cfg, "head")
        with migrate_engine.connect() as conn:
            last_error = conn.scalar(
                text("SELECT last_error FROM jobs WHERE dedupe_key = :key"), {"key": key}
            )
        assert last_error == "SMTPRecipientsRefused"
        assert address not in (last_error or "")
    finally:
        # Delete before the upgrade: `jobs` is not tenant-scoped, so an upgrade that raises would
        # otherwise leak this row into it for every later test to see.
        with migrate_engine.begin() as conn:
            conn.execute(text("DELETE FROM jobs WHERE dedupe_key = :key"), {"key": key})
        command.upgrade(cfg, "head")


def test_downgrading_and_upgrading_0025_keeps_the_timerange_type(
    migrated: None, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F18: 0025's downgrade must never DROP TYPE timerange - working_hours' own exclusion
    # constraint (0017) still uses it.
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    try:
        command.downgrade(cfg, "0024")
        with migrate_engine.connect() as conn:
            assert conn.scalar(text("SELECT to_regclass('opening_hours')")) is None
            assert conn.scalar(text("SELECT to_regtype('timerange')")) is not None
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert conn.scalar(text("SELECT to_regclass('opening_hours')")) is not None
        assert (
            conn.scalar(
                text("SELECT conname FROM pg_constraint WHERE conname = 'ex_opening_hours_overlap'")
            )
            is not None
        )


def test_downgrading_and_upgrading_0026_restores_the_tables_and_their_grants(
    migrated: None, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    try:
        command.downgrade(cfg, "0025")
        with migrate_engine.connect() as conn:
            assert conn.scalar(text("SELECT to_regclass('bookings')")) is None
            assert conn.scalar(text("SELECT to_regclass('booking_events')")) is None
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert not conn.scalar(
            text("SELECT has_table_privilege('ziftbook_app', 'bookings', 'DELETE')")
        )
        assert not conn.scalar(
            text("SELECT has_table_privilege('ziftbook_app', 'booking_events', 'DELETE')")
        )
        assert not conn.scalar(
            text("SELECT has_table_privilege('ziftbook_app', 'booking_events', 'UPDATE')")
        )
        assert not conn.scalar(
            text(
                "SELECT has_column_privilege('ziftbook_app', 'booking_events', "
                "'created_at', 'INSERT')"
            )
        )
        assert conn.scalar(
            text("SELECT has_column_privilege('ziftbook_app', 'booking_events', 'event', 'INSERT')")
        )
        # The re-upgrade must also restore what the app still needs: the REVOKE ALL runs before
        # the GRANTs, and a downgrade/upgrade that lost them breaks every read and every write.
        assert conn.scalar(
            text("SELECT has_table_privilege('ziftbook_app', 'booking_events', 'SELECT')")
        )
        assert conn.scalar(text("SELECT has_table_privilege('ziftbook_app', 'bookings', 'SELECT')"))
        # INSERT is a column list since 0047 (ZB-09), so has_table_privilege reports it False.
        assert conn.scalar(
            text("SELECT has_column_privilege('ziftbook_app', 'bookings', 'status', 'INSERT')")
        )
        # Column-level since 0027, which revokes table-level UPDATE and grants it back on every
        # column but the two cancellation snapshots. `status` is the only one the app updates.
        assert conn.scalar(
            text("SELECT has_column_privilege('ziftbook_app', 'bookings', 'status', 'UPDATE')")
        )


# F19 (T7) - 0027's round trip: both columns, both CHECKs, and the app's privileges on them.
# Kills: a downgrade that drops one column and not the other, or takes cancellation_policy_text
# with them; an upgrade that adds them nullable or without the CHECK; a 0027 whose REVOKE/GRANT
# pair loses SELECT or INSERT on the way up, or does not put table-level UPDATE back on the way
# down; an upgrade that leaves the snapshot columns UPDATE-able by the app role, or that revokes
# UPDATE from a column the app does write.
#
# The last assertion is also the tripwire for 0027's column-list GRANT: bookings is now a
# column-grant table, so a LATER migration that adds a column without granting UPDATE on it turns
# this red instead of 500ing a route.
NEW_COLUMNS = text("""
SELECT column_name, is_nullable FROM information_schema.columns
WHERE table_name = 'bookings'
  AND column_name IN ('free_cancellation_hours', 'reschedule_cutoff_hours',
                      'cancellation_policy_text')
""")
NEW_CHECKS = text("""
SELECT conname FROM pg_constraint WHERE conrelid = 'bookings'::regclass
  AND conname IN ('ck_bookings_free_cancellation_hours', 'ck_bookings_reschedule_cutoff_hours')
""")
NO_UPDATE = text("""
SELECT column_name FROM information_schema.columns
WHERE table_name = 'bookings'
  AND NOT has_column_privilege('ziftbook_app', 'bookings', column_name, 'UPDATE')
""")
SNAPSHOT = (
    "free_cancellation_hours",
    "reschedule_cutoff_hours",
    "max_reschedules",
    # ZIF-157 (0041): write-once, so no UPDATE grant.
    "client_name",
    "client_phone",
    "client_message",
    # ZB-15 (0046): the checkout secret's hash, written once at placement.
    "checkout_hash",
)
# ZB-09 (0047): the rest of the evidence, and the row's identity, which place() / INSERT_BOOKING
# writes once and no route updates. id and created_at are not INSERT-able either.
EVIDENCE = (
    "id",
    "tenant_id",
    "client_id",
    "service_id",
    "source",
    "service_name",
    "price_amount_minor",
    "price_currency",
    "duration_minutes",
    "cancellation_policy_text",
    "auto_confirm_at_booking",
    "created_at",
)


def test_downgrading_and_upgrading_0027_restores_both_thresholds(
    people: People, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    try:
        command.downgrade(cfg, "0026")
        with migrate_engine.connect() as conn:
            assert dict(conn.execute(NEW_COLUMNS).all()) == {"cancellation_policy_text": "YES"}
            assert conn.scalars(NEW_CHECKS).all() == []
            # Table-level UPDATE is back, exactly as 0026 leaves it.
            assert conn.scalars(NO_UPDATE).all() == []
            assert conn.scalar(
                text("SELECT has_table_privilege('ziftbook_app', 'bookings', 'UPDATE')")
            )
    finally:
        # D17: tenant-scoped, as the migrate role, BEFORE the re-upgrade. 0027's upgrade refuses a
        # non-empty bookings (23502) and would raise out of this finally, leaving the database at
        # 0026 with both columns gone for every later test in this xdist worker.
        with migrate_engine.begin() as conn:
            delete_bookings(conn, (people.a, people.b))
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert dict(conn.execute(NEW_COLUMNS).all()) == {
            "cancellation_policy_text": "YES",
            "free_cancellation_hours": "NO",
            "reschedule_cutoff_hours": "NO",
        }
        assert sorted(conn.scalars(NEW_CHECKS)) == [
            "ck_bookings_free_cancellation_hours",
            "ck_bookings_reschedule_cutoff_hours",
        ]
        for column in SNAPSHOT:
            for privilege in ("INSERT", "SELECT"):
                assert conn.scalar(
                    text("SELECT has_column_privilege('ziftbook_app', 'bookings', :c, :p)"),
                    {"c": column, "p": privilege},
                ), (column, privilege)
        # The snapshot is written once and never updated: immutability as a privilege, the way
        # booking_events' append-only shape is. Exactly these columns, no others.
        assert sorted(conn.scalars(NO_UPDATE)) == sorted(SNAPSHOT + EVIDENCE)


# F20 (T8) - 0027 REFUSES on a non-empty bookings table. That refusal is the feature: ZIF-55 is
# cannot-backfill, and there is no honest value for a booking sold before the policy existed.
# Kills: ADD COLUMN ... DEFAULT 48 (the upgrade succeeds and the pre-existing row silently reads a
# policy that did not exist); nullable columns; any backfill form.
#
# The booking is inserted by its own 0026-shaped statement rather than by
# test_bookings_db.insert_booking: that helper names the two new columns, and this runs BELOW 0027
# where they do not exist, so it would 42703 before the migration could 23502.
BOOKING_AT_0026 = text("""
INSERT INTO bookings (tenant_id, client_id, worker_id, service_id, starts_at, ends_at, status,
                      source, service_name, price_amount_minor, price_currency, duration_minutes,
                      auto_confirm_at_booking)
SELECT current_setting('app.tenant_id')::uuid, :client_id, :worker_id, id,
       now() + interval '1 day', now() + interval '1 day 30 minutes', 'confirmed', 'merchant',
       name, price_amount_minor, price_currency, duration_minutes, true
FROM services WHERE id = :service_id
""")


def test_0027_refuses_to_backfill_an_existing_booking(
    people: People, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    service_id = seed_service(people.a)
    worker_id = member_id(people.a, people.both)
    try:
        command.downgrade(cfg, "0026")
        with tenant_context(people.a) as session:
            client_id = session.scalar(
                text(
                    "INSERT INTO clients (tenant_id, name) "
                    "VALUES (current_setting('app.tenant_id')::uuid, 'Sold') RETURNING id"
                )
            )
            session.execute(
                BOOKING_AT_0026,
                {"client_id": client_id, "worker_id": worker_id, "service_id": service_id},
            )

        # SQLAlchemy wraps it: the spec said psycopg.errors.NotNullViolation, and that is what
        # arrives, but under an IntegrityError. Both are asserted, so another backfill form's
        # CheckViolation would not satisfy this either.
        with pytest.raises(IntegrityError) as raised:
            command.upgrade(cfg, "head")
        assert isinstance(raised.value.orig, NotNullViolation)
    finally:
        # Tenant-scoped, as the migrate role, BEFORE the re-upgrade: the row that made the upgrade
        # raise would otherwise make it raise again and leave every later test at 0026.
        with migrate_engine.begin() as conn:
            delete_bookings(conn, (people.a,))
        command.upgrade(cfg, "head")


# 26. fence (ZIF-101): 0028 is DDL only, no backfill. A partial block inserted at 0027 (tenant-
# scoped, two tenants, one of them midnight-to-midnight local) survives 0028's upgrade unchanged,
# with first_day/last_day NULL and still listed as a partial block. Kills any backfill (it would
# error under FORCE RLS, or wrongly turn the midnight-to-midnight row into a day block).
def test_0028_does_not_backfill_existing_partial_blocks(
    people: People, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    a_member = member_id(people.a, people.only_a)
    b_member = member_id(people.b, people.only_b)
    try:
        command.downgrade(cfg, "0027")
        with tenant_context(people.a) as session:
            a_id = session.scalar(
                text("""
                INSERT INTO time_off (tenant_id, member_id, starts_at, ends_at, reason)
                VALUES (current_setting('app.tenant_id')::uuid, :m, :s, :e, 'A') RETURNING id
                """),
                {"m": a_member, "s": "2026-10-01T08:00:00Z", "e": "2026-10-01T09:00:00Z"},
            )
        with tenant_context(people.b) as session:
            # Midnight-to-midnight local (Europe/Amsterdam, summer time): must stay a partial
            # block, never be read as a day block.
            b_id = session.scalar(
                text("""
                INSERT INTO time_off (tenant_id, member_id, starts_at, ends_at, reason)
                VALUES (current_setting('app.tenant_id')::uuid, :m, :s, :e, 'B') RETURNING id
                """),
                {"m": b_member, "s": "2026-10-01T22:00:00Z", "e": "2026-10-02T22:00:00Z"},
            )
        command.upgrade(cfg, "head")
        with migrate_engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
            a_row = conn.execute(
                text("SELECT starts_at, ends_at, first_day, last_day FROM time_off WHERE id = :id"),
                {"id": a_id},
            ).one()
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.b)})
            b_row = conn.execute(
                text("SELECT starts_at, ends_at, first_day, last_day FROM time_off WHERE id = :id"),
                {"id": b_id},
            ).one()
        assert a_row.first_day is None and a_row.last_day is None
        assert a_row.starts_at is not None and a_row.ends_at is not None
        assert b_row.first_day is None and b_row.last_day is None
        assert b_row.starts_at is not None and b_row.ends_at is not None
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
            conn.execute(text("DELETE FROM time_off"))
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.b)})
            conn.execute(text("DELETE FROM time_off"))


# 27. fence (ZIF-101): downgrade with a whole-day row present refuses with NotNullViolation (never
# 42704: a DML-first downgrade would raise that instead), and leaves the schema at 0028 with the
# row intact; delete it, downgrade succeeds: columns and both CHECKs gone, starts_at/ends_at NOT
# NULL again; upgrade again restores both CHECKs, and they bite. Kills a downgrade that drops or
# converts day rows, and one that leaves the instants nullable.
def test_0028_downgrade_refuses_while_a_whole_day_row_exists(
    people: People, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    a_member = member_id(people.a, people.only_a)
    with tenant_context(people.a) as session:
        day_id = session.scalar(
            text("""
            INSERT INTO time_off (tenant_id, member_id, first_day, last_day, reason)
            VALUES (current_setting('app.tenant_id')::uuid, :m, :f, :l, 'Holiday') RETURNING id
            """),
            {"m": a_member, "f": "2026-10-01", "l": "2026-10-02"},
        )
    try:
        with pytest.raises(IntegrityError) as raised:
            command.downgrade(cfg, "0027")
        assert isinstance(raised.value.orig, NotNullViolation)
        with migrate_engine.connect() as conn:
            version = conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == "0028"
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
            still_there = conn.scalar(
                text("SELECT count(*) FROM time_off WHERE id = :id"), {"id": day_id}
            )
        assert still_there == 1

        with tenant_context(people.a) as session:
            session.execute(text("DELETE FROM time_off WHERE id = :id"), {"id": day_id})
        command.downgrade(cfg, "0027")
        with migrate_engine.connect() as conn:
            columns = set(
                conn.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'time_off'"
                    )
                )
            )
            assert {"first_day", "last_day"}.isdisjoint(columns)
            not_null: dict[str, str] = dict(
                conn.execute(
                    text(
                        "SELECT column_name, is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'time_off' AND column_name IN ('starts_at', 'ends_at')"
                    )
                ).all()
            )
            assert not_null == {"starts_at": "NO", "ends_at": "NO"}
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        checks = set(
            conn.scalars(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid = 'time_off'::regclass "
                    "AND conname IN ('ck_time_off_kind', 'ck_time_off_days')"
                )
            )
        )
        assert checks == {"ck_time_off_kind", "ck_time_off_days"}
    with pytest.raises(IntegrityError) as bites, tenant_context(people.a) as session:
        session.execute(
            text("""
            INSERT INTO time_off (tenant_id, member_id, starts_at, ends_at, first_day, last_day)
            VALUES (current_setting('app.tenant_id')::uuid, :m, NULL, NULL, NULL, NULL)
            """),
            {"m": a_member},
        )
    assert isinstance(bites.value.orig, CheckViolation)


# 35. fence (ZIF-54): 0029's round trip. booking_links exists and is insert-only for the app role
# (no UPDATE, no DELETE); bookings gains original_starts_at, earliest_starts_at, max_reschedules,
# reschedule_count, with the app role able to UPDATE only the last two; and
# ck_bookings_earliest_starts_at_current refuses raising earliest_starts_at above the CURRENT
# starts_at. Kills a downgrade that drops one column and not the rest, an upgrade with a missing or
# too-narrow UPDATE grant, or a CHECK that only compares against original_starts_at.
LINK_TABLE = "SELECT to_regclass('booking_links')"
LINK_PRIVILEGES = """
SELECT has_table_privilege('ziftbook_app', 'booking_links', :priv)
"""
NEW_BOOKING_COLUMNS = text("""
SELECT column_name FROM information_schema.columns
WHERE table_name = 'bookings'
  AND column_name IN ('original_starts_at', 'earliest_starts_at', 'max_reschedules',
                      'reschedule_count')
""")


def test_downgrading_and_upgrading_0029_restores_booking_links_and_the_snapshots(
    people: People, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    try:
        command.downgrade(cfg, "0028")
        with migrate_engine.connect() as conn:
            assert conn.scalar(text(LINK_TABLE)) is None
            assert conn.scalars(NEW_BOOKING_COLUMNS).all() == []
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert conn.scalar(text(LINK_TABLE)) is not None
        # SELECT is table-level; INSERT is a column list (like booking_events' since 0026), so
        # has_table_privilege reports it False even though the app can insert. UPDATE and DELETE
        # are absent from booking_links entirely -- insert-only, exactly booking_events' shape.
        for privilege, expected in (("SELECT", True), ("UPDATE", False), ("DELETE", False)):
            assert (conn.scalar(text(LINK_PRIVILEGES), {"priv": privilege}) or False) == expected, (
                privilege
            )
        assert conn.scalar(
            text(
                "SELECT has_column_privilege('ziftbook_app', 'booking_links', "
                "'token_hash', 'INSERT')"
            )
        )
        assert sorted(conn.scalars(NEW_BOOKING_COLUMNS)) == [
            "earliest_starts_at",
            "max_reschedules",
            "original_starts_at",
            "reschedule_count",
        ]
        for column, expected in (
            ("original_starts_at", True),
            ("earliest_starts_at", True),
            ("max_reschedules", False),
            ("reschedule_count", True),
        ):
            assert (
                conn.scalar(
                    text("SELECT has_column_privilege('ziftbook_app', 'bookings', :c, 'UPDATE')"),
                    {"c": column},
                )
            ) == expected, column
    service_id = seed_service(people.a)
    worker_id = member_id(people.a, people.both)
    with tenant_context(people.a) as session:
        client_id = session.scalar(
            text(
                "INSERT INTO clients (tenant_id, name) "
                "VALUES (current_setting('app.tenant_id')::uuid, 'X') RETURNING id"
            )
        )
        starts_at = datetime.now(UTC) + timedelta(days=1)
        booking_id = insert_booking(
            session,
            client_id=client_id,
            worker_id=worker_id,
            service_id=service_id,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            original_starts_at=None,
            earliest_starts_at=None,
        )
        with pytest.raises(IntegrityError) as bites:
            session.execute(
                text(
                    "UPDATE bookings SET earliest_starts_at = starts_at + interval '1 day' "
                    "WHERE id = :id"
                ),
                {"id": booking_id},
            )
        assert isinstance(bites.value.orig, CheckViolation)
        assert bites.value.orig.diag.constraint_name == "ck_bookings_earliest_starts_at_current"
    # Spec 35: ck_bookings_earliest_starts_at refuses a raise above original_starts_at, on a row
    # whose current start is later still (so only this CHECK, not _current, can refuse it).
    # A fresh client: the refused UPDATE above rolled its whole transaction back.
    original = datetime.now(UTC) + timedelta(days=2)
    with tenant_context(people.a) as session:
        moved_id = insert_booking(
            session,
            client_id=session.scalar(
                text(
                    "INSERT INTO clients (tenant_id, name) "
                    "VALUES (current_setting('app.tenant_id')::uuid, 'Y') RETURNING id"
                )
            ),
            worker_id=worker_id,
            service_id=service_id,
            starts_at=original + timedelta(hours=2),
            ends_at=original + timedelta(hours=2, minutes=30),
            original_starts_at=original,
            earliest_starts_at=original,
            reschedule_count=1,
        )
        with pytest.raises(IntegrityError) as raised:
            session.execute(
                text(
                    "UPDATE bookings SET earliest_starts_at = original_starts_at "
                    "+ interval '1 hour' WHERE id = :id"
                ),
                {"id": moved_id},
            )
    assert isinstance(raised.value.orig, CheckViolation)
    assert raised.value.orig.diag.constraint_name == "ck_bookings_earliest_starts_at"


SLUG_OBJECTS = """
SELECT (SELECT count(*) FROM information_schema.columns
        WHERE table_name = 'tenants' AND column_name = 'slug'),
       (SELECT count(*) FROM pg_trigger WHERE tgname = 'tenants_default_slug'),
       to_regprocedure('slug_ok(text)') IS NULL
         AND to_regprocedure('free_slug(text)') IS NULL
         AND to_regprocedure('tenants_default_slug()') IS NULL
"""
SLUGS = text("SELECT id, slug FROM tenants WHERE id = ANY(:ids) ORDER BY id")


def lock_timeout_config(monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    return cfg


def plain_insert(conn: Connection) -> str:
    try:
        with conn.begin_nested():
            conn.execute(text("INSERT INTO tenants (name) VALUES ('Plain')"))
            conn.execute(text("DELETE FROM tenants WHERE name = 'Plain'"))
        return "ok"
    except DBAPIError as error:
        return str(getattr(error.orig, "sqlstate", error))


def downgrade_outcome(cfg: Config) -> str | None:
    """Run the downgrade; None on success, else its SQLSTATE (or exception type as a fallback).

    A broken downgrade (e.g. dropping slug_ok before the column that CHECKs against it) raises
    instead of returning, so a bare `command.downgrade` call would turn the test's assertion below
    into an unhandled error. Capturing the outcome here keeps the failure an assertion, not an
    error, and a `finally: upgrade head` around the caller can no longer hide it.
    """
    try:
        command.downgrade(cfg, "0029")
        return None
    except Exception as error:
        orig = getattr(error, "orig", None)
        return getattr(orig, "sqlstate", None) or type(error).__name__


# ZIF-56 test 11. fence
def test_0030_round_trips(
    migrated: None, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    tag = uuid.uuid4().hex[:8]
    ids: list[uuid.UUID] = []
    cfg = lock_timeout_config(monkeypatch)
    try:
        assert downgrade_outcome(cfg) is None
        with migrate_engine.begin() as conn:
            # A downgrade that misses a function leaves it behind here, not at head, so catch it
            # now as an assertion; left uncaught, the next upgrade's CREATE FUNCTION would collide
            # with the leftover and crash the test setup instead of failing an assertion.
            assert tuple(conn.execute(text(SLUG_OBJECTS)).one()) == (0, 0, True)
            for name in (f"Round {tag}", f"Trip {tag}"):
                ids.append(
                    conn.scalar(
                        text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": name}
                    )
                )
        command.upgrade(cfg, "0030")
        with migrate_engine.connect() as conn:
            before = conn.execute(SLUGS, {"ids": ids}).all()
            assert [s for _, s in before] == [f"round-{tag}", f"trip-{tag}"]
            assert (
                conn.scalar(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'tenants' AND column_name = 'slug'"
                    )
                )
                == "NO"
            )
            assert (
                conn.scalar(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = 'uq_tenants_slug'")
                )
                == 1
            )
            assert tuple(conn.execute(text(SLUG_OBJECTS)).one()) == (1, 1, False)
        assert downgrade_outcome(cfg) is None
        with migrate_engine.begin() as conn:
            assert tuple(conn.execute(text(SLUG_OBJECTS)).one()) == (0, 0, True)
            assert plain_insert(conn) == "ok"
    finally:
        # Best-effort: a broken migration under test can leave the DB in a state this can't
        # cleanly return to head from (e.g. a function a wrong downgrade failed to drop, colliding
        # with upgrade's CREATE FUNCTION). Swallow that here so it can never replace the assertion
        # above as the test's reported failure.
        try:
            command.upgrade(cfg, "head")
        except Exception:
            pass
    with migrate_engine.begin() as conn:
        assert conn.execute(SLUGS, {"ids": ids}).all() == before
        conn.execute(text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": ids})


# ZIF-56 test 12. fence
def test_0030_backfills_oldest_first(
    migrated: None, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    tag = uuid.uuid4().hex[:8]
    older, newer = uuid.uuid7(), uuid.uuid7()
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0029")
        with migrate_engine.begin() as conn:
            for tenant_id in (newer, older):  # heap order: the newer business first
                conn.execute(
                    text("INSERT INTO tenants (id, name) VALUES (:i, :n)"),
                    {"i": tenant_id, "n": f"Nail bar {tag}"},
                )
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.begin() as conn:
        slugs: dict[uuid.UUID, str] = dict(conn.execute(SLUGS, {"ids": [older, newer]}).all())
        conn.execute(text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [older, newer]})
    assert (slugs[older], slugs[newer]) == (f"nail-bar-{tag}", f"nail-bar-{tag}-2")


DECLINE_MESSAGE = text("""
SELECT (SELECT count(*) FROM information_schema.columns
        WHERE table_name = 'bookings' AND column_name = 'decline_message'),
       (SELECT count(*) FROM pg_constraint WHERE conname = 'ck_bookings_decline_message')
""")


def booked(session: Session, worker_id: uuid.UUID, service_id: uuid.UUID, status: str) -> uuid.UUID:
    """A booking straight in as the app role, for a fresh client."""
    starts_at = datetime.now(UTC) + timedelta(days=1)
    return insert_booking(
        session,
        client_id=session.scalar(
            text(
                "INSERT INTO clients (tenant_id, name) "
                "VALUES (current_setting('app.tenant_id')::uuid, 'X') RETURNING id"
            )
        ),
        worker_id=worker_id,
        service_id=service_id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=30),
        status=status,
        expires_at=starts_at if status == "pending" else None,
    )


SET_MESSAGE = text("UPDATE bookings SET decline_message = :m WHERE id = :id")


# ZIF-121 G1. fence: 0031's round trip over a table already holding a pending row.
def test_0031_round_trips(
    people: People, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    service_id = seed_service(people.a)
    worker_id = member_id(people.a, people.both)
    with tenant_context(people.a) as session:
        pending_id = booked(session, worker_id, service_id, "pending")
    try:
        command.downgrade(cfg, "0030")
        with migrate_engine.connect() as conn:
            assert tuple(conn.execute(DECLINE_MESSAGE).one()) == (0, 0)
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert tuple(conn.execute(DECLINE_MESSAGE).one()) == (1, 1)
        assert conn.scalar(
            text(
                "SELECT has_column_privilege('ziftbook_app', 'bookings', "
                "'decline_message', 'UPDATE')"
            )
        )
    with tenant_context(people.a) as session:
        assert session.scalar(
            text("SELECT decline_message IS NULL FROM bookings WHERE id = :id"), {"id": pending_id}
        )


# ZIF-121 F11. fence, as the app role: the CHECK refuses a message on a non-declined row and one
# over 1000 characters, and accepts NULL everywhere. Kills: no CHECK, a weakened one, or one
# without the explicit IS NULL (NULL AND FALSE is FALSE, which would refuse every other row).
def test_the_decline_message_check(people: People) -> None:
    service_id = seed_service(people.a)
    worker_id = member_id(people.a, people.both)
    with tenant_context(people.a) as session:
        pending_id = booked(session, worker_id, service_id, "pending")  # NULL inserts fine
        declined_id = booked(session, worker_id, service_id, "declined")
        for id_, message in ((pending_id, "Sorry"), (declined_id, "a" * 1001), (declined_id, "")):
            with pytest.raises(IntegrityError) as bites, session.begin_nested():
                session.execute(SET_MESSAGE, {"m": message, "id": id_})
            assert isinstance(bites.value.orig, CheckViolation)
            assert bites.value.orig.diag.constraint_name == "ck_bookings_decline_message"
        session.execute(SET_MESSAGE, {"m": "a" * 1000, "id": declined_id})
        session.execute(SET_MESSAGE, {"m": None, "id": declined_id})  # ZIF-58's erasure path


HOLD_PRIVILEGE = "SELECT has_table_privilege('ziftbook_app', 'booking_holds', :p)"
HOLD_COLUMN_PRIVILEGE = "SELECT has_column_privilege('ziftbook_app', 'booking_holds', :c, :p)"


# ZIF-117 G2. guard: 0034 is one new table and nothing else; its downgrade drops it and its
# upgrade restores forced row-level security and exactly the grants app.holds needs: no UPDATE but
# the token hash, no say over id or created_at (the link's 24 hours run from it).
def test_0034_round_trips_booking_holds(
    people: People, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0033")
        with migrate_engine.connect() as conn:
            assert conn.scalar(text("SELECT to_regclass('booking_holds')")) is None
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = 'booking_holds'::regclass"
            )
        ).one() == (True, True)
        for privilege, expected in (("SELECT", True), ("DELETE", True), ("UPDATE", False)):
            assert conn.scalar(text(HOLD_PRIVILEGE), {"p": privilege}) == expected, privilege
        for column, privilege, expected in (
            ("token_hash", "UPDATE", True),
            ("expires_at", "UPDATE", False),
            ("email", "INSERT", True),
            ("secret_hash", "INSERT", True),
            ("id", "INSERT", False),
            ("created_at", "INSERT", False),
            ("token_hash", "INSERT", False),
        ):
            assert (
                conn.scalar(text(HOLD_COLUMN_PRIVILEGE), {"c": column, "p": privilege}) == expected
            ), (column, privilege)


SLUG_OK_DEF = text("SELECT pg_get_functiondef('slug_ok(text)'::regprocedure)")
SLUG_OK_GRANTS = text(
    "SELECT has_function_privilege('ziftbook_app', 'slug_ok(text)', 'EXECUTE'), "
    "has_function_privilege('public', 'slug_ok(text)', 'EXECUTE')"
)
VERSION = text("SELECT version_num FROM alembic_version")


# ZIF-70 PR0 guard: 0035's downgrade puts 0030's slug_ok back and the upgrade restores its own,
# grants untouched (CREATE OR REPLACE keeps them).
def test_0035_round_trips_slug_ok(migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = lock_timeout_config(monkeypatch)
    with migrate_engine.connect() as conn:
        at_head = conn.scalar(SLUG_OK_DEF)
    assert "'setup'" in at_head
    try:
        command.downgrade(cfg, "0034")
        with migrate_engine.connect() as conn:
            before = conn.scalar(SLUG_OK_DEF)
            assert conn.scalar(text("SELECT slug_ok('setup')")) is True
            assert tuple(conn.execute(SLUG_OK_GRANTS).one()) == (True, False)
    finally:
        command.upgrade(cfg, "head")
    assert before == at_head.replace(",'setup'", "")
    with migrate_engine.connect() as conn:
        assert conn.scalar(SLUG_OK_DEF) == at_head
        assert tuple(conn.execute(SLUG_OK_GRANTS).one()) == (True, False)


# ZIF-70 PR0 fence: 0035 refuses to upgrade while a business holds "setup", instead of silently
# grandfathering it (CHECKs are not re-run on existing rows). Kills an upgrade without the
# precondition.
def test_0035_refuses_while_a_business_holds_setup(
    migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0034")
        with migrate_engine.begin() as conn:
            conn.execute(text("INSERT INTO tenants (name, slug) VALUES ('Setup', 'setup')"))
        with pytest.raises(DBAPIError, match='holds the slug "setup"'):
            command.upgrade(cfg, "0035")
        with migrate_engine.connect() as conn:
            assert conn.scalar(VERSION) == "0034"
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE slug = 'setup'"))
        command.upgrade(cfg, "head")


RESET_DEF = text("SELECT pg_get_functiondef('reset_password(bytea,text)'::regprocedure)")
RESET_GRANTS = text(
    "SELECT has_function_privilege('ziftbook_app', 'reset_password(bytea,text)', 'EXECUTE'), "
    "has_function_privilege('public', 'reset_password(bytea,text)', 'EXECUTE')"
)
ADMIN_SESSION_OBJECTS = text("""
SELECT to_regclass('admin_sessions') IS NOT NULL,
       to_regprocedure('start_admin_session(uuid,text,bytea,inet,text)') IS NOT NULL,
       to_regprocedure('resolve_admin_session(bytea)') IS NOT NULL,
       to_regprocedure('end_admin_session(bytea)') IS NOT NULL
""")
END_ADMIN_SESSIONS = "\n  DELETE FROM admin_sessions s WHERE s.user_id = v_user;"


# ZIF-70 PR2 guard: 0037's downgrade drops admin sessions and puts 0010's reset_password back; the
# upgrade restores its own, grants untouched (CREATE OR REPLACE keeps them).
def test_0037_round_trips_admin_sessions(
    migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    with migrate_engine.connect() as conn:
        at_head = conn.scalar(RESET_DEF)
    assert END_ADMIN_SESSIONS in at_head
    try:
        command.downgrade(cfg, "0036")
        with migrate_engine.connect() as conn:
            before = conn.scalar(RESET_DEF)
            assert tuple(conn.execute(ADMIN_SESSION_OBJECTS).one()) == (False,) * 4
            assert tuple(conn.execute(RESET_GRANTS).one()) == (True, False)
    finally:
        command.upgrade(cfg, "head")
    assert before == at_head.replace(END_ADMIN_SESSIONS, "")
    with migrate_engine.connect() as conn:
        assert conn.scalar(RESET_DEF) == at_head
        assert tuple(conn.execute(ADMIN_SESSION_OBJECTS).one()) == (True,) * 4
        assert tuple(conn.execute(RESET_GRANTS).one()) == (True, False)


def migration(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, API_DIR / "migrations" / "versions" / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ZIF-70 PR2 guard: 0037's reset_password template, without the new line, is 0010's body verbatim,
# so the downgrade restores what 0010 wrote, not a drifted copy.
def test_0037_reset_password_is_0010s_plus_one_line() -> None:
    old = migration("0010_audit_events.py").RESET_PASSWORD
    new = migration("0037_admin_sessions.py")

    assert new.reset_password("") == old.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    assert new.reset_password(new.END_ADMIN_SESSIONS) != new.reset_password("")


RESOLVE_DEF = text("SELECT pg_get_functiondef('resolve_admin_session(bytea)'::regprocedure)")
RESOLVE_GRANTS = text(
    "SELECT has_function_privilege('ziftbook_app', 'resolve_admin_session(bytea)', 'EXECUTE'), "
    "has_function_privilege('public', 'resolve_admin_session(bytea)', 'EXECUTE')"
)
SECOND_FACTOR_OBJECTS = text("""
SELECT to_regclass('second_factors') IS NOT NULL,
       to_regprocedure('live_admin_user(bytea)') IS NOT NULL,
       to_regprocedure('totp_ciphertext(bytea)') IS NOT NULL,
       to_regprocedure('begin_totp_enrolment(bytea,bytea,bytea,bytea)') IS NOT NULL,
       to_regprocedure('confirm_totp_enrolment(bytea,bytea,bigint,bytea[])') IS NOT NULL,
       to_regprocedure('pass_second_factor(bytea,bytea,bigint)') IS NOT NULL,
       to_regprocedure('use_recovery_code(bytea,bytea)') IS NOT NULL,
       EXISTS (SELECT FROM information_schema.columns
               WHERE table_name = 'admin_sessions' AND column_name = 'second_factor_at')
""")


# ZIF-71 PR1 guard: 0039's downgrade drops every second-factor object and puts 0037's
# resolve_admin_session back, grants included; the upgrade brings them all back.
def test_0039_round_trips_second_factors(
    migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    with migrate_engine.connect() as conn:
        at_head = conn.scalar(RESOLVE_DEF)
    try:
        command.downgrade(cfg, "0038")
        with migrate_engine.connect() as conn:
            before = conn.scalar(RESOLVE_DEF)
            assert tuple(conn.execute(SECOND_FACTOR_OBJECTS).one()) == (False,) * 8
            assert tuple(conn.execute(RESOLVE_GRANTS).one()) == (True, False)
    finally:
        command.upgrade(cfg, "head")
    assert "factor_passed" in at_head and "factor_passed" not in before
    with migrate_engine.connect() as conn:
        assert conn.scalar(RESOLVE_DEF) == at_head
        assert tuple(conn.execute(SECOND_FACTOR_OBJECTS).one()) == (True,) * 8
        assert tuple(conn.execute(RESOLVE_GRANTS).one()) == (True, False)


# ZIF-71 PR1 guard: the downgrade's resolve_admin_session is 0037's text verbatim.
def test_0039_restores_0037s_resolve_admin_session() -> None:
    old = migration("0037_admin_sessions.py").FUNCTIONS["resolve_admin_session(bytea)"]
    assert migration("0039_admin_second_factor.py").RESOLVE_0037 == old


# ZIF-157 B1 FENCE. Wrong impl: an upgrade missing a CHECK, or a client_phone that takes any text
# (not E.164); a downgrade that leaves a column behind.
SNAPSHOT_COLUMNS = text("""
SELECT column_name, is_nullable FROM information_schema.columns
WHERE table_name = 'bookings'
  AND column_name IN ('client_name', 'client_phone', 'client_message')
""")


def test_0041_round_trips_the_booking_snapshot(
    people: People, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0040")
        with migrate_engine.connect() as conn:
            assert conn.execute(SNAPSHOT_COLUMNS).all() == []
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert dict(conn.execute(SNAPSHOT_COLUMNS).all()) == {
            "client_name": "YES",
            "client_phone": "YES",
            "client_message": "YES",
        }

    service_id = seed_service(people.a)
    starts_at = datetime.now(UTC) + timedelta(days=1)
    with tenant_context(people.a) as session:
        client_id = session.scalar(
            text(
                "INSERT INTO clients (tenant_id, name) "
                "VALUES (current_setting('app.tenant_id')::uuid, 'Z') RETURNING id"
            )
        )
        booking_id = insert_booking(
            session,
            client_id=client_id,
            worker_id=member_id(people.a, people.both),
            service_id=service_id,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
        )
    # The app role cannot UPDATE these columns (write-once), so the migrate role proves the CHECKs.
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        for column, bad in (
            ("client_name", "x" * 101),
            ("client_name", ""),
            ("client_phone", "0612345678"),
            ("client_message", ""),
            ("client_message", "x" * 1001),
        ):
            with pytest.raises(IntegrityError) as bites, conn.begin_nested():
                conn.execute(
                    text(f"UPDATE bookings SET {column} = :v WHERE id = :id"),
                    {"v": bad, "id": booking_id},
                )
            assert isinstance(bites.value.orig, CheckViolation)
            assert bites.value.orig.diag.constraint_name == f"ck_bookings_{column}"
        conn.execute(
            text(
                "UPDATE bookings SET client_name = :n, client_phone = '+31612345678', "
                "client_message = :m WHERE id = :id"
            ),
            {"n": "x" * 100, "m": "y" * 1000, "id": booking_id},
        )


ANCHOR_GUARD = text("""
SELECT has_column_privilege('ziftbook_app', 'bookings', 'original_starts_at', 'UPDATE'),
       EXISTS (SELECT FROM pg_trigger WHERE tgname = 'original_starts_at_while_pending')
""")


# ZIF-157 B3 GUARD. Wrong impl: a 0042 downgrade that leaves the UPDATE grant or the trigger
# behind, or an upgrade that adds neither.
def test_0042_round_trips(migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0041")
        with migrate_engine.connect() as conn:
            assert conn.execute(ANCHOR_GUARD).one() == (False, False)
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert conn.execute(ANCHOR_GUARD).one() == (True, True)


PAYMENT_ACCOUNTS = text("""
SELECT to_regclass('payment_accounts') IS NOT NULL,
       (SELECT count(*) FROM pg_policy WHERE polrelid = to_regclass('payment_accounts')),
       (SELECT count(*) FROM pg_trigger
         WHERE tgrelid = to_regclass('payment_accounts') AND NOT tgisinternal),
       to_regprocedure('stripe_account_tenant(text)') IS NOT NULL,
       to_regprocedure('payment_account_write_once()') IS NOT NULL,
       to_regprocedure('payment_account_fee_payer_fixed()') IS NOT NULL
""")
PAYMENT_PRIVILEGES = text("""
SELECT has_table_privilege('ziftbook_app', 'payment_accounts', 'DELETE'),
       has_column_privilege('ziftbook_app', 'payment_accounts', 'charges_enabled', 'UPDATE'),
       has_column_privilege('ziftbook_app', 'payment_accounts', 'stripe_account_id', 'UPDATE'),
       has_column_privilege('ziftbook_app', 'payment_accounts', 'tenant_id', 'UPDATE'),
       has_column_privilege('ziftbook_app', 'payment_accounts', 'fee_mode', 'UPDATE'),
       has_column_privilege('ziftbook_app', 'payment_accounts', 'fee_mode', 'INSERT'),
       has_column_privilege('ziftbook_app', 'payment_accounts', 'charges_enabled', 'INSERT'),
       has_function_privilege('ziftbook_app', 'stripe_account_tenant(text)', 'EXECUTE'),
       has_function_privilege('public', 'stripe_account_tenant(text)', 'EXECUTE')
""")


# ZIF-158 G19. guard. Up, down, up again: the second up fails on any function left behind.
def test_0043_round_trips_payment_accounts(
    migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0042")
        with migrate_engine.connect() as conn:
            assert tuple(conn.execute(PAYMENT_ACCOUNTS).one()) == (False, 0, 0, False, False, False)
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert tuple(conn.execute(PAYMENT_ACCOUNTS).one()) == (True, 2, 2, True, True, True)
        assert tuple(conn.execute(PAYMENT_PRIVILEGES).one()) == (
            False,
            True,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
        )


BOOKING_PAYMENTS = text("""
SELECT to_regclass('booking_payments') IS NOT NULL,
       (SELECT count(*) FROM pg_constraint
         WHERE conname = 'uq_bookings_tenant_id_id_price' AND conrelid = 'bookings'::regclass)
""")
PAYMENT_COLUMNS = text("""
SELECT column_name FROM information_schema.columns WHERE table_name = 'booking_payments'
""")
PAYMENT_GRANT = "SELECT has_column_privilege('ziftbook_app', 'booking_payments', :c, :p)"
GOOD_PAYMENT: dict[str, Any] = {
    "kind": "check",
    "intent_id": "pi_abc123",
    "amount_minor": 2500,
    "currency": "EUR",
    "fee_mode": "business_pays",
    "application_fee_minor": 0,
    "stripe_fee_minor": None,
    "status": "open",
    "refunded_minor": 0,
    "refund_id": None,
}
# Each CHECK refused once, by name: the row is good except for the one field that breaks it.
PAYMENT_CHECKS: list[tuple[str, dict[str, Any]]] = [
    ("ck_booking_payments_kind", {"kind": "x"}),
    ("ck_booking_payments_intent_id", {"intent_id": "nope"}),
    ("ck_booking_payments_amount_minor", {"amount_minor": 0}),
    ("ck_booking_payments_fee_mode", {"fee_mode": "x"}),
    ("ck_booking_payments_application_fee_minor", {"application_fee_minor": -1}),
    ("ck_booking_payments_stripe_fee_minor", {"stripe_fee_minor": 2501}),
    ("ck_booking_payments_fee_needs_mode_2", {"application_fee_minor": 5}),
    ("ck_booking_payments_status", {"status": "x"}),
    (
        "ck_booking_payments_refunded_minor",
        {"kind": "pay", "status": "paid", "refunded_minor": 2501},
    ),
    ("ck_booking_payments_refund_id", {"refund_id": "x"}),
    ("ck_booking_payments_kind_status", {"status": "paid"}),
    ("ck_booking_payments_setup_is_a_check", {"kind": "pay", "intent_id": "seti_abc"}),
    ("ck_booking_payments_refund_needs_paid", {"refunded_minor": 5}),
]


# ZIF-158 B2 17. guard: 0044's downgrade drops the table and the bookings key it needed; the
# upgrade restores the grants as listed (no DELETE, the fee policy write-once), and every CHECK
# refuses its own bad row.
def test_0044_round_trips_booking_payments(
    people: People, migrate_engine: Engine, app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0043")
        with migrate_engine.connect() as conn:
            assert tuple(conn.execute(BOOKING_PAYMENTS).one()) == (False, 0)
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert tuple(conn.execute(BOOKING_PAYMENTS).one()) == (True, 1)
        assert (
            conn.scalar(
                text("SELECT has_table_privilege('ziftbook_app', 'booking_payments', 'DELETE')")
            )
            is False
        )
        assert (
            conn.scalar(
                text("SELECT has_table_privilege('ziftbook_app', 'booking_payments', 'SELECT')")
            )
            is True
        )
        columns = set(conn.scalars(PAYMENT_COLUMNS))
        for column in sorted(columns):
            insert = column in {
                "tenant_id",
                "booking_id",
                "kind",
                "intent_id",
                "amount_minor",
                "currency",
                "fee_mode",
                "application_fee_minor",
            }
            update = column in {
                "status",
                "refunded_minor",
                "refund_id",
                "settled_at",
                "stripe_fee_minor",
                "refund_failed_at",
            }
            for privilege, expected in (("INSERT", insert), ("UPDATE", update)):
                got = conn.scalar(text(PAYMENT_GRANT), {"c": column, "p": privilege})
                assert got is expected, (column, privilege)

    booking_id = _seed_booking(people)
    for constraint, change in PAYMENT_CHECKS:
        row = {**GOOD_PAYMENT, **change}
        with pytest.raises(IntegrityError) as raised:
            _insert_payment(migrate_engine, people, booking_id, row)
        assert raised.value.orig.diag.constraint_name == constraint, constraint  # type: ignore[union-attr]
    _insert_payment(migrate_engine, people, booking_id, GOOD_PAYMENT)
    with pytest.raises(IntegrityError) as raised:
        _insert_payment(migrate_engine, people, booking_id, {**GOOD_PAYMENT, "intent_id": "pi_two"})
    assert raised.value.orig.diag.constraint_name == "uq_booking_payments_live"  # type: ignore[union-attr]
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(text("UPDATE booking_payments SET status = 'canceled'"))
    _insert_payment(migrate_engine, people, booking_id, {**GOOD_PAYMENT, "intent_id": "pi_two"})


def _seed_booking(people: People) -> uuid.UUID:
    from tests.test_bookings_db import book
    from tests.test_clients_db import seed_client

    start = datetime.now(UTC) + timedelta(days=3)
    return book(
        people.a,
        client_id=seed_client(people.a, email=f"{uuid.uuid4().hex}@example.com"),
        worker_id=member_id(people.a, people.both),
        service_id=seed_service(people.a),
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
    )


def _insert_payment(
    engine: Engine, people: People, booking_id: uuid.UUID, row: dict[str, Any]
) -> None:
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text(
                "INSERT INTO booking_payments (tenant_id, booking_id, kind, intent_id, "
                "amount_minor, currency, fee_mode, application_fee_minor, stripe_fee_minor, "
                "status, refunded_minor, refund_id) VALUES (:t, :b, :kind, :intent_id, "
                ":amount_minor, :currency, :fee_mode, :application_fee_minor, :stripe_fee_minor, "
                ":status, :refunded_minor, :refund_id)"
            ),
            {"t": people.a, "b": booking_id, **row},
        )


FUNCTION_SOURCE = text("SELECT prosrc FROM pg_proc WHERE proname = :n")
MEMBERSHIP_SHAPE = text("""
SELECT
  (SELECT count(*) FROM information_schema.columns
   WHERE table_name = 'memberships' AND column_name = 'removed_at'),
  (SELECT count(*) FROM pg_constraint WHERE conname = 'ck_sessions_role'),
  (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_memberships_role')
""")


# ZIF-112 27. fence: 0045's downgrade restores the old role CHECK and the three old function
# bodies exactly, its upgrade brings everything back, and the removed_at pairing bites both ways.
def test_downgrading_and_upgrading_0045_restores_the_role_check_and_the_three_functions(
    people: People, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)

    def body_of(source: str) -> str:
        return " ".join(source.split())

    def body(create: str) -> str:
        return body_of(create.split("$$")[1])

    # The bodies the earlier migrations installed. Whitespace is collapsed on both sides: 0045
    # wraps account_by_email's one long line around the filter placeholder; every token is compared.
    old_sources = [
        body(migration("0007_password_auth.py").FUNCTIONS["account_by_email(text)"]),
        body(migration("0032_user_name.py").NEW_ACCEPT_INVITE),
        body(migration("0032_user_name.py").SET_OWN_NAME_FUNCTION),
    ]
    functions = ("account_by_email", "accept_invite", "set_own_name")

    def sources() -> list[str]:
        with migrate_engine.connect() as conn:
            return [body_of(conn.scalar(FUNCTION_SOURCE, {"n": n})) for n in functions]

    def shape() -> tuple[Any, ...]:
        with migrate_engine.connect() as conn:
            columns, sessions_check, role_check = conn.execute(MEMBERSHIP_SHAPE).one()
        return columns, sessions_check, "removed" in role_check

    try:
        command.downgrade(cfg, "0044")
        assert shape() == (0, 0, False)
        assert sources() == old_sources
    finally:
        command.upgrade(cfg, "head")
    assert shape() == (1, 1, True)
    assert all("removed" in source for source in sources())
    for change in ("SET role = 'removed'", "SET removed_at = now()"):
        with pytest.raises(IntegrityError) as raised, tenant_context(people.a) as session:
            session.execute(
                text(f"UPDATE memberships {change} WHERE user_id = :u"), {"u": people.only_a}
            )
        assert raised.value.orig.diag.constraint_name == "ck_memberships_removed_at"  # type: ignore[union-attr]


# ZIF-112 28. fence: the downgrade refuses while a membership is removed (the old role CHECK), and
# changes nothing: version still 0045, the row intact.
def test_0045_downgrade_refuses_while_a_membership_is_removed(
    people: People, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    end_membership(people.a, people.only_a)
    try:
        with pytest.raises(IntegrityError) as raised:
            command.downgrade(cfg, "0044")
        assert isinstance(raised.value.orig, CheckViolation)
        assert raised.value.orig.diag.constraint_name == "ck_memberships_role"
        with migrate_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0045"
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
            role = conn.scalar(
                text("SELECT role FROM memberships WHERE user_id = :u"), {"u": people.only_a}
            )
        assert role == "removed"
    finally:
        command.upgrade(cfg, "head")


CHECKOUT_HASH = text("""
SELECT data_type, is_nullable,
       has_column_privilege('ziftbook_app', 'bookings', column_name, 'INSERT'),
       has_column_privilege('ziftbook_app', 'bookings', column_name, 'SELECT'),
       has_column_privilege('ziftbook_app', 'bookings', column_name, 'UPDATE')
FROM information_schema.columns WHERE table_name = 'bookings' AND column_name = 'checkout_hash'
""")


# ZB-15 FENCE. Wrong impl: a downgrade that leaves the column behind; an upgrade that makes it
# NOT NULL (the release still running inserts without it, and its bookings have none), or that
# lets the app role UPDATE it (the hash is written once, when the booking is placed).
def test_0046_round_trips_the_checkout_hash(
    migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0045")
        with migrate_engine.connect() as conn:
            assert conn.execute(CHECKOUT_HASH).all() == []
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert tuple(conn.execute(CHECKOUT_HASH).one()) == ("bytea", "YES", True, True, False)


EVIDENCE_GRANTS = text("""
SELECT column_name,
       has_column_privilege('ziftbook_app', 'bookings', column_name, 'INSERT'),
       has_column_privilege('ziftbook_app', 'bookings', column_name, 'UPDATE')
FROM information_schema.columns WHERE table_name = 'bookings'
""")
EVIDENCE_GUARD = text("""
SELECT has_table_privilege('ziftbook_app', 'bookings', 'INSERT'),
       has_table_privilege('ziftbook_app', 'bookings', 'UPDATE'),
       (SELECT count(*) FROM pg_attribute a, aclexplode(a.attacl) x
         WHERE a.attrelid = 'bookings'::regclass AND x.privilege_type = 'INSERT'
           AND x.grantee = 'ziftbook_app'::regrole),
       EXISTS (SELECT FROM pg_trigger WHERE tgname = 'bookings_status_to_pending'),
       to_regprocedure('bookings_status_to_pending()') IS NOT NULL
""")
STATUS_GUARD_FUNCTION = text("""
SELECT p.prosecdef, p.proconfig, has_function_privilege('public', p.oid, 'EXECUTE')
FROM pg_proc p WHERE p.oid = to_regprocedure('bookings_status_to_pending()')
""")
# Explicit literals, never 0047's constants: a shrunk grant would shrink its expectation with it.
# Before 0047: 0027's list plus 0029's, 0031's and 0042's column grants.
UPDATABLE_AT_0046 = {
    "id",
    "tenant_id",
    "client_id",
    "worker_id",
    "service_id",
    "starts_at",
    "ends_at",
    "status",
    "expires_at",
    "source",
    "service_name",
    "price_amount_minor",
    "price_currency",
    "duration_minutes",
    "cancellation_policy_text",
    "auto_confirm_at_booking",
    "worker_display_name",
    "created_at",
    "reschedule_count",
    "earliest_starts_at",
    "decline_message",
    "original_starts_at",
}
# What app/bookings.py's UPDATEs set, and nothing else.
UPDATABLE = {
    "status",
    "expires_at",
    "decline_message",
    "starts_at",
    "ends_at",
    "earliest_starts_at",
    "reschedule_count",
    "original_starts_at",
    "worker_id",
    "worker_display_name",
}
# What app/bookings.py's INSERT_BOOKING names, and nothing else.
INSERTABLE = {
    "tenant_id",
    "client_id",
    "worker_id",
    "service_id",
    "starts_at",
    "ends_at",
    "status",
    "expires_at",
    "source",
    "service_name",
    "price_amount_minor",
    "price_currency",
    "duration_minutes",
    "cancellation_policy_text",
    "auto_confirm_at_booking",
    "worker_display_name",
    "free_cancellation_hours",
    "reschedule_cutoff_hours",
    "original_starts_at",
    "earliest_starts_at",
    "max_reschedules",
    "client_name",
    "client_phone",
    "client_message",
    "checkout_hash",
}


# ZB-09 FENCE. Wrong impl: an upgrade that leaves table-level INSERT, grants UPDATE or INSERT on a
# column no app statement writes, or skips the trigger; a downgrade that leaves the column lists,
# the trigger or its function behind, or does not put back exactly the UPDATE columns and the
# table-level INSERT that 0027-0046 left.
def test_0047_round_trips_the_booking_evidence_grants(
    migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0046")
        with migrate_engine.connect() as conn:
            grants = conn.execute(EVIDENCE_GRANTS).all()
            assert {column for column, _, update in grants if update} == UPDATABLE_AT_0046
            assert all(insert for _, insert, _ in grants)
            assert tuple(conn.execute(EVIDENCE_GUARD).one()) == (True, False, 0, False, False)
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        grants = conn.execute(EVIDENCE_GRANTS).all()
        assert {column for column, _, update in grants if update} == UPDATABLE
        assert {column for column, insert, _ in grants if insert} == INSERTABLE
        assert tuple(conn.execute(EVIDENCE_GUARD).one()) == (False, False, 25, True, True)
        assert tuple(conn.execute(STATUS_GUARD_FUNCTION).one()) == (
            False,
            ["search_path=pg_catalog, public, pg_temp"],
            False,
        )


# ZB-10 (0048): ziftbook_app's table and column privileges on the three tables it lost writes on.
WRITE_GRANTS = text("""
SELECT c.relname,
       (SELECT string_agg(x.privilege_type, ' ' ORDER BY x.privilege_type)
        FROM aclexplode(c.relacl) x WHERE x.grantee = 'ziftbook_app'::regrole),
       (SELECT string_agg(a.attname || ' ' || x.privilege_type, ', '
                          ORDER BY a.attname, x.privilege_type)
        FROM pg_attribute a, aclexplode(a.attacl) x
        WHERE a.attrelid = c.oid AND x.grantee = 'ziftbook_app'::regrole)
FROM pg_class c
WHERE c.oid IN ('users'::regclass, 'tenants'::regclass, 'email_outbox'::regclass)
ORDER BY 1
""")
USERS_INSERT_POLICY = text("""
SELECT policyname, cmd, roles::text, with_check FROM pg_policies
WHERE schemaname = 'public' AND tablename = 'users' AND cmd = 'INSERT'
""")
FUNCTION_DEFAULTS = text("""
SELECT defaclnamespace, defaclacl::text FROM pg_default_acl
WHERE defaclrole = 'ziftbook_migrate'::regrole AND defaclobjtype = 'f'
""")
# Explicit literals, never 0048's constants. Before 0048: 0001's default DML, narrowed only on
# tenants (0013: UPDATE (name), no table-level UPDATE or DELETE); no default for functions, so
# Postgres's built-in EXECUTE to PUBLIC.
WRITE_GRANTS_AT_0047 = [
    ("email_outbox", "DELETE INSERT SELECT UPDATE", None),
    ("tenants", "INSERT SELECT", "name UPDATE"),
    ("users", "DELETE INSERT SELECT UPDATE", None),
]
WRITE_GRANTS_AT_0048 = [
    ("email_outbox", "INSERT SELECT", "sent_at UPDATE, status UPDATE, subject UPDATE"),
    ("tenants", "SELECT", "name UPDATE"),
    ("users", "SELECT", None),
]


# ZB-10 FENCE. Wrong impl: a downgrade that leaves the email_outbox column grant beside the
# table-level UPDATE, forgets create_users, or leaves the function default behind.
def test_0048_round_trips_the_app_roles_write_privileges(
    migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0047")
        with migrate_engine.connect() as conn:
            assert [tuple(r) for r in conn.execute(WRITE_GRANTS)] == WRITE_GRANTS_AT_0047
            assert [tuple(r) for r in conn.execute(USERS_INSERT_POLICY)] == [
                ("create_users", "INSERT", "{public}", "true")
            ]
            assert conn.execute(FUNCTION_DEFAULTS).all() == []
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert [tuple(r) for r in conn.execute(WRITE_GRANTS)] == WRITE_GRANTS_AT_0048
        assert conn.execute(USERS_INSERT_POLICY).all() == []
        assert [tuple(r) for r in conn.execute(FUNCTION_DEFAULTS)] == [
            (0, "{ziftbook_migrate=X/ziftbook_migrate}")
        ]


# ZB-08 (0049): the roles holding EXECUTE on mint_email_token in their own right.
MINT_HOLDERS = text("""
SELECT x.grantee::regrole::text FROM pg_proc p, aclexplode(p.proacl) x
WHERE p.oid = 'mint_email_token(uuid,bytea)'::regprocedure AND x.privilege_type = 'EXECUTE'
ORDER BY 1
""")


# ZB-08 FENCE. Wrong impl: a downgrade that leaves the worker's grant behind, or an upgrade that
# does not give it to the worker. At head the app role no longer holds it (0051).
def test_0049_round_trips_the_workers_mint_grant(
    migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0048")
        with migrate_engine.connect() as conn:
            assert list(conn.scalars(MINT_HOLDERS)) == ["ziftbook_app", "ziftbook_migrate"]
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert list(conn.scalars(MINT_HOLDERS)) == ["ziftbook_migrate", "ziftbook_worker"]


# ZB-31 (0050): which of these may create temporary tables in this database. The owner,
# ziftbook_migrate in the test databases, always may.
TEMPORARY_HOLDERS = text("""
SELECT r FROM unnest(ARRAY['public', 'ziftbook_app', 'ziftbook_migrate', 'ziftbook_worker']) r
WHERE has_database_privilege(r, current_database(), 'TEMPORARY')
ORDER BY 1
""")
PUBLIC_TEMPORARY = text("SELECT has_database_privilege('public', current_database(), 'TEMPORARY')")


# ZB-31 FENCE. Wrong impl: an upgrade that leaves TEMPORARY to PUBLIC, or a downgrade that does not
# give it back.
def test_0050_round_trips_temporary_tables(
    migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0049")
        with migrate_engine.connect() as conn:
            assert list(conn.scalars(TEMPORARY_HOLDERS)) == [
                "public",
                "ziftbook_app",
                "ziftbook_migrate",
                "ziftbook_worker",
            ]
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert list(conn.scalars(TEMPORARY_HOLDERS)) == ["ziftbook_migrate"]


@pytest.fixture
def admins_database() -> Iterator[Engine]:
    """A database of this xdist worker's own that the admin role owns, as docker-compose's and
    production's do, and an engine on it as ziftbook_migrate. Dropped after."""
    base = make_url(os.environ["ZIF_MIGRATE_DATABASE_URL"])
    name = f"{base.database}_admins"
    # It renders into DROP DATABASE as the superuser: a plain identifier within 63 bytes only.
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise RuntimeError(f"database name {name!r} is not a plain identifier of 63 bytes or less")
    admin = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        conn.execute(text(f"CREATE DATABASE {name}"))
    engine = create_engine(base.set(database=name), poolclass=NullPool)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            # FORCE: a test that failed mid-way must not keep it. The name is this worker's alone.
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


def run_0050(conn: Connection, step: str) -> bool:
    """0050's upgrade() or downgrade() on conn, as Alembic runs it; then whether PUBLIC may create
    temporary tables."""
    module = migration("0050_no_temporary_tables.py")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(module, step)()
    return bool(conn.scalar(PUBLIC_TEMPORARY))


def skips_0050(lines: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    return [
        (line["logger"], line["level"], line["step"], line["role"])
        for line in lines
        if line["msg"] == "temporary tables left to bootstrap.sql"
    ]


# ZB-31 GUARD. Where ziftbook_migrate does not own the database, 0050 neither fails nor changes
# anything: only the owner can take TEMPORARY from PUBLIC, and bootstrap.sql, run as the admin
# before the migrations, does it there. Wrong impl: a skip the migrate job does not show, as a
# NOTICE is not (app.logs keeps the sqlalchemy loggers at WARNING).
def test_0050_leaves_a_database_it_does_not_own_to_bootstrap_and_says_so(
    admins_database: Engine, log_lines: Callable[[], list[dict[str, Any]]]
) -> None:
    with admins_database.begin() as conn:
        assert conn.scalar(text("SELECT current_user")) == "ziftbook_migrate"
        after = [run_0050(conn, "upgrade"), run_0050(conn, "downgrade")]
    assert after == [True, True]
    assert skips_0050(log_lines()) == [
        ("app.migrations", "WARNING", "upgrade", "ziftbook_migrate"),
        ("app.migrations", "WARNING", "downgrade", "ziftbook_migrate"),
    ]


# ZB-31 FENCE. Wrong impl: the warning where ziftbook_migrate owns the database and 0050 does its
# work. Rolled back: this worker's database keeps its state.
def test_0050_says_nothing_where_it_owns_the_database(
    migrate_engine: Engine, log_lines: Callable[[], list[dict[str, Any]]]
) -> None:
    with migrate_engine.connect() as conn, conn.begin() as tx:
        after = [run_0050(conn, "downgrade"), run_0050(conn, "upgrade")]
        tx.rollback()
    assert after == [True, False]
    assert skips_0050(log_lines()) == []


# ZB-08 FENCE (0051, the contract step). Wrong impl: an upgrade that leaves EXECUTE on the app role
# or takes it from the worker; a downgrade that does not give the app role its grant back.
def test_0051_round_trips_the_app_roles_mint_grant(
    migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0050")
        with migrate_engine.connect() as conn:
            assert list(conn.scalars(MINT_HOLDERS)) == [
                "ziftbook_app",
                "ziftbook_migrate",
                "ziftbook_worker",
            ]
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert list(conn.scalars(MINT_HOLDERS)) == ["ziftbook_migrate", "ziftbook_worker"]


# ZB-10 (0052): ziftbook_app's table privileges on memberships.
MEMBERSHIP_GRANTS = text("""
SELECT string_agg(x.privilege_type, ' ' ORDER BY x.privilege_type)
FROM pg_class c, aclexplode(c.relacl) x
WHERE c.oid = 'memberships'::regclass AND x.grantee = 'ziftbook_app'::regrole
""")


# ZB-10 FENCE (0052, the contract step). Wrong impl: an upgrade that leaves DELETE on memberships
# to the app role or takes anything else; a downgrade that does not give it back.
def test_0052_round_trips_the_app_roles_membership_delete(
    migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0051")
        with migrate_engine.connect() as conn:
            assert conn.scalar(MEMBERSHIP_GRANTS) == "DELETE INSERT SELECT UPDATE"
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert conn.scalar(MEMBERSHIP_GRANTS) == "INSERT SELECT UPDATE"


# ZB-08 (0053): the trigger that leaves writing an invite's token hash to the worker, and its
# function.
INVITE_TOKEN_GUARD = text("""
SELECT EXISTS (SELECT FROM pg_trigger WHERE tgname = 'invites_token_hash_by_worker'
                 AND tgrelid = 'invites'::regclass),
       to_regprocedure('invites_token_hash_by_worker()') IS NOT NULL
""")
INVITE_TOKEN_GUARD_FUNCTION = text("""
SELECT p.prosecdef, p.proconfig, has_function_privilege('public', p.oid, 'EXECUTE')
FROM pg_proc p WHERE p.oid = to_regprocedure('invites_token_hash_by_worker()')
""")


# ZB-08 FENCE (0053). Wrong impl: a downgrade that leaves the trigger or its function behind; an
# upgrade whose function runs as its owner, searches a caller's schema first, or is PUBLIC's.
def test_0053_round_trips_the_invite_token_guard(
    migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        command.downgrade(cfg, "0052")
        with migrate_engine.connect() as conn:
            assert tuple(conn.execute(INVITE_TOKEN_GUARD).one()) == (False, False)
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert tuple(conn.execute(INVITE_TOKEN_GUARD).one()) == (True, True)
        assert tuple(conn.execute(INVITE_TOKEN_GUARD_FUNCTION).one()) == (
            False,
            ["search_path=pg_catalog, public, pg_temp"],
            False,
        )
