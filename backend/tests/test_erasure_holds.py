"""The contact hold (ZIF-58): an erased client still hears about their cancellations and refunds.

The erase core (0064) keeps an erased client's email and locale in client_erasure_holds when the
client must still be told something. The API role can neither read nor write a hold; the worker
sends the cancellation and refund emails through it and deletes it once nothing is left to tell,
and never later than its purge date, at most 31 days after the erasure."""

import asyncio
import json
import re
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from psycopg.errors import CheckViolation, InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app import bookings, erasure_holds, jobs, mail, payments, worker
from app.db import tenant_context
from app.mail import render
from app.main import create_app, openapi_document
from tests.conftest import (
    MAILGUN_CALLS,
    People,
    fresh_email,
    jobs_as_the_worker,
    member_id,
    migrate_tenant_context,
    new_client,
    save_setting,
    sent_to,
)
from tests.stripe_fake import FakeGateway
from tests.test_booking_email import REPLY_TO, outbox_for, run_send_booking, set_client, set_contact
from tests.test_booking_payments_api import account_of, make_shop, run, webhook
from tests.test_booking_refunds import booked as paid_booking
from tests.test_booking_refunds import settle_job
from tests.test_bookings_api import at
from tests.test_client_erasure_db import (
    ENDED_BY_ERASURE,
    ERASE_CLIENT,
    PAID_AND_KEPT,
    Pay,
    Seeded,
    booking_lock,
    hold_after,
    seed_person,
    session_hash,
)
from tests.test_erased_client_reads import (  # noqa: F401 -- fixtures
    app,
    booked,
    clean_outbox,
    client_of,
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
    # A live client of the same business: the INSERT would succeed if the role were allowed it.
    live = seed_person(
        migrate_engine, people.a, member_id(people.a, people.only_a), people.both, days_ago=20
    )

    with pytest.raises(DBAPIError) as error, tenant_context(people.a) as session:
        session.execute(text(HOLD_STATEMENTS[statement]), {"c": live.client_id})

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
       jsonb_build_object('reason', CAST(:reason AS text))
FROM ended
""")


def erase_announcing(
    migrate_engine: Engine,
    people: People,
    client_id: object,
    *,
    announce: bool = True,
    reason: str = "client_erased",
) -> None:
    """Erase a client of business A as its owner does: the booking lock, the live bookings ended
    with the erasure's reason, then the core (announce false: a re-applied erasure)."""
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        conn.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})
        conn.execute(END_ANNOUNCED, {"id": client_id, "actor": people.both, "reason": reason})
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


PRICE = 4000


@pytest.fixture
def shop(people: People, owner: TestClient, fake_stripe: FakeGateway) -> str:  # noqa: F811
    """A service sold with payment up front, on a published page."""
    save_setting(people.a, "published", True)
    return make_shop(people, owner, fake_stripe, price=PRICE)


# ZIF-58 F31 FENCE. Wrong impl: the client email enqueued on every refund event (a replayed
# settle, a refund.failed webhook delivered twice), not once per failure told. A paid booking
# ended by its client's erasure, whose refund Stripe refuses at once or fails days later: the
# erased client gets exactly one booking_refund_failed, at the held address, naming the amount and
# the business's contact address and replying to it. No other email, and no log line, carries the
# client's address.
@pytest.mark.parametrize("how", ["refused", "failed later"])
def test_a_failed_refund_is_mailed_once_to_the_erased_client(
    people: People,
    app: FastAPI,  # noqa: F811
    owner: TestClient,  # noqa: F811
    migrate_engine: Engine,
    fake_stripe: FakeGateway,
    shop: str,
    clean_outbox: None,  # noqa: F811
    log_lines: Callable[[], list[dict[str, Any]]],
    how: str,
) -> None:
    set_contact(owner)
    _, booking_id, _ = paid_booking(app, people, fake_stripe, shop)
    client_id = client_of(people.a, booking_id)
    address = address_of(people.a, client_id)
    erase_announcing(migrate_engine, people, client_id)

    if how == "refused":
        fake_stripe.fail_next("refund", "invalid")
        for _ in range(2):  # the retry and the sweep's backstop find it settled
            payments.settle(settle_job(people, booking_id))
    else:
        payments.settle(settle_job(people, booking_id))
        (refund_id,) = fake_stripe.refunds
        fake_stripe.fail_refund(refund_id)
        for _ in range(2):  # Stripe delivers an event more than once
            assert (
                webhook(new_client(app), "refund.failed", account_of(people), refund_id).status_code
                == 200
            )
            run("stripe.event")
    with jobs_as_the_worker():
        run("email.booking")

    subject = render("booking_refund_failed", "en")[0]
    (sent,) = [message for message in sent_to(address) if message["Subject"] == subject]
    assert "€40.00" in sent["Text"]
    assert REPLY_TO in sent["Text"]
    assert sent["Headers"]["Reply-To"] == [REPLY_TO]
    assert address not in json.dumps([call for call in MAILGUN_CALLS if call["To"] != address])
    assert address not in json.dumps(log_lines())


