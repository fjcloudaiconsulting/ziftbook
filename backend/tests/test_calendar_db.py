"""Real runtime-role checks for calendar storage; no provider call or global fixture change."""

from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.db import tenant_context
from tests.conftest import People, member_id, migrate_tenant_context

TABLES = ("google_calendar_connections", "google_calendar_oauth_attempts")


@pytest.fixture
def calendar_people(people: People, migrate_engine: Engine) -> Iterator[People]:
    yield people
    for tenant in (people.a, people.b):
        with migrate_tenant_context(migrate_engine, tenant) as conn:
            conn.execute(text("DELETE FROM time_off WHERE source = 'google'"))
            conn.execute(text("DELETE FROM google_calendar_oauth_attempts"))
            conn.execute(text("DELETE FROM google_calendar_connections"))


def insert_connection(conn: Connection | Session, tenant: UUID, member: UUID, user: UUID) -> UUID:
    found: UUID = conn.execute(
        text("""
        INSERT INTO google_calendar_connections
          (tenant_id, member_id, user_id, key_id, wrapped_dek, wrap_nonce, payload_nonce,
           ciphertext, next_sync_at)
        VALUES (:t, :m, :u, 'test-key', :d, :n, :n, :c, now()) RETURNING id
        """),
        {"t": tenant, "m": member, "u": user, "d": b"d" * 48, "n": b"n" * 12, "c": b"c" * 16},
    ).scalar_one()
    return found


def insert_attempt(conn: Connection | Session, tenant: UUID, member: UUID, user: UUID) -> UUID:
    found: UUID = conn.execute(
        text("""
        INSERT INTO google_calendar_oauth_attempts
          (tenant_id, member_id, user_id, state_hash, session_hash, key_id, wrapped_dek,
           wrap_nonce, payload_nonce, ciphertext, expires_at, locale, return_path, redirect_uri)
        VALUES (:t, :m, :u, :h, :h, 'test-key', :d, :n, :n, :c, now() + interval '10 minutes',
                'en', '/en/my-hours', 'https://example.test/api/google-calendar/callback')
        RETURNING id
        """),
        {
            "t": tenant,
            "m": member,
            "u": user,
            "h": b"h" * 32,
            "d": b"d" * 48,
            "n": b"n" * 12,
            "c": b"c" * 16,
        },
    ).scalar_one()
    return found


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("role", ["app", "worker"])
def test_runtime_rls_hides_and_cannot_lock_other_tenants(
    calendar_people: People, app_engine: Engine, worker_engine: Engine, table: str, role: str
) -> None:
    p = calendar_people
    member = member_id(p.a, p.only_a)
    insert = insert_connection if table == TABLES[0] else insert_attempt
    with tenant_context(p.a) as conn:
        row_id = insert(conn, p.a, member, p.only_a)
    engine = app_engine if role == "app" else worker_engine
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(p.b)})
        assert conn.execute(text(f"SELECT id FROM {table} FOR UPDATE")).all() == []
        assert conn.execute(text(f"UPDATE {table} SET wrap_revision = 2 RETURNING id")).all() == []
        assert conn.execute(text(f"DELETE FROM {table} RETURNING id")).all() == []
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(p.a)})
        assert conn.execute(text(f"SELECT id FROM {table} FOR UPDATE")).scalar_one() == row_id
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(text(f"SELECT id FROM {table} FOR UPDATE"))
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(p.b)})
        insert(conn, p.a, member, p.only_a)


@pytest.mark.parametrize("table", TABLES)
def test_catalog_and_grants(migrate_engine: Engine, table: str) -> None:
    with migrate_engine.connect() as conn:
        row = conn.execute(
            text("""
          SELECT relrowsecurity, relforcerowsecurity, relowner::regrole::text FROM pg_class
          WHERE oid = to_regclass(:table)
        """),
            {"table": table},
        ).one()
        assert tuple(row) == (True, True, "ziftbook_migrate")
        for role in ("ziftbook_app", "ziftbook_worker"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert conn.scalar(
                    text("SELECT has_table_privilege(:r, :t, :p)"),
                    {"r": role, "t": table, "p": privilege},
                )
        assert not conn.scalar(
            text("SELECT has_table_privilege('public', :t, 'SELECT')"), {"t": table}
        )
        assert not conn.scalar(
            text("SELECT has_function_privilege('public', 'calendar_row_binding()', 'EXECUTE')")
        )
        assert not conn.scalar(
            text(
                "SELECT has_function_privilege('ziftbook_app', 'calendar_row_binding()', 'EXECUTE')"
            )
        )
        assert (
            conn.scalar(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name=:t AND column_name='id'"
                ),
                {"t": table},
            )
            == "uuidv7()"
        )


@pytest.mark.parametrize("table", TABLES)
def test_member_user_binding_and_immutable_encryption_generation(
    calendar_people: People, table: str
) -> None:
    p = calendar_people
    member = member_id(p.a, p.only_a)
    insert = insert_connection if table == TABLES[0] else insert_attempt
    with pytest.raises(IntegrityError), tenant_context(p.a) as conn:
        insert(conn, p.a, member, p.both)
    with tenant_context(p.a) as conn:
        insert(conn, p.a, member, p.only_a)
    with pytest.raises(IntegrityError), tenant_context(p.a) as conn:
        conn.execute(text(f"UPDATE {table} SET encryption_generation = 2"))
    with tenant_context(p.a) as conn:
        conn.execute(text(f"UPDATE {table} SET wrap_revision = 2"))
        assert conn.scalar(text(f"SELECT encryption_generation FROM {table}")) == 1


