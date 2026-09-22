"""A migration's downgrade must undo exactly what its upgrade did, grants included."""

import os
import uuid
from urllib.parse import urlencode

import pytest
from alembic import command
from alembic.config import Config
from psycopg.errors import CheckViolation, NotNullViolation
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app.db import tenant_context
from tests.conftest import API_DIR, People, delete_bookings, member_id
from tests.test_bookings_db import seed_service

COLUMNS = text("SELECT column_name FROM information_schema.columns WHERE table_name = 'tenants'")
TABLE_PRIVILEGE = "SELECT has_table_privilege('ziftbook_app', 'tenants', :priv)"
COLUMN_PRIVILEGE = "SELECT has_column_privilege('ziftbook_app', 'tenants', 'name', 'UPDATE')"
NAME_ACL = """
SELECT attacl FROM pg_attribute WHERE attrelid = 'tenants'::regclass AND attname = 'name'
"""
FIVE_ARG = "SELECT to_regprocedure('complete_sign_up(bytea,text,text,text,text)')"
THREE_ARG = "SELECT to_regprocedure('complete_sign_up(bytea,text,text)')"
THREE_ARG_TO_APP = (
    "SELECT has_function_privilege('ziftbook_app', 'complete_sign_up(bytea,text,text)', 'EXECUTE')"
)
THREE_ARG_TO_PUBLIC = (
    "SELECT has_function_privilege('public', 'complete_sign_up(bytea,text,text)', 'EXECUTE')"
)
INVITES_TABLE = "SELECT to_regclass('invites')"
ACCEPT_INVITE = "SELECT to_regprocedure('accept_invite(bytea,text)')"
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
        assert conn.scalar(text(FIVE_ARG)) is not None
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
        assert conn.scalar(text(FIVE_ARG)) is not None


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
        assert conn.scalar(text("SELECT has_table_privilege('ziftbook_app', 'bookings', 'INSERT')"))
        assert conn.scalar(text("SELECT has_table_privilege('ziftbook_app', 'bookings', 'UPDATE')"))


# F19 (T7) - 0027's round trip: both columns, both CHECKs, and the app's privileges on them.
# Kills: a downgrade that drops one column and not the other, or takes cancellation_policy_text
# with them; an upgrade that adds them nullable or without the CHECK; a 0027 that copies 0026's
# REVOKE ALL ... GRANT pattern and loses a privilege on the way back up.
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


def test_downgrading_and_upgrading_0027_restores_both_thresholds(
    migrated: None, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    try:
        command.downgrade(cfg, "0026")
        with migrate_engine.connect() as conn:
            assert dict(conn.execute(NEW_COLUMNS).tuples().all()) == {
                "cancellation_policy_text": "YES"
            }
            assert conn.scalars(NEW_CHECKS).all() == []
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert dict(conn.execute(NEW_COLUMNS).tuples().all()) == {
            "cancellation_policy_text": "YES",
            "free_cancellation_hours": "NO",
            "reschedule_cutoff_hours": "NO",
        }
        assert sorted(conn.scalars(NEW_CHECKS)) == [
            "ck_bookings_free_cancellation_hours",
            "ck_bookings_reschedule_cutoff_hours",
        ]
        for column in ("free_cancellation_hours", "reschedule_cutoff_hours"):
            for privilege in ("INSERT", "SELECT", "UPDATE"):
                assert conn.scalar(
                    text("SELECT has_column_privilege('ziftbook_app', 'bookings', :c, :p)"),
                    {"c": column, "p": privilege},
                ), (column, privilege)


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