# ZIF-58 GUARD. A failed refund is mailed through a hold only: a live client's is the business's to
# follow up (the console flags it), so the job sends nothing, whatever enqueued it.
def test_a_live_clients_failed_refund_is_never_mailed(
    people: People,
    owner: TestClient,  # noqa: F811
    booked: dict[str, str],  # noqa: F811
    clean_outbox: None,  # noqa: F811
) -> None:
    assert (
        owner.patch(
            f"/api/bookings/{booked['live']}", json={"status": "cancelled_by_merchant"}
        ).status_code
        == 200
    )
    with tenant_context(people.a) as session:
        told = session.scalar(
            payments.TELL_REFUND_FAILED,
            {
                "booking_id": booked["live"],
                "refund_id": None,
                "details": json.dumps({"amount_minor": 2500, "refund": None}),
            },
        )
    job = run_send_booking(people.a, booked["live"], "booking_refund_failed", event_id=str(told))

    assert outbox_for(people.a, job.id) is None


def purge() -> None:
    """The worker's sweep of the holds, as ziftbook_worker."""
    with jobs_as_the_worker():
        erasure_holds.sweep()


def booking_mail(seeded: Seeded, booking: uuid.UUID, template: str) -> None:
    """An email.booking job for that booking, unfinished."""
    with tenant_context(seeded.tenant_id) as session:
        jobs.enqueue(
            session,
            "email.booking",
            f"email.booking:{seeded.tenant_id}:{booking}:{template}:{uuid.uuid4()}",
            {"booking_id": str(booking), "template": template},
            tenant_id=seeded.tenant_id,
        )


def finish_mails(migrate_engine: Engine, seeded: Seeded, finish: str) -> None:
    """Every email.booking job of that business done, given up after its last attempt (its last
    lease run out), or on its last attempt still (as CLAIM leaves it: the lease runs 16 minutes)."""
    last = f"attempts = {jobs.MAX_ATTEMPTS}, next_attempt_at = now()"
    change = {
        "done": "completed_at = now()",
        "given up": f"{last} - interval '1 second'",
        "last attempt": f"{last} + interval '16 minutes'",
    }[finish]
    with migrate_engine.begin() as conn:
        conn.execute(
            text(f"UPDATE jobs SET {change} WHERE kind = 'email.booking' AND tenant_id = :t"),
            {"t": seeded.tenant_id},
        )


def change_payment(migrate_engine: Engine, seeded: Seeded, change: str) -> None:
    with migrate_tenant_context(migrate_engine, seeded.tenant_id) as conn:
        conn.execute(
            text(f"UPDATE booking_payments SET {change} WHERE booking_id = :b"),
            {"b": seeded.bookings[1]},
        )


@pytest.fixture
def no_jobs(people: People, migrate_engine: Engine) -> Any:
    """The test's own jobs, deleted after: no later test's worker run picks them up."""
    yield
    with migrate_engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs WHERE tenant_id = :t"), {"t": people.a})


# ZIF-58 F32 FENCE (1 of 5). Wrong impl: a purge on purge_after only, so an erasure with nothing
# owed keeps the address a month instead of minutes. A hold is deleted as soon as nothing is owed
# and none of the client's emails through it (mail.HOLD_TEMPLATES) is still to be sent; another
# booking email still queued (a reminder) does not keep it, and a mail that gave up counts as out.
# One on its last attempt has not given up: CLAIM counts the attempt when it starts, and that
# attempt still has to read the hold (wrong impl: `attempts < MAX_ATTEMPTS` alone).
@pytest.mark.parametrize("finish", ["done", "given up", "last attempt"])
def test_a_hold_goes_once_nothing_is_owed_and_its_mails_are_out(
    people: People, migrate_engine: Engine, no_jobs: None, finish: str
) -> None:
    seeded = held(people, migrate_engine)
    booking_mail(seeded, seeded.bookings[0], "booking_cancelled")

    purge()
    assert hold_address(migrate_engine, seeded) == seeded.email

    finish_mails(migrate_engine, seeded, finish)
    booking_mail(seeded, seeded.bookings[1], "booking_reminder")
    purge()
    assert hold_address(migrate_engine, seeded) == (
        seeded.email if finish == "last attempt" else None
    )