def test_connections_attempts_and_time_off_use_matching_tenant_and_member_fks(
    calendar_people: People,
) -> None:
    p = calendar_people
    a, b, owner = member_id(p.a, p.only_a), member_id(p.b, p.only_b), member_id(p.a, p.both)
    with tenant_context(p.a) as conn:
        a_connection = insert_connection(conn, p.a, a, p.only_a)
        insert_attempt(conn, p.a, owner, p.both)
    with tenant_context(p.b) as conn:
        b_connection = insert_connection(conn, p.b, b, p.only_b)
    for connection in (a_connection, b_connection):
        with pytest.raises(IntegrityError), tenant_context(p.a) as conn:
            conn.execute(
                text(
                    "UPDATE google_calendar_oauth_attempts "
                    "SET connection_id=:c, connection_generation=1"
                ),
                {"c": connection},
            )
        with pytest.raises(IntegrityError), tenant_context(p.a) as conn:
            google_block(conn, p.a, owner, connection)
    with pytest.raises(IntegrityError), tenant_context(p.a) as conn:
        insert_connection(conn, p.a, a, p.only_a)
    with pytest.raises(IntegrityError), tenant_context(p.a) as conn:
        insert_attempt(conn, p.a, owner, p.both)


def google_block(conn: Session, tenant: UUID, member: UUID, connection: UUID) -> None:
    conn.execute(
        text("""
      INSERT INTO time_off (tenant_id, member_id, source, external_id, calendar_connection_id,
                            starts_at, ends_at)
      VALUES (:t, :m, 'google', :external, :c, now(), now() + interval '1 hour')
    """),
        {"t": tenant, "m": member, "external": f"{connection}:v1:interval", "c": connection},
    )


@pytest.mark.parametrize(
    "changes",
    [
        "reason = 'private'",
        "calendar_connection_id = NULL",
        "external_id = 'wrong-prefix'",
        "starts_at = NULL, ends_at = NULL, first_day = CURRENT_DATE, last_day = CURRENT_DATE",
        "starts_at = '-infinity', ends_at = '-infinity'",
        "source = 'manual', external_id = NULL",
    ],
)
def test_google_blocks_require_connection_owned_bounded_instants(
    calendar_people: People, changes: str
) -> None:
    p = calendar_people
    member = member_id(p.a, p.only_a)
    with tenant_context(p.a) as conn:
        connection = insert_connection(conn, p.a, member, p.only_a)
        google_block(conn, p.a, member, connection)
    with pytest.raises(DBAPIError), tenant_context(p.a) as conn:
        conn.execute(text(f"UPDATE time_off SET {changes} WHERE source = 'google'"))


def test_old_manual_rows_remain_valid(calendar_people: People) -> None:
    p = calendar_people
    member = member_id(p.a, p.only_a)
    with tenant_context(p.a) as conn:
        conn.execute(
            text("""
          INSERT INTO time_off (tenant_id, member_id, first_day, last_day, reason)
          VALUES (:t, :m, CURRENT_DATE, CURRENT_DATE, 'manual reason')
        """),
            {"t": p.a, "m": member},
        )
        conn.execute(
            text("""
          INSERT INTO time_off (tenant_id, member_id, starts_at, ends_at)
          VALUES (:t, :m, now(), now() + interval '1 hour')
        """),
            {"t": p.a, "m": member},
        )
        assert (
            conn.scalar(text("SELECT count(*) FROM time_off WHERE calendar_connection_id IS NULL"))
            == 2
        )


@pytest.mark.parametrize(
    "changes",
    [
        "state = 'unknown'",
        "scheduling_state = 'parked'",
        "lease_id = uuidv7()",
        "coverage_start = now()",
        "coverage_start = now(), coverage_end = now()",
        "wrap_nonce = 'short'::bytea",
        "failure_count = -1",
        "token_revision = 0",
        "error_category = 'provider response text'",
        "state = 'revocation_pending'",
    ],
)
def test_connection_constraints(calendar_people: People, changes: str) -> None:
    p = calendar_people
    member = member_id(p.a, p.only_a)
    with tenant_context(p.a) as conn:
        insert_connection(conn, p.a, member, p.only_a)
    with pytest.raises(IntegrityError), tenant_context(p.a) as conn:
        conn.execute(text(f"UPDATE google_calendar_connections SET {changes}"))


@pytest.mark.parametrize(
    "changes",
    [
        "state_hash = 'short'::bytea",
        "session_hash = 'short'::bytea",
        "expires_at = created_at + interval '11 minutes'",
        "expires_at = 'infinity'",
        "state = 'exchanging'",
        "return_path = '//other.test'",
        "locale = 'de'",
        "connection_generation = 1",
    ],
)
def test_attempt_constraints(calendar_people: People, changes: str) -> None:
    p = calendar_people
    member = member_id(p.a, p.only_a)
    with tenant_context(p.a) as conn:
        insert_attempt(conn, p.a, member, p.only_a)
    with pytest.raises(IntegrityError), tenant_context(p.a) as conn:
        conn.execute(text(f"UPDATE google_calendar_oauth_attempts SET {changes}"))


def test_pending_credentials_prevent_identity_deletion(
    calendar_people: People, migrate_engine: Engine
) -> None:
    p = calendar_people
    member = member_id(p.a, p.only_a)
    with tenant_context(p.a) as conn:
        insert_connection(conn, p.a, member, p.only_a)
    with pytest.raises(IntegrityError), migrate_tenant_context(migrate_engine, p.a) as conn:
        conn.execute(text("DELETE FROM memberships WHERE id = :m"), {"m": member})
    with pytest.raises(IntegrityError), migrate_tenant_context(migrate_engine, p.a) as conn:
        conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": p.a})
