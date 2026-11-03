"""The contact hold (ZIF-58): an erased client still hears about their cancellations and refunds.

The erase core (0064) keeps an erased client's email and locale in client_erasure_holds when the
client must still be told something. The API role can neither read nor write a hold; the worker
sends the cancellation and refund emails through it and deletes it once nothing is left to tell,
and never later than its purge date, at most 31 days after the erasure."""

import uuid

import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.db import tenant_context
from tests.conftest import People, member_id, migrate_tenant_context
from tests.test_client_erasure_db import PAID_AND_KEPT, Pay, Seeded, hold_after, seed_person

HOLD_OF = text("SELECT email FROM client_erasure_holds WHERE client_id = :c")


def held(
    people: People,
    migrate_engine: Engine,
    *,
    payment: Pay = PAID_AND_KEPT,
    reason: str | None = "client_erased",
) -> Seeded:
    """A client of business A erased by its owner, as the erase route erases: one of its bookings
    ended by the erasure (reason), or money still owed (payment), so the erasure kept a hold."""
    seeded = seed_person(
        migrate_engine,
        people.a,
        member_id(people.a, people.only_a),
        people.both,
        days_ago=10,
        payment=payment,
    )
    assert hold_after(migrate_engine, seeded, people.both, reason=reason, announce=True)
    return seeded


def hold_address(migrate_engine: Engine, seeded: Seeded) -> str | None:
    with migrate_tenant_context(migrate_engine, seeded.tenant_id) as conn:
        found: str | None = conn.scalar(HOLD_OF, {"c": seeded.client_id})
    return found


HOLD_STATEMENTS = {
    "SELECT": "SELECT email FROM client_erasure_holds",
    "INSERT": """
        INSERT INTO client_erasure_holds (tenant_id, client_id, email, purge_after)
        VALUES (current_setting('app.tenant_id')::uuid, :c, 'probe@example.com', now())""",
    "UPDATE": "UPDATE client_erasure_holds SET email = 'probe@example.com'",
    "DELETE": "DELETE FROM client_erasure_holds",
}


# ZIF-58 F29 FENCE. Wrong impl: the hold table left with the privileges the app role gets by
# default (no REVOKE in 0064): the API could read an erased client's address or write one. As the
# app role, in the hold's own business, each statement is refused and the hold stays as it was.
@pytest.mark.parametrize("statement", list(HOLD_STATEMENTS))
def test_the_api_role_can_neither_read_nor_write_a_hold(
    people: People, migrate_engine: Engine, statement: str
) -> None:
    seeded = held(people, migrate_engine)

    with pytest.raises(DBAPIError) as error, tenant_context(people.a) as session:
        session.execute(text(HOLD_STATEMENTS[statement]), {"c": uuid.uuid7()})

    assert isinstance(error.value.orig, InsufficientPrivilege)
    assert hold_address(migrate_engine, seeded) == seeded.email