# ZIF-58 F32 FENCE (2 of 5). Wrong impl: a purge on purge_after only; or one that lets the hold go
# the moment a refund settles, when a refund can still fail at the bank days later and the client
# would then have no channel. A paid booking ended by the erasure keeps the hold while the refund
# is owed, then for 7 days after it settled, and no longer.
def test_a_hold_goes_7_days_after_a_refund_settled(people: People, migrate_engine: Engine) -> None:
    seeded = held(
        people,
        migrate_engine,
        payment=Pay(settled=False, booking="cancelled_by_merchant"),
        reason=None,
    )
    purge()
    assert hold_address(migrate_engine, seeded) == seeded.email  # the refund is owed

    change_payment(migrate_engine, seeded, "settled_at = now(), refunded_minor = amount_minor")
    purge()
    assert hold_address(migrate_engine, seeded) == seeded.email  # settled today

    change_payment(migrate_engine, seeded, "settled_at = now() - interval '7 days 1 minute'")
    purge()
    assert hold_address(migrate_engine, seeded) is None


def due(migrate_engine: Engine, seeded: Seeded, ago: str) -> None:
    """The hold written long enough ago that its purge date was that long ago (as if the clock had
    moved: created_at goes back with it, so the CHECK still holds)."""
    with migrate_tenant_context(migrate_engine, seeded.tenant_id) as conn:
        conn.execute(
            text(
                "UPDATE client_erasure_holds SET purge_after = now() - CAST(:ago AS interval),"
                " created_at = now() - CAST(:ago AS interval) - interval '30 days'"
                " WHERE client_id = :c"
            ),
            {"ago": ago, "c": seeded.client_id},
        )


def late_lines(lines: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [
        (line["level"], line["client_id"], line["tenant_id"])
        for line in lines
        if line["msg"] == "contact hold purged late"
    ]


# ZIF-58 F32 FENCE (3 of 5). Wrong impl: a purge that waits for the client's money to be settled
# whatever the date, so a refund that failed for good keeps the address for ever. At its purge
# date the hold goes regardless of what is owed, and on time it is no ERROR.
def test_a_hold_goes_at_its_purge_date_whatever_is_owed(
    people: People, migrate_engine: Engine, log_lines: Callable[[], list[dict[str, Any]]]
) -> None:
    seeded = held(
        people,
        migrate_engine,
        payment=Pay(booking="cancelled_by_merchant", refund_failed=True),
        reason=None,
    )
    purge()
    assert hold_address(migrate_engine, seeded) == seeded.email  # the failed refund is owed

    due(migrate_engine, seeded, "1 second")
    purge()
    assert hold_address(migrate_engine, seeded) is None
    assert late_lines(log_lines()) == []


async def first_iteration(kinds: dict[str, Any]) -> None:
    """The worker's first loop after it starts (drain, prune, sweeps), then stop."""
    stop = asyncio.Event()

    def sweep() -> int:
        stop.set()  # the booking sweep comes last: the loop ends after this iteration
        return 0

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(bookings, "sweep", sweep)
        await worker.work(kinds, None, stop)


# ZIF-58 F32 FENCE (4 of 5). Wrong impl: no purge on the worker's timer, or none at start, so a
# worker that was down past a hold's purge date leaves the address there until something else
# runs. Its first iteration after it starts deletes the hold, and logs one ERROR naming the client
# (ids only): a hold found past its purge date means the time box was at risk.
def test_a_worker_started_after_a_holds_purge_date_purges_it_and_says_so(
    people: People, migrate_engine: Engine, log_lines: Callable[[], list[dict[str, Any]]]
) -> None:
    seeded = held(
        people,
        migrate_engine,
        payment=Pay(booking="cancelled_by_merchant", refund_failed=True),
        reason=None,
    )
    due(migrate_engine, seeded, "2 days")

    with jobs_as_the_worker():
        asyncio.run(first_iteration({}))

    assert hold_address(migrate_engine, seeded) is None
    assert late_lines(log_lines()) == [("ERROR", str(seeded.client_id), str(people.a))]


BACKSTOP = text("""
SELECT j.due_at = h.purge_after FROM jobs j
JOIN client_erasure_holds h ON h.client_id = (j.payload ->> 'client_id')::uuid
WHERE j.kind = 'erasure.hold_purge' AND j.tenant_id = :t
""")
JOB_OUTCOME = text("""
SELECT completed_at IS NOT NULL, skipped FROM jobs
WHERE kind = 'erasure.hold_purge' AND tenant_id = :t
""")


# ZIF-58 F32 FENCE (5 of 5, the backstop). Wrong impl: an erasure.hold_purge kind with the usual
# grace, so a worker down past the purge date skips the job when it starts; or no such kind. The
# erase route's call (as the app role, through erase_client) enqueues it due exactly at the hold's
# purge date; a worker started two days later runs it, which purges the hold and logs the ERROR.
def test_the_backstop_job_purges_a_hold_even_when_the_worker_starts_late(
    people: People,
    migrate_engine: Engine,
    no_jobs: None,
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    seeded = seed_person(
        migrate_engine,
        people.a,
        member_id(people.a, people.only_a),
        people.both,
        days_ago=10,
        payment=Pay(booking="cancelled_by_merchant", refund_failed=True),
    )
    with tenant_context(people.a) as session:
        booking_lock(session)
        session.execute(
            ENDED_BY_ERASURE,
            {
                "booking": seeded.bookings[0],
                "actor": people.both,
                "details": json.dumps({"reason": "client_erased"}),
            },
        )
        erased = session.scalar(
            ERASE_CLIENT, {"h": session_hash(people.a, people.both), "id": seeded.client_id}
        )
        erasure_holds.enqueue_purge(session, people.a, seeded.client_id, erased)
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        assert conn.scalars(BACKSTOP, {"t": people.a}).all() == [True]
    due(migrate_engine, seeded, "2 days")
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET due_at = now() - interval '2 days', next_attempt_at = now()"
                " WHERE kind = 'erasure.hold_purge' AND tenant_id = :t"
            ),
            {"t": people.a},
        )

    with jobs_as_the_worker():
        asyncio.run(first_iteration({"erasure.hold_purge": worker.KINDS["erasure.hold_purge"]}))

    assert hold_address(migrate_engine, seeded) is None
    with migrate_engine.connect() as conn:
        assert tuple(conn.execute(JOB_OUTCOME, {"t": people.a}).one()) == (True, False)
    (line,) = [line for line in log_lines() if line["msg"] == "contact hold purged late"]
    assert (line["level"], line["client_id"], line["job_kind"]) == (
        "ERROR",
        str(seeded.client_id),
        "erasure.hold_purge",
    )


