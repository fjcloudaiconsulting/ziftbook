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
SNAPSHOT = ("free_cancellation_hours", "reschedule_cutoff_hours")


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
            assert dict(conn.execute(NEW_COLUMNS).tuples().all()) == {
                "cancellation_policy_text": "YES"
            }
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
        assert dict(conn.execute(NEW_COLUMNS).tuples().all()) == {
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
        # booking_events' append-only shape is. Exactly these two columns, no others.
        assert sorted(conn.scalars(NO_UPDATE)) == sorted(SNAPSHOT)


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
            not_null = dict(
                conn.execute(
                    text(
                        "SELECT column_name, is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'time_off' AND column_name IN ('starts_at', 'ends_at')"
                    )
                )
                .tuples()
                .all()
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
