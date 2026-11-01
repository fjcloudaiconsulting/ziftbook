"""The console no longer shows an erased client (ZIF-58, spec 2.3): the client list skips it,
PATCH and consents answer 404, it cannot be booked, the booking lists mark it with client_erased,
and the team gets no booking mail naming it. Erased rows are written here as the erase function
will write them, as the migrate role: the app role cannot write erased_at (0063)."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text

from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    People,
    fresh_email,
    migrate_tenant_context,
    signed_in,
)

# What the erase function blanks of a client and of its bookings (spec 1.1, 1.3).
ERASE_BOOKINGS = text("""
UPDATE bookings SET client_name = NULL, client_phone = NULL, client_message = NULL,
                    decline_message = NULL, checkout_hash = NULL
WHERE client_id = :id
""")
ERASE_CLIENT = text("""
UPDATE clients SET erased_at = now(), name = '', user_id = NULL, email = NULL, phone = NULL,
                   locale = NULL, client_note = NULL, internal_note = NULL
WHERE id = :id
""")


def erase_in(conn: Connection, client_id: object) -> None:
    conn.execute(ERASE_BOOKINGS, {"id": client_id})
    conn.execute(ERASE_CLIENT, {"id": client_id})


def erase_by_hand(migrate_engine: Engine, tenant_id: uuid.UUID, client_id: object) -> None:
    """Erase a client as the erase function will, as the migrate role, committed."""
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        erase_in(conn, client_id)


def client_of(tenant_id: uuid.UUID, booking_id: str) -> uuid.UUID:
    with tenant_context(tenant_id) as session:
        found: uuid.UUID = session.scalar(
            text("SELECT client_id FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    return found


def consent_rows(tenant_id: uuid.UUID, client_id: object) -> int:
    with tenant_context(tenant_id) as session:
        count: int = session.scalar(
            text("SELECT count(*) FROM consents WHERE client_id = :id"), {"id": client_id}
        )
    return count


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture
def added(owner: TestClient) -> Iterator[tuple[str, str]]:
    """Two clients made in the console: one to erase, one that stays."""
    gone = owner.post("/api/clients", json={"name": "Gone Client", "email": fresh_email()})
    kept = owner.post("/api/clients", json={"name": "Kept Client", "email": fresh_email()})
    assert (gone.status_code, kept.status_code) == (201, 201)
    yield gone.json()["id"], kept.json()["id"]


# ZIF-58 F7 FENCE. Wrong impl: the list with no erased_at filter (the tombstone is listed, with
# its blank name).
def test_the_client_list_skips_an_erased_client(
    people: People, owner: TestClient, migrate_engine: Engine, added: tuple[str, str]
) -> None:
    gone, kept = added
    erase_by_hand(migrate_engine, people.a, gone)

    listed = [c["id"] for c in owner.get("/api/clients").json()]
    assert kept in listed
    assert gone not in listed
    # Paging past the tombstone does not bring it back either.
    paged = [c["id"] for c in owner.get("/api/clients", params={"before": kept}).json()]
    assert gone not in paged
    assert [c["id"] for c in owner.get("/api/clients", params={"q": "Client"}).json()] == [kept]