APP = Path(__file__).parents[1] / "app"
# The only code that may name the hold table: the worker's. Each entry says why.
HOLD_READERS = {
    "mail.py": "the booking mail job sends an erased client's mail through it",
    "erasure_holds.py": "the worker's purge deletes it",
}


# ZIF-58 F34 FENCE. Wrong impl: a debug or convenience field that shows a merchant the address an
# erasure kept (a console route reading the hold, a field in the API naming it). Three looks:
# only the worker's modules name the table, and no route is served from them; the API document
# has no field or path about a hold; and as the owner, every GET route of the console, given the
# erased client's bookings, answers without the held address anywhere in its body.
def test_no_console_route_can_read_a_hold_address(
    people: People,
    owner: TestClient,  # noqa: F811
    migrate_engine: Engine,
    ready: str,  # noqa: F811
    booked: dict[str, str],  # noqa: F811
) -> None:
    naming = {path.name for path in APP.glob("*.py") if "client_erasure_holds" in path.read_text()}
    assert naming == set(HOLD_READERS)
    workers_only = {f"app.{name.removesuffix('.py')}" for name in HOLD_READERS}
    served = {
        route.endpoint.__module__ for route in create_app().routes if isinstance(route, APIRoute)
    }
    assert not served & workers_only

    spec = json.loads(openapi_document())
    words = set()
    for name in [*spec["paths"], *_properties(spec)]:
        words.update(re.split(r"[^a-z]+", name.lower()))
    assert not words & {"hold", "holds", "held"}

    client_id = uuid.UUID(booked["client"])
    address = fresh_email()  # the client's alone: the booking helper made its first one a member's
    set_client(people.a, client_id, email=address)
    erase_announcing(migrate_engine, people, client_id)
    assert hold_address(migrate_engine, Seeded(people.a, client_id, (), address)) == address
    values = {
        "booking_id": booked["pending"],
        "service_id": ready,
        "member_id": str(member_id(people.a, people.both)),
        "tenant_id": str(people.a),
        "slug": "none",
    }
    window = {"from": at("00:00")[:10], "to": at("23:59")[:10]}
    seen = 0
    for path, operations in spec["paths"].items():
        if "get" not in operations:
            continue
        for booking in ("started", "pending"):
            values["booking_id"] = booked[booking]
            answered = owner.get(path.format(**values), params=window)
            assert address not in answered.text, path
            seen += answered.status_code == 200
    assert seen > 10
    assert owner.get(f"/api/bookings/{booked['pending']}").json()["client_erased"] is True


def _properties(spec: Any) -> list[str]:
    """Every property name anywhere in the document."""
    if isinstance(spec, list):
        return [name for item in spec for name in _properties(item)]
    if not isinstance(spec, dict):
        return []
    found = list(spec["properties"]) if isinstance(spec.get("properties"), dict) else []
    return found + [name for value in spec.values() for name in _properties(value)]


NEEDED = text("SELECT client_hold_needed(CAST(:t AS uuid), :c)")
NEEDED_CASES = {
    "refund settled 2 days ago": True,
    "refund settled 8 days ago": False,
    **{f"queued {template}": True for template in sorted(mail.HOLD_TEMPLATES)},
    "queued booking_reminder": False,  # not sent through a hold
    "booking_cancelled on its last attempt": True,
    "booking_cancelled given up": False,
    "nothing": False,
}


