"""An erased client is a final tombstone (ZIF-58, migration 0063): the app role never writes
erased_at, the combined CHECK holds an erased row to nothing about the person, and the trigger
keeps it erased, whoever runs the UPDATE."""

import uuid

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import CheckViolation, InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app import clients
from app.db import tenant_context
from app.main import create_app
from tests.conftest import People, fresh_email, migrate_tenant_context, signed_in
from tests.test_clients_db import row_of, seed_client

TOMBSTONE = text("""
INSERT INTO clients (tenant_id, name, erased_at)
VALUES (current_setting('app.tenant_id')::uuid, '', now())
RETURNING id
""")
ERASE = text("""
UPDATE clients SET erased_at = now(), name = '', email = NULL, phone = NULL, locale = NULL,
                   client_note = NULL, internal_note = NULL, user_id = NULL
WHERE id = :id
""")


def seed_tombstone(migrate_engine: Engine, tenant_id: uuid.UUID) -> uuid.UUID:
    """An erased client, written as the migrate role: the app role cannot write erased_at."""
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        client_id: uuid.UUID = conn.scalar(TOMBSTONE)
    return client_id


def constraint_of(error: pytest.ExceptionInfo[DBAPIError]) -> str | None:
    """The constraint a refused statement names (psycopg's diag)."""
    orig = error.value.orig
    assert orig is not None
    name: str | None = orig.diag.constraint_name  # type: ignore[attr-defined]
    return name


# ZIF-58 F2 FENCE. Wrong impl: 0063 without its REVOKE, so the table-level INSERT and UPDATE stay
# and the app role writes erased_at. Both writes are otherwise valid tombstones, so only the
# privilege can refuse them.
def test_the_app_role_cannot_write_erased_at(people: People) -> None:
    with pytest.raises(DBAPIError) as inserted, tenant_context(people.a) as session:
        session.execute(TOMBSTONE)
    assert isinstance(inserted.value.orig, InsufficientPrivilege)

    client_id = seed_client(people.a, name="Live")
    with pytest.raises(DBAPIError) as updated, tenant_context(people.a) as session:
        session.execute(ERASE, {"id": client_id})
    assert isinstance(updated.value.orig, InsufficientPrivilege)
    assert row_of(people.a, client_id)["name"] == "Live"


# ZIF-58 F3 FENCE. Wrong impl: a column list missing email or name: the booking upserts (REFRESH,
# KEEP), the console's insert and its note PATCH each write a column the app role must keep.
def test_every_write_the_app_makes_today_still_runs(people: People) -> None:
    email = fresh_email()
    with tenant_context(people.a) as session:
        created = clients.find_or_create(
            session, name="Booker", email=email, phone="+31600000000", locale="nl", proven=True
        )
        refreshed = clients.find_or_create(
            session, name="Booker Two", email=email, phone=None, locale=None, proven=True
        )
        kept = clients.find_or_create(
            session, name="Stranger", email=email, phone=None, locale="pt", proven=False
        )
        console = clients.insert(
            session, clients.ClientIn(name="Walk In", email=fresh_email(), phone="0612345678")
        )
    assert created.id == refreshed.id == kept.id
    assert row_of(people.a, created.id)["name"] == "Booker Two"

    owner: TestClient = signed_in(create_app(), people.a, people.both)
    patched = owner.patch(
        f"/api/clients/{console.id}", json={"client_note": "Note", "internal_note": "Own"}
    )
    assert patched.status_code == 200, patched.json()
    assert row_of(people.a, console.id)["internal_note"] == "Own"


ERASED_FIELDS = {
    "user_id": uuid.uuid7(),
    "email": "erased@example.com",
    "phone": "+31600000000",
    "locale": "en",
    "client_note": "A note",
    "internal_note": "A private note",
}


# ZIF-58 F4 FENCE. Wrong impl: a CHECK that forgets user_id or internal_note (or any other field),
# or that allows a name other than '' on an erased row. The constraint is named, not only the
# SQLSTATE, so another CHECK cannot stand in for it.
@pytest.mark.parametrize("field", [*ERASED_FIELDS, "name"])
def test_an_erased_row_holds_nothing_about_the_person(
    people: People, migrate_engine: Engine, field: str
) -> None:
    columns = "name" if field == "name" else f"name, {field}"
    values = ":name" if field == "name" else f":name, :{field}"
    params = {"name": "Kept Name" if field == "name" else "", **ERASED_FIELDS}
    with (
        pytest.raises(DBAPIError) as error,
        migrate_tenant_context(migrate_engine, people.a) as conn,
    ):
        conn.execute(
            text(f"""
            INSERT INTO clients (tenant_id, erased_at, {columns})
            VALUES (current_setting('app.tenant_id')::uuid, now(), {values})
            """),
            params,
        )
    assert isinstance(error.value.orig, CheckViolation)
    assert constraint_of(error) == "ck_clients_name_or_erased"


# ZIF-58 F4 FENCE, both sides. Wrong impl: a CHECK that lets a live row have no name. The positive
# control: a blank tombstone is accepted, so the refusals above are not a CHECK refusing every
# erased row.
def test_a_live_row_needs_a_name_and_a_blank_tombstone_is_accepted(
    people: People, migrate_engine: Engine
) -> None:
    with (
        pytest.raises(DBAPIError) as error,
        migrate_tenant_context(migrate_engine, people.a) as conn,
    ):
        conn.execute(
            text("""
            INSERT INTO clients (tenant_id, name)
            VALUES (current_setting('app.tenant_id')::uuid, '')
            """)
        )
    assert isinstance(error.value.orig, CheckViolation)
    assert constraint_of(error) == "ck_clients_name_or_erased"

    assert seed_tombstone(migrate_engine, people.a)


# ZIF-58 F5 FENCE. Wrong impl: no trigger (both updates succeed: each satisfies the CHECK), or a
# trigger WHEN (NEW.erased_at IS NOT NULL) (the un-erase passes). As ziftbook_migrate, the role the
# erase function runs as: the trigger binds it too.
@pytest.mark.parametrize(
    "change",
    [
        "erased_at = NULL, name = 'Back'",  # an un-erase that satisfies the CHECK
        "erased_at = erased_at - interval '1 year'",  # a rewritten erasure time
    ],
)
def test_an_erased_client_is_final(people: People, migrate_engine: Engine, change: str) -> None:
    client_id = seed_tombstone(migrate_engine, people.a)
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        before = conn.execute(
            text("SELECT name, erased_at FROM clients WHERE id = :id"), {"id": client_id}
        ).one()
    with (
        pytest.raises(DBAPIError) as error,
        migrate_tenant_context(migrate_engine, people.a) as conn,
    ):
        conn.execute(text(f"UPDATE clients SET {change} WHERE id = :id"), {"id": client_id})
    assert isinstance(error.value.orig, CheckViolation)
    assert constraint_of(error) == "clients_erased_final"
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        after = conn.execute(
            text("SELECT name, erased_at FROM clients WHERE id = :id"), {"id": client_id}
        ).one()
    assert after == before
