"""clients and consents (ZIF-49): the tables' own constraints and grants, and the module-level
helpers (find_or_create, record_consents, current_consents) with no route to go through."""

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation, InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import clients
from app.db import tenant_context
from app.errors import ApiError
from tests.conftest import People, fresh_email, wait_until_blocked


def seed_client(
    tenant_id: uuid.UUID,
    *,
    name: str = "Client",
    email: str | None = None,
    phone: str | None = None,
    locale: str | None = None,
    client_note: str | None = None,
    internal_note: str | None = None,
    user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert a client directly, as the app role in tenant_context, with fields no route sets
    (client_note, internal_note, user_id) available for seeding."""
    with tenant_context(tenant_id) as session:
        client_id: uuid.UUID = session.scalar(
            text("""
            INSERT INTO clients (tenant_id, name, email, phone, locale, client_note,
                                  internal_note, user_id)
            VALUES (current_setting('app.tenant_id')::uuid, :name, :email, :phone, :locale,
                    :client_note, :internal_note, :user_id)
            RETURNING id
            """),
            {
                "name": name,
                "email": email,
                "phone": phone,
                "locale": locale,
                "client_note": client_note,
                "internal_note": internal_note,
                "user_id": user_id,
            },
        )
    return client_id


def row_of(tenant_id: uuid.UUID, client_id: uuid.UUID) -> dict[str, Any]:
    """A client row exactly as stored, read in tenant_context."""
    with tenant_context(tenant_id) as session:
        return dict(
            session.execute(
                text("""
                SELECT name, email, phone, locale, client_note, internal_note, user_id
                FROM clients WHERE id = :id
                """),
                {"id": client_id},
            )
            .mappings()
            .one()
        )


# 1: FENCE. Wrong impl: SELECT-then-INSERT instead of one INSERT ... ON CONFLICT ... DO UPDATE.
def test_find_or_create_survives_two_bookings_of_the_same_address_at_once(
    people: People, app_engine: Engine
) -> None:
    email = fresh_email()
    inserted = threading.Event()
    release = threading.Event()
    results: dict[str, Any] = {}

    def thread_a() -> None:
        with tenant_context(people.a) as session:
            results["a"] = clients.find_or_create(
                session, name="Booker A", email=email, phone=None, locale=None
            )
            inserted.set()
            release.wait(timeout=10)  # holds the transaction open until B has queued behind it

    def thread_b() -> None:
        try:
            with tenant_context(people.a) as session:
                results["b"] = clients.find_or_create(
                    session, name="Booker B", email=email, phone="+31 6 00 00 00 00", locale="en"
                )
        except Exception as error:  # would be a bug in find_or_create, not the expected outcome
            results["b"] = error

    thread_a_handle = threading.Thread(target=thread_a)
    thread_a_handle.start()
    assert inserted.wait(timeout=10)
    thread_b_handle = threading.Thread(target=thread_b)
    thread_b_handle.start()
    wait_until_blocked(app_engine, 1)
    release.set()
    thread_a_handle.join(timeout=10)
    thread_b_handle.join(timeout=10)
    assert not thread_a_handle.is_alive()
    assert not thread_b_handle.is_alive()

    assert not isinstance(results.get("b"), Exception), results.get("b")
    assert results["a"].id == results["b"].id
    with tenant_context(people.a) as session:
        count = session.scalar(text("SELECT count(*) FROM clients WHERE email = :e"), {"e": email})
    assert count == 1


# 2: FENCE. Wrong impl: add client_note/internal_note/user_id to the DO UPDATE SET list.
def test_find_or_create_never_touches_the_notes_or_the_user_id(people: People) -> None:
    email = fresh_email()
    seeded_user = uuid.uuid7()
    client_id = seed_client(
        people.a,
        name="Old Name",
        email=email,
        phone="+31600000000",
        client_note="Client note",
        internal_note="Internal note",
        user_id=seeded_user,
    )

    with tenant_context(people.a) as session:
        found = clients.find_or_create(
            session, name="New Name", email=email, phone="+31611111111", locale="nl"
        )

    assert found.id == client_id
    row = row_of(people.a, client_id)
    assert row["name"] == "New Name"
    assert row["phone"] == "+31611111111"
    assert row["locale"] == "nl"
    assert row["client_note"] == "Client note"
    assert row["internal_note"] == "Internal note"
    assert row["user_id"] == seeded_user


# 3: GUARD.
def test_a_booking_with_no_email_creates_a_new_client_every_time(people: People) -> None:
    with tenant_context(people.a) as session:
        first = clients.find_or_create(session, name="Jan", email=None, phone=None, locale=None)
        second = clients.find_or_create(session, name="Jan", email=None, phone=None, locale=None)

    assert first.id != second.id
    with tenant_context(people.a) as session:
        count = session.scalar(
            text("SELECT count(*) FROM clients WHERE name = 'Jan' AND email IS NULL")
        )
    assert count == 2


# 4: FENCE. Wrong impl: drop 0024's REVOKE ALL / GRANT SELECT / GRANT INSERT (...) block.
def test_the_app_role_cannot_update_or_delete_a_consent(people: People, app_engine: Engine) -> None:
    client_id = seed_client(people.a, email=fresh_email())
    with tenant_context(people.a) as session:
        clients.record_consents(
            session,
            client_id=client_id,
            policy_version="2026-09-01",
            purposes={"marketing_email": True},
            source="merchant",
            ip=None,
            user_agent=None,
        )

    with pytest.raises(DBAPIError) as update_error, app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(text("UPDATE consents SET granted = false"))
    assert isinstance(update_error.value.orig, InsufficientPrivilege)

    with pytest.raises(DBAPIError) as delete_error, app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(text("DELETE FROM consents"))
    assert isinstance(delete_error.value.orig, InsufficientPrivilege)


# 5: FENCE. Wrong impl: GRANT INSERT ON consents TO ziftbook_app (table-wide, no column list).
def test_the_app_role_cannot_backdate_a_consent(people: People, app_engine: Engine) -> None:
    client_id = seed_client(people.a, email=fresh_email())

    with pytest.raises(DBAPIError) as error, app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text("""
            INSERT INTO consents (tenant_id, client_id, purpose, granted, text_shown,
                                   policy_version, source, created_at)
            VALUES (current_setting('app.tenant_id')::uuid, :client_id, 'marketing_email', true,
                    'text', '2026-09-01', 'merchant', now() - interval '1 year')
            """),
            {"client_id": client_id},
        )
    assert isinstance(error.value.orig, InsufficientPrivilege)

    with app_engine.begin() as conn:  # the same insert, minus created_at, succeeds
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text("""
            INSERT INTO consents (tenant_id, client_id, purpose, granted, text_shown,
                                   policy_version, source)
            VALUES (current_setting('app.tenant_id')::uuid, :client_id, 'marketing_email', true,
                    'text', '2026-09-01', 'merchant')
            """),
            {"client_id": client_id},
        )


# 6: FENCE. Wrong impl: drop REVOKE DELETE ON clients FROM ziftbook_app.
def test_the_app_role_cannot_delete_a_client(people: People, app_engine: Engine) -> None:
    seed_client(people.a, email=fresh_email())

    with pytest.raises(DBAPIError) as error, app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(text("DELETE FROM clients"))
    assert isinstance(error.value.orig, InsufficientPrivilege)


# 7: FENCE. Wrong impl: shorten 0024's CONSENT_COLUMNS to "tenant_id, client_id, purpose, granted".
def test_every_consent_column_but_id_and_created_at_is_insertable(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        all_columns = set(
            conn.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'consents'"
                )
            )
        )
        insertable = {
            column
            for column in all_columns
            if conn.scalar(
                text("SELECT has_column_privilege('ziftbook_app', 'consents', :col, 'INSERT')"),
                {"col": column},
            )
        }
    assert insertable == all_columns - {"id", "created_at"}


# 8: FENCE. Wrong impl: ORDER BY client_id, purpose, created_at DESC in CURRENT_CONSENTS.
def test_the_current_consent_is_the_newest_row_by_id_not_by_created_at(
    people: People, migrate_engine: Engine
) -> None:
    client_id = seed_client(people.a, email=fresh_email())
    lower_id, higher_id = sorted([uuid.uuid7(), uuid.uuid7()])
    now = datetime.now(UTC)
    insert = text("""
        INSERT INTO consents (id, tenant_id, client_id, purpose, granted, text_shown,
                               policy_version, source, created_at)
        VALUES (:id, current_setting('app.tenant_id')::uuid, :client_id, 'marketing_email',
                :granted, 'text', '2026-09-01', 'merchant', :created_at)
    """)
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        # The lower id (older) is the grant, timestamped a second into the future.
        conn.execute(
            insert,
            {
                "id": lower_id,
                "client_id": client_id,
                "granted": True,
                "created_at": now + timedelta(seconds=1),
            },
        )
        # The higher id (newer) is the withdrawal, timestamped now: id says it came later, its
        # created_at doesn't.
        conn.execute(
            insert,
            {"id": higher_id, "client_id": client_id, "granted": False, "created_at": now},
        )

    with tenant_context(people.a) as session:
        result = clients.current_consents(session, [client_id])
    assert result[client_id]["marketing_email"].granted is False


# 9: FENCE. Wrong impl: `return granted` in mailable.
def test_an_imported_consent_is_never_mailable(people: People, migrate_engine: Engine) -> None:
    client_id = seed_client(people.a, email=fresh_email())
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text("""
            INSERT INTO consents (tenant_id, client_id, purpose, granted, text_shown,
                                   policy_version, source)
            VALUES (current_setting('app.tenant_id')::uuid, :client_id, 'marketing_email', true,
                    'text', '2026-09-01', 'import')
            """),
            {"client_id": client_id},
        )
    with tenant_context(people.a) as session:
        clients.record_consents(
            session,
            client_id=client_id,
            policy_version="2026-09-01",
            purposes={"sms": True},
            source="booking_page",
            ip=None,
            user_agent=None,
        )
        result = clients.current_consents(session, [client_id])

    assert result[client_id]["marketing_email"] == clients.ConsentOut(
        granted=True, source="import", mailable=False
    )
    assert result[client_id]["sms"].mailable is True


# 10: FENCE + positive control. Wrong impl: a plain `client_id uuid REFERENCES clients (id)`.
def test_a_consent_cannot_point_at_another_businesses_client(people: People) -> None:
    client_id = seed_client(people.a, email=fresh_email())
    insert = text("""
        INSERT INTO consents (tenant_id, client_id, purpose, granted, text_shown,
                               policy_version, source)
        VALUES (current_setting('app.tenant_id')::uuid, :client_id, 'marketing_email', true,
                'text', '2026-09-01', 'merchant')
    """)

    with pytest.raises(DBAPIError) as error, tenant_context(people.b) as session:
        session.execute(insert, {"client_id": client_id})
    assert isinstance(error.value.orig, ForeignKeyViolation)

    with tenant_context(people.a) as session:  # positive control: A's own client, from A
        session.execute(insert, {"client_id": client_id})
        count = session.scalar(
            text("SELECT count(*) FROM consents WHERE client_id = :c"), {"c": client_id}
        )
    assert count == 1


# 11: FENCE (runnable) + positive control. Wrong impl: keep only the unique constraint the
# composite foreign key targets, drop the row-level security enable_tenant_isolation gives.
def test_a_client_of_another_business_is_invisible(people: People) -> None:
    client_id = seed_client(people.a, email=fresh_email())

    with tenant_context(people.b) as session:
        seen = session.execute(
            text("SELECT id FROM clients WHERE id = :id"), {"id": client_id}
        ).first()
        listed = set(session.scalars(text("SELECT id FROM clients")))
    assert seen is None
    assert client_id not in listed

    with tenant_context(people.a) as session:  # positive control
        seen_a = session.execute(
            text("SELECT id FROM clients WHERE id = :id"), {"id": client_id}
        ).first()
    assert seen_a is not None


# 12: GUARD.
def test_two_businesses_can_hold_the_same_client_email(people: People) -> None:
    email = fresh_email()
    a_id = seed_client(people.a, email=email)
    b_id = seed_client(people.b, email=email)
    assert a_id != b_id


# 13: GUARD.
def test_the_database_refuses_an_uppercase_email_and_an_over_long_note(
    people: People, migrate_engine: Engine
) -> None:
    with pytest.raises(IntegrityError) as email_error, migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text(
                "INSERT INTO clients (tenant_id, name, email) VALUES "
                "(current_setting('app.tenant_id')::uuid, 'Ada', 'Ada@Example.com')"
            )
        )
    assert isinstance(email_error.value.orig, CheckViolation)

    with pytest.raises(IntegrityError) as note_error, migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text(
                "INSERT INTO clients (tenant_id, name, client_note) VALUES "
                "(current_setting('app.tenant_id')::uuid, 'Ada', :note)"
            ),
            {"note": "x" * 2001},
        )
    assert isinstance(note_error.value.orig, CheckViolation)


# 13b: GUARD.
def test_a_version_that_omits_a_purpose_is_a_422_not_a_500(
    people: People, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(clients.CONSENT_TEXTS, "test-subset", {"sms": "You may text me."})

    with tenant_context(people.a) as session, pytest.raises(ApiError) as error:
        clients.record_consents(
            session,
            client_id=uuid.uuid7(),
            policy_version="test-subset",
            purposes={"marketing_email": True},
            source="merchant",
            ip=None,
            user_agent=None,
        )
    assert (error.value.status_code, error.value.code) == (422, "unknown_policy_version")