# ZIF-58 FENCE (0078). Wrong impl: the core writing a hold on money owed alone (0064), so a client
# erased days after a refund settled, or with a hold email still queued from just before, gets no
# hold and is never told; or the purge with its own copy of the rule. client_hold_needed is the one
# rule: money owed (F22), a refund settled in the last 7 days, or a mail.HOLD_TEMPLATES email still
# to be sent (given up only once its last lease ran out, jobs.MAX_ATTEMPTS). The core writes a
# hold exactly when it is true (here with nothing announced), and the purge keeps it.
@pytest.mark.parametrize("case", list(NEEDED_CASES))
def test_the_core_and_the_purge_share_when_a_hold_is_needed(
    people: People, migrate_engine: Engine, no_jobs: None, case: str
) -> None:
    needed = NEEDED_CASES[case]
    refunded = case.startswith("refund")
    seeded = seed_person(
        migrate_engine,
        people.a,
        member_id(people.a, people.only_a),
        people.both,
        days_ago=10,
        payment=Pay(booking="cancelled_by_merchant") if refunded else PAID_AND_KEPT,
    )
    if refunded:
        days = case.split()[2]
        change_payment(
            migrate_engine,
            seeded,
            f"refunded_minor = amount_minor, settled_at = now() - interval '{days} days'",
        )
    elif case != "nothing":
        template = case.split()[-1] if case.startswith("queued") else "booking_cancelled"
        booking_mail(seeded, seeded.bookings[0], template)
        if not case.startswith("queued"):
            finish_mails(migrate_engine, seeded, " ".join(case.split()[-2:]).removeprefix("its "))
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        assert conn.scalar(NEEDED, {"t": str(people.a), "c": seeded.client_id}) is needed

    holds = hold_after(migrate_engine, seeded, people.both, reason=None, announce=False)

    assert len(holds) == (1 if needed else 0)
    purge()
    assert hold_address(migrate_engine, seeded) == (seeded.email if needed else None)


# ZIF-58 FENCE (0078). The purge has no copy of the rule: it calls the function the core calls.
def test_the_purge_calls_the_cores_hold_rule() -> None:
    purge_sql = str(erasure_holds.PURGE)
    assert "client_hold_needed(h.tenant_id, h.client_id)" in purge_sql
    assert "7 days" not in purge_sql
    assert "jobs" not in purge_sql


# ZIF-58 FENCE (0078), the reviewers' case. Wrong impl: 0064's hold rule (money owed alone). A
# paid booking the team cancelled, refunded two days before its client's erasure: nothing is live
# or owed, yet the refund can still fail. The erasure keeps a hold, so when the refund then fails
# at the bank the client is told, once, at the held address.
def test_a_refund_failing_after_an_erasure_still_reaches_the_client(
    people: People,
    app: FastAPI,  # noqa: F811
    owner: TestClient,  # noqa: F811
    migrate_engine: Engine,
    fake_stripe: FakeGateway,
    shop: str,
    clean_outbox: None,  # noqa: F811
) -> None:
    _, booking_id, _ = paid_booking(app, people, fake_stripe, shop)
    client_id = client_of(people.a, booking_id)
    address = fresh_email()  # the client's alone: the booking helper made its first one a member's
    set_client(people.a, client_id, email=address)
    cancelled = owner.patch(f"/api/bookings/{booking_id}", json={"status": "cancelled_by_merchant"})
    assert cancelled.status_code == 200
    payments.settle(settle_job(people, booking_id))
    (refund_id,) = fake_stripe.refunds
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        conn.execute(
            text(
                "UPDATE booking_payments SET settled_at = now() - interval '2 days'"
                " WHERE booking_id = :b"
            ),
            {"b": booking_id},
        )
    with jobs_as_the_worker():
        run("email.booking")  # the team's cancellation mail, before the erasure

    erase_announcing(migrate_engine, people, client_id)
    assert hold_address(migrate_engine, Seeded(people.a, client_id, (), address)) == address
    fake_stripe.fail_refund(refund_id)
    assert (
        webhook(new_client(app), "refund.failed", account_of(people), refund_id).status_code == 200
    )
    run("stripe.event")
    with jobs_as_the_worker():
        run("email.booking")

    subject = render("booking_refund_failed", "en")[0]
    assert [message["Subject"] for message in sent_to(address)].count(subject) == 1


# ZIF-58 FENCE. Wrong impl: the send reads the hold by client id alone, so a hold past its purge
# date that the purge has not reached yet (a worker back from a long outage drains its mail before
# it sweeps; a restored backup) still sends. Past purge_after a hold is no hold.
def test_a_hold_past_its_purge_date_sends_nothing(
    people: People,
    migrate_engine: Engine,
    booked: dict[str, str],  # noqa: F811
    clean_outbox: None,  # noqa: F811
) -> None:
    client_id = uuid.UUID(booked["client"])
    address = address_of(people.a, client_id)
    erase_announcing(migrate_engine, people, client_id)
    due(migrate_engine, Seeded(people.a, client_id, (), address), "1 second")

    with jobs_as_the_worker():
        cancelled = run_send_booking(people.a, booked["pending"], "booking_cancelled")

    assert outbox_for(people.a, cancelled.id) is None
    assert sent_to(address) == []


