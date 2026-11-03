"""An erased client is a final tombstone (ZIF-58, migration 0063): the app role never writes
erased_at, the combined CHECK holds an erased row to nothing about the person, one trigger keeps
it erased, whoever runs the UPDATE, and another lets no new booking or consent attach to it."""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import CheckViolation, InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app import clients
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    People,
    fresh_email,
    member_id,
    migrate_tenant_context,
    signed_in,
    wait_until_blocked,
)
from tests.test_bookings_db import insert_booking, seed_service
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


# ZIF-58 F4 FENCE, both sides. Wrong impl: a CHECK that lets a live row have no name, or a name
# over 100 characters (the bound ck_clients_name_length held before 0063 rewrote it). The positive
# control: a blank tombstone is accepted, so the refusals above are not a CHECK refusing every
# erased row.
@pytest.mark.parametrize("name", ["", "x" * 101], ids=["empty", "101"])
def test_a_live_row_needs_a_name_of_1_to_100_and_a_blank_tombstone_is_accepted(
    people: People, migrate_engine: Engine, name: str
) -> None:
    with (
        pytest.raises(DBAPIError) as error,
        migrate_tenant_context(migrate_engine, people.a) as conn,
    ):
        conn.execute(
            text("""
            INSERT INTO clients (tenant_id, name)
            VALUES (current_setting('app.tenant_id')::uuid, :name)
            """),
            {"name": name},
        )
    assert isinstance(error.value.orig, CheckViolation)
    assert constraint_of(error) == "ck_clients_name_or_erased"

    assert seed_tombstone(migrate_engine, people.a)
    with migrate_tenant_context(migrate_engine, people.a) as conn:  # and 100 is a name
        conn.execute(
            text("INSERT INTO clients (tenant_id, name) VALUES (:t, :name)"),
            {"t": people.a, "name": "x" * 100},
        )


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


def consent_for(client_id: uuid.UUID) -> dict[str, object]:
    return {
        "client_id": client_id,
        "policy_version": "2026-09-01",
        "purposes": {"sms": True},
        "source": "merchant",
        "ip": "192.0.2.4",
        "user_agent": "Browser/1",
    }


def book_directly(tenant_id: uuid.UUID, client_id: uuid.UUID, worker: uuid.UUID, days: int) -> None:
    """A booking INSERT as the app role, with no route in front of it, `days` from now."""
    service_id = seed_service(tenant_id)
    starts = datetime.now(UTC) + timedelta(days=days)
    with tenant_context(tenant_id) as session:
        insert_booking(
            session,
            client_id=client_id,
            worker_id=worker,
            service_id=service_id,
            starts_at=starts,
            ends_at=starts + timedelta(minutes=30),
        )


# ZIF-58 FENCE (an erased client gains nothing). Wrong impl: no trigger on consents or bookings, so
# a route that forgets its erased_at filter, or a direct INSERT as the app role, attaches a new
# consent (with the person's address and browser) or a booking (with a typed name) to a tombstone.
# The same writes for a live client go through.
@pytest.mark.parametrize("child", ["consents", "bookings"])
def test_nothing_new_attaches_to_an_erased_client(
    people: People, migrate_engine: Engine, child: str
) -> None:
    worker = member_id(people.a, people.both)
    live = seed_client(people.a, name="Live")
    gone = seed_tombstone(migrate_engine, people.a)

    def attach(client_id: uuid.UUID, days: int) -> None:
        if child == "consents":
            with tenant_context(people.a) as session:
                clients.record_consents(session, **consent_for(client_id))  # type: ignore[arg-type]
        else:
            book_directly(people.a, client_id, worker, days)

    attach(live, 3)
    with pytest.raises(DBAPIError) as error:
        attach(gone, 4)
    assert isinstance(error.value.orig, CheckViolation)
    assert constraint_of(error) == "clients_erased_children"


# ZIF-58 FENCE. Wrong impl: the trigger reads the client without FOR SHARE (or filters on erased_at
# in its WHERE, which skips the row instead of waiting). A consent's foreign key takes only KEY
# SHARE, which an erasure's NO KEY UPDATE does not block, so an INSERT racing an erasure in flight
# would land on the tombstone; with FOR SHARE it waits, re-reads the row and is refused.
def test_a_consent_racing_an_erasure_waits_and_is_refused(
    people: People, migrate_engine: Engine, app_engine: Engine
) -> None:
    client_id = seed_client(people.a, name="Racing", email=fresh_email())
    refused: dict[str, BaseException] = {}

    def write() -> None:
        try:
            with tenant_context(people.a) as session:
                clients.record_consents(session, **consent_for(client_id))  # type: ignore[arg-type]
        except DBAPIError as error:
            refused["error"] = error

    writer = threading.Thread(target=write)
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        conn.execute(ERASE, {"id": client_id})  # holds the row lock until this block commits
        writer.start()
        wait_until_blocked(app_engine, 1)
    writer.join(timeout=10)
    assert not writer.is_alive()

    error = refused["error"]
    assert isinstance(error, DBAPIError)
    assert isinstance(error.orig, CheckViolation)
    assert error.orig.diag.constraint_name == "clients_erased_children"
