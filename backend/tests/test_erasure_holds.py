"""The contact hold (ZIF-58): an erased client still hears about their cancellations and refunds.

The erase core (0064) keeps an erased client's email and locale in client_erasure_holds when the
client must still be told something. The API role can neither read nor write a hold; the worker
sends the cancellation and refund emails through it and deletes it once nothing is left to tell,
and never later than its purge date, at most 31 days after the erasure."""

import uuid

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import CheckViolation, InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app import bookings
from app.db import tenant_context
from app.mail import render
from tests.conftest import (
    People,
    jobs_as_the_worker,
    member_id,
    migrate_tenant_context,
    sent_to,
)
from tests.test_booking_email import REPLY_TO, outbox_for, run_send_booking, set_client, set_contact
from tests.test_client_erasure_db import PAID_AND_KEPT, Pay, Seeded, hold_after, seed_person
from tests.test_erased_client_reads import (  # noqa: F401 -- fixtures
    app,
    booked,
    clean_outbox,
    links_of,
    owner,
    ready,
)

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


WRITE_HOLD = text("""
INSERT INTO client_erasure_holds (tenant_id, client_id, email, created_at, purge_after)
VALUES (current_setting('app.tenant_id')::uuid, :c, 'held@example.com',
        now() - CAST(:age AS interval), now() + CAST(:left AS interval))
""")


# ZIF-58 F33 FENCE. Wrong impl: no CHECK on the hold's purge date, so a hold written (or moved) by
# anything but the core could keep an erased client's address past the one-month answer window.
# As ziftbook_migrate, the only role that may write one: a purge date more than 31 days after the
# hold was written is refused, on INSERT and on UPDATE; exactly 31 days is the limit.
@pytest.mark.parametrize(
    ("age", "left", "refused"),
    [
        ("0 days", "32 days", True),
        ("10 days", "22 days", True),  # backdated: 32 days after it was written
        ("10 days", "21 days", False),  # exactly 31 days after
        ("0 days", "30 days", False),  # the core's own
    ],
)
def test_a_hold_cannot_outlive_31_days(
    people: People, migrate_engine: Engine, age: str, left: str, refused: bool
) -> None:
    seeded = seed_person(
        migrate_engine, people.a, member_id(people.a, people.only_a), people.both, days_ago=10
    )
    write = {"c": seeded.client_id, "age": age, "left": left}
    if not refused:
        with migrate_tenant_context(migrate_engine, people.a) as conn:
            conn.execute(WRITE_HOLD, write)
        assert hold_address(migrate_engine, seeded) == "held@example.com"
        return
    with (
        pytest.raises(DBAPIError) as error,
        migrate_tenant_context(migrate_engine, people.a) as conn,
    ):
        conn.execute(WRITE_HOLD, write)
    assert isinstance(error.value.orig, CheckViolation)
    assert error.value.orig.diag.constraint_name == "ck_client_erasure_holds_purge_after"

    with migrate_tenant_context(migrate_engine, people.a) as conn:
        conn.execute(WRITE_HOLD, {**write, "left": "1 day"})
    with (
        pytest.raises(DBAPIError) as error,
        migrate_tenant_context(migrate_engine, people.a) as conn,
    ):
        conn.execute(
            text(
                "UPDATE client_erasure_holds SET purge_after = now() + CAST(:left AS interval)"
                " WHERE client_id = :c"
            ),
            write,
        )
    assert isinstance(error.value.orig, CheckViolation)
    assert error.value.orig.diag.constraint_name == "ck_client_erasure_holds_purge_after"


# The erase route's work before the core, as PR 7 will do it: the client's live bookings ended by
# the owner, each with an event that gives the erasure as its reason.
END_ANNOUNCED = text("""
WITH ended AS (
  UPDATE bookings SET status = 'cancelled_by_merchant'
  WHERE client_id = :id
    AND ((status = 'confirmed' AND starts_at > now())
         OR (status = 'pending' AND expires_at > now()))
  RETURNING id)
INSERT INTO booking_events (tenant_id, booking_id, event, actor_user_id, details)
SELECT current_setting('app.tenant_id')::uuid, id, 'cancelled_by_merchant', :actor,
       '{"reason": "client_erased"}'::jsonb
FROM ended
""")


def erase_announcing(
    migrate_engine: Engine, people: People, client_id: object, *, announce: bool = True
) -> None:
    """Erase a client of business A as its owner does: the booking lock, the live bookings ended
    with the erasure's reason, then the core (announce false: a re-applied erasure)."""
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        conn.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})
        conn.execute(END_ANNOUNCED, {"id": client_id, "actor": people.both})
        erased = conn.scalar(
            text("SELECT erase_client_core(:id, :announce)"),
            {"id": client_id, "announce": announce},
        )
    assert erased is not None


def address_of(tenant_id: uuid.UUID, client_id: object) -> str:
    with tenant_context(tenant_id) as session:
        found: str = session.scalar(
            text("SELECT email FROM clients WHERE id = :id"), {"id": client_id}
        )
    return found


# ZIF-58 F30 FENCE. Wrong impl: a send that uses the hold for every template, so a confirmation
# (with a freshly minted manage link) reaches an erased client; or one that reads the business's
# language instead of the hold's locale. Only booking_cancelled, booking_expired and
# booking_refund_failed go to an erased client, at the address its erasure kept, in its locale,
# replying to the business; nothing at all goes when the erasure kept no hold (a re-applied
# erasure with nothing owed).
@pytest.mark.parametrize("announce", [True, False], ids=["held", "no hold"])
def test_an_erased_client_hears_of_a_cancellation_at_the_held_address_only(
    people: People,
    owner: TestClient,  # noqa: F811
    migrate_engine: Engine,
    booked: dict[str, str],  # noqa: F811
    clean_outbox: None,  # noqa: F811
    announce: bool,
) -> None:
    set_contact(owner)
    client_id = uuid.UUID(booked["client"])
    address = address_of(people.a, client_id)
    set_client(people.a, client_id, locale="pt")  # not the business's language
    erase_announcing(migrate_engine, people, client_id, announce=announce)

    with jobs_as_the_worker():
        cancelled = run_send_booking(people.a, booked["pending"], "booking_cancelled")
        confirmed = run_send_booking(people.a, booked["started"], "booking_confirmed")

    assert outbox_for(people.a, confirmed.id) is None
    assert links_of(people.a, booked["started"]) == 0
    if not announce:
        assert sent_to(address) == []
        assert outbox_for(people.a, cancelled.id) is None
        return
    (sent,) = sent_to(address)
    assert sent["Subject"] == render("booking_cancelled", "pt")[0]
    assert sent["Headers"]["Reply-To"] == [REPLY_TO]
    assert outbox_for(people.a, cancelled.id)["recipient_id"] == client_id