# ZIF-58 FENCE. Wrong impl: every booking_cancelled job for an erased client goes to its hold, so
# while a hold lives the team could mail the erased person by cancelling one of the bookings the
# erasure kept (one under way or past), a use the hold was never kept for. Only a cancellation the
# erasure itself made (its event gives an erasure's reason) is mailed; the team's later one is not.
def test_a_teams_cancellation_after_the_erasure_mails_nobody(
    people: People,
    owner: TestClient,  # noqa: F811
    migrate_engine: Engine,
    booked: dict[str, str],  # noqa: F811
    clean_outbox: None,  # noqa: F811
) -> None:
    client_id = uuid.UUID(booked["client"])
    address = address_of(people.a, client_id)
    erase_announcing(migrate_engine, people, client_id)
    later = owner.patch(
        f"/api/bookings/{booked['started']}", json={"status": "cancelled_by_merchant"}
    )
    assert later.status_code == 200

    with jobs_as_the_worker():
        team = run_send_booking(people.a, booked["started"], "booking_cancelled")
        erasure = run_send_booking(people.a, booked["pending"], "booking_cancelled")

    assert outbox_for(people.a, team.id) is None
    assert outbox_for(people.a, erasure.id) is not None
    assert len(sent_to(address)) == 1


# ZIF-58 FENCE. Wrong impl: hold mails counted against the business's public client-mail quota at
# enqueue (anonymous bookings fill it, and a dropped refund-failed job is never retried) or the
# per-recipient quota at send (an erasure that cancels many bookings uses it up), so the one notice
# the hold exists for is lost and the purge then lets the hold go. Hold mails are bounded by the
# hold's lifetime and by real events instead. With every quota at zero, the erased client still
# gets the erasure's cancellation and the refund that Stripe refused; a live client's mail is still
# held back (the quotas are on).
def test_a_full_quota_still_delivers_the_hold_mails(
    people: People,
    app: FastAPI,  # noqa: F811
    owner: TestClient,  # noqa: F811
    migrate_engine: Engine,
    fake_stripe: FakeGateway,
    shop: str,
    clean_outbox: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, booking_id, _ = paid_booking(app, people, fake_stripe, shop)
    _, live_id, _ = paid_booking(app, people, fake_stripe, shop, starts_at=at("11:00"))
    client_id = client_of(people.a, booking_id)
    address = fresh_email()  # the client's alone: the booking helper made its first one a member's
    set_client(people.a, client_id, email=address)
    erase_announcing(migrate_engine, people, client_id)
    for quota in ("CLIENT_MAIL_PUBLIC_PER_TENANT_HOUR", "CLIENT_MAIL_PER_TENANT_HOUR"):
        monkeypatch.setattr(bookings, quota, 0)
    for quota in ("CLIENT_MAIL_PER_RECIPIENT_HOUR", "CLIENT_MAIL_PER_RECIPIENT_ANY_HOUR"):
        monkeypatch.setattr(mail, quota, 0)
    fake_stripe.fail_next("refund", "invalid")
    payments.settle(settle_job(people, booking_id))
    cancelled = owner.patch(f"/api/bookings/{live_id}", json={"status": "cancelled_by_merchant"})
    assert cancelled.status_code == 200

    with jobs_as_the_worker():
        run_send_booking(people.a, booking_id, "booking_cancelled")
        run("email.booking")
        live = run_send_booking(people.a, live_id, "booking_cancelled")

    subjects = [message["Subject"] for message in sent_to(address)]
    assert sorted(subjects) == sorted(
        render(template, "en")[0] for template in ("booking_cancelled", "booking_refund_failed")
    )
    assert outbox_for(people.a, live.id)["status"] == "pending"  # held back by the quota


STATUS_OF = text("SELECT status FROM bookings WHERE id = :id")


def failing_except(keep: uuid.UUID) -> Callable[..., Any]:
    """A stand-in that raises in every business but `keep`, where it calls through."""

    def wrap(real: Callable[..., Any]) -> Callable[..., Any]:
        def call(db: Any, *args: Any) -> Any:
            if db.scalar(text("SELECT current_setting('app.tenant_id')")) != str(keep):
                raise RuntimeError("business boom")
            return real(db, *args)

        return call

    return wrap


# ZIF-58 FENCE. Wrong impl: a sweep that stops at the first business that raises, so one failing
# business holds back every other's purge (or, in the booking sweep, its expiries and the re-arm of
# its refunds). Each sweep goes on to the next business, logs the failure with its business (ids
# only), and raises at the end so the worker withholds its ping.
@pytest.mark.parametrize("sweep", ["holds", "bookings"])
def test_a_failing_business_never_stops_a_sweep_for_the_others(
    people: People,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    log_lines: Callable[[], list[dict[str, Any]]],
    sweep: str,
) -> None:
    for tenant_id, user_id in ((people.a, people.only_a), (people.b, people.only_b)):
        seeded = seed_person(
            migrate_engine,
            tenant_id,
            member_id(tenant_id, user_id),
            user_id,
            days_ago=10,
            # A lapsed request for the booking sweep to expire; for the hold sweep, nothing owed.
            payment=PAID_AND_KEPT if sweep == "holds" else Pay(booking="pending", settled=False),
        )
        if sweep == "holds":
            assert hold_after(
                migrate_engine, seeded, user_id, reason="client_erased", announce=True
            )
    wrap = failing_except(people.b)
    if sweep == "holds":
        monkeypatch.setattr(erasure_holds, "purge", wrap(erasure_holds.purge))
    else:
        monkeypatch.setattr(payments, "enqueue_unsettled", wrap(payments.enqueue_unsettled))

    with jobs_as_the_worker(), pytest.raises(RuntimeError):
        erasure_holds.sweep() if sweep == "holds" else bookings.sweep()

    with migrate_tenant_context(migrate_engine, people.b) as conn:
        if sweep == "holds":
            assert conn.scalar(HOLD_OF, {"c": seeded.client_id}) is None
        else:
            assert conn.scalar(STATUS_OF, {"id": seeded.bookings[1]}) == "expired"
    failed = [line for line in log_lines() if line["msg"] == "business sweep failed"]
    assert str(people.a) in {line["tenant_id"] for line in failed}
    assert str(people.b) not in {line["tenant_id"] for line in failed}
    assert {line["sweep"] for line in failed} == {sweep}


# ZIF-58 FENCE (review N2). Wrong impl: the backstop's due date computed in Python (erased_at plus
# 30 days of 24 hours) while the core dates the hold in SQL (now() + interval '30 days', in the
# session's time zone): across a DST change they differ by an hour, so the job runs early and
# purges nothing, or late. The job is due exactly when the core's arithmetic says.
def test_the_backstop_is_due_as_the_core_dates_the_hold(people: People, no_jobs: None) -> None:
    erased = datetime(2026, 3, 10, 12, tzinfo=UTC)  # 30 days on crosses Europe's DST start
    with tenant_context(people.a) as session:
        session.execute(text("SET LOCAL TimeZone = 'Europe/Amsterdam'"))
        erasure_holds.enqueue_purge(session, people.a, uuid.uuid7(), erased)
        expected = session.scalar(
            text("SELECT CAST(:at AS timestamptz) + interval '30 days'"), {"at": erased}
        )
        due = session.scalar(
            text("SELECT due_at FROM jobs WHERE kind = 'erasure.hold_purge' AND tenant_id = :t"),
            {"t": people.a},
        )
    assert expected != erased + timedelta(days=30)
    assert due == expected


# ZIF-58 GUARD (review N4). A failed refund for an erased client whose hold is gone (the spec
# allows it: more than 7 days after the refund settled, or past the purge date) cannot be mailed;
# ops sees it as email skipped, reason no_hold, with ids only.
def test_a_refund_failure_with_no_hold_left_is_logged(
    people: People,
    migrate_engine: Engine,
    booked: dict[str, str],  # noqa: F811
    clean_outbox: None,  # noqa: F811
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    client_id = uuid.UUID(booked["client"])
    erase_announcing(migrate_engine, people, client_id, announce=False)  # nothing owed: no hold
    with tenant_context(people.a) as session:
        told = session.scalar(
            payments.TELL_REFUND_FAILED,
            {
                "booking_id": booked["pending"],
                "refund_id": None,
                "details": json.dumps({"amount_minor": 2500, "refund": None}),
            },
        )

    with jobs_as_the_worker():
        job = run_send_booking(
            people.a, booked["pending"], "booking_refund_failed", event_id=str(told)
        )

    assert outbox_for(people.a, job.id) is None
    skipped = [line for line in log_lines() if line["msg"] == "email skipped"]
    assert [(line["template"], line["reason"]) for line in skipped] == [
        ("booking_refund_failed", "no_hold")
    ]


# ZIF-58 FENCE (review N5). Wrong impl: the hold sweep takes every business's booking lock every
# five minutes, holds or none, doubling the booking sweep's lock traffic, waiting on slow holders.
# A business with no hold is skipped without the lock (a hold written after that look is new and
# not due): the sweep finishes while another session holds that business's booking lock.
def test_the_hold_sweep_skips_a_business_with_no_hold_without_its_lock(
    people: People, migrate_engine: Engine
) -> None:
    done = threading.Event()

    def sweep() -> None:
        with jobs_as_the_worker():
            erasure_holds.sweep()
        done.set()

    with migrate_tenant_context(migrate_engine, people.b) as conn:
        assert conn.scalar(text("SELECT count(*) FROM client_erasure_holds")) == 0
        conn.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})
        sweeper = threading.Thread(target=sweep)
        sweeper.start()
        finished = done.wait(timeout=10)
    sweeper.join(timeout=10)
    assert finished


# Bodies through pg_get_functiondef, never prosrc: a BEGIN ATOMIC body is not in prosrc.
HOLD_FUNCTIONS = text("""
SELECT p.proname, has_function_privilege('ziftbook_app', p.oid, 'EXECUTE')
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')
  AND pg_get_functiondef(p.oid) LIKE '%client_erasure_holds%'
ORDER BY 1
""")
ATOMIC_PEEK = text("""
CREATE FUNCTION zif58_atomic_peek(p uuid) RETURNS bigint LANGUAGE sql SECURITY DEFINER
BEGIN ATOMIC
  SELECT count(*) FROM client_erasure_holds WHERE client_id = p;
END
""")


# ZIF-58 FENCE (security review NIT-4), the database's side of F34. Wrong impl: a function that
# reads the hold table and that the app role can run (a definer reads past the table's grants). The
# only function naming the table is the erase core, which writes it and which the app role cannot
# run.
def test_no_function_the_api_can_run_names_the_hold_table(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        assert [tuple(row) for row in conn.execute(HOLD_FUNCTIONS)] == [
            ("erase_client_core", False)
        ]
        # The positive control, rolled back: a BEGIN ATOMIC reader the app role may run is caught.
        conn.execute(ATOMIC_PEEK)
        conn.execute(text("GRANT EXECUTE ON FUNCTION zif58_atomic_peek(uuid) TO ziftbook_app"))
        caught = [tuple(row) for row in conn.execute(HOLD_FUNCTIONS)]
        conn.rollback()
    assert caught == [("erase_client_core", False), ("zif58_atomic_peek", True)]


# ZIF-58 FENCE (re-check SHOULD-A). Wrong impl: a booking_cancelled reaches a hold only when the
# erasure made the cancellation, so a cancellation the team made while the client was still live,
# whose mail was still queued when the client was erased minutes later, is refused: the erasure
# keeps a hold for that mail (client_hold_needed's mail arm) and nothing is ever sent through it.
# A cancellation from before the erasure is the person's to hear of; one after it is not.
def test_a_cancellation_queued_before_the_erasure_is_still_mailed(
    people: People,
    owner: TestClient,  # noqa: F811
    migrate_engine: Engine,
    booked: dict[str, str],  # noqa: F811
    clean_outbox: None,  # noqa: F811
    no_jobs: None,
) -> None:
    client_id = uuid.UUID(booked["client"])
    address = fresh_email()  # the client's alone: the booking helper made its first one a member's
    set_client(people.a, client_id, email=address)
    before = owner.patch(
        f"/api/bookings/{booked['started']}", json={"status": "cancelled_by_merchant"}
    )
    assert before.status_code == 200  # its booking_cancelled is queued, not yet sent

    erase_announcing(migrate_engine, people, client_id, announce=False)  # nothing ended by it
    assert hold_address(migrate_engine, Seeded(people.a, client_id, (), address)) == address
    with jobs_as_the_worker():
        run("email.booking")

    assert [message["Subject"] for message in sent_to(address)] == [
        render("booking_cancelled", "en")[0]
    ]


# ZIF-58 FENCE (re-check SHOULD-C). Wrong impl: a re-applied erasure's cancellations (reason
# erasure_reapplied) mailed through a hold. After a restore the client was already told at the
# original erasure; with 0078 a cancellation mail queued in the re-apply would also write a hold,
# so the person would be told twice. A re-apply's cancellation is never mailed.
def test_a_reapplied_erasure_never_mails_its_cancellations_again(
    people: People,
    migrate_engine: Engine,
    booked: dict[str, str],  # noqa: F811
    clean_outbox: None,  # noqa: F811
    no_jobs: None,
) -> None:
    client_id = uuid.UUID(booked["client"])
    address = fresh_email()  # the client's alone: the booking helper made its first one a member's
    set_client(people.a, client_id, email=address)
    with tenant_context(people.a) as session:
        bookings.email(session, people.a, booked["pending"], "booking_cancelled", merchant=True)
    erase_announcing(migrate_engine, people, client_id, announce=False, reason="erasure_reapplied")
    assert hold_address(migrate_engine, Seeded(people.a, client_id, (), address)) == address

    with jobs_as_the_worker():
        run("email.booking")

    assert sent_to(address) == []
