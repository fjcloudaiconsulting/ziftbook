"""ZIF-158 B3: a client pays in advance, and a booking that ends refunds what its terms say.

Fake Stripe throughout. The sweep and the settle jobs are run by hand (settle_all): that is the
only place money moves back. Amounts are read from the fake's ledger, never from a Refund object.
"""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app import bookings, payments, stripe_gateway
from app.db import tenant_context
from app.jobs import Job
from app.main import create_app
from tests.conftest import People, new_client, points, put_settings, save_setting, signed_in
from tests.stripe_fake import FakeGateway
from tests.test_booking_email import (
    clean_outbox,  # noqa: F401 -- autouse: these tests send mail, so they leave outbox rows
    jobs_for_booking,
    mail_for,
    run_send_booking,
)
from tests.test_booking_links_api import cancel_body, get, linked, post, reschedule_body
from tests.test_booking_payments_api import (
    account_of,
    due_now,
    emails,
    history,
    make_shop,
    pay_rows,
    place,
    run,
    status_of,
    sync,
    webhook,
)
from tests.test_bookings_api import at
from tests.test_bookings_approval_api import PAST, expire, patch, shift
from tests.test_merchant_booking_api import move

PRICE = 4000


@pytest.fixture
def app(people: People, fake_stripe: FakeGateway) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture(autouse=True)
def _published(people: People) -> None:
    save_setting(people.a, "published", True)


@pytest.fixture
def ready(people: People, owner: TestClient, fake_stripe: FakeGateway) -> str:
    return make_shop(people, owner, fake_stripe, price=PRICE)


def booked(
    app: FastAPI,
    people: People,
    fake: FakeGateway,
    service_id: str,
    kind: str = "pay",
    *,
    fee: int | None = None,
    **overrides: Any,
) -> tuple[TestClient, str, str]:
    """A booking whose payment (or check) landed: (the client, the booking id, the intent id)."""
    client = new_client(app)
    made = place(app, people, service_id, payment=kind, client=client, **overrides)
    assert made.status_code == 201, made.text
    booking_id = made.json()["id"]
    intent = fake.latest(booking_id)
    fake.confirm(intent)
    fake.intents[intent]["fee"] = fee
    assert sync(client, people, booking_id).json()["status"] in ("pending", "confirmed")
    return client, booking_id, intent


def settle_all() -> None:
    """The worker's sweep, then the settle jobs it queued."""
    bookings.sweep()
    run("payment.settle")


def settle_job(people: People, booking_id: str) -> Job:
    return Job(uuid.uuid4(), "payment.settle", people.a, {"booking_id": booking_id})


def paid_row(people: People, booking_id: str) -> dict[str, Any]:
    with tenant_context(people.a) as session:
        row = (
            session.execute(
                text(
                    "SELECT status, amount_minor, refunded_minor, refund_id, stripe_fee_minor, "
                    "settled_at, refund_failed_at FROM booking_payments WHERE booking_id = :b"
                ),
                {"b": booking_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def count_events(people: People, booking_id: str, event: str) -> int:
    return history(people, booking_id).count(event)


CASES = {
    "declined": True,
    "pending expiry": True,
    "merchant cancel pending": True,
    "merchant cancel confirmed": True,
    "merchant cancel completed": True,
    "client cancel pending": True,
    "client cancel engine 100": True,
    "client cancel engine 0": False,
    "accept": False,
    "client move": False,
    "merchant move": False,
    "complete": False,
    "no show": False,
}


PENDING_CASES = {"declined", "pending expiry", "merchant cancel pending", "client cancel pending"}


# 1: FENCE. Kills: one rule for every path (refunding moves, or refusing a pending client 100%);
# reading live settings instead of the snapshot engine; refunding a no-show (O7).
@pytest.mark.parametrize("kind", ["pay", "check"])
@pytest.mark.parametrize("case", list(CASES))
def test_each_ending_moves_the_right_money(
    case: str,
    kind: str,
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
) -> None:
    hours = 720 if case == "client cancel engine 0" else 0
    assert put_settings(owner, {"free_cancellation_hours": hours}).status_code == 200
    _, booking_id, _ = booked(app, people, fake_stripe, ready, kind)
    if case not in PENDING_CASES:
        assert patch(owner, booking_id, "confirmed").status_code == 200

    if case == "declined":
        assert patch(owner, booking_id, "declined").status_code == 200
    elif case == "pending expiry":
        expire(people.a, booking_id)
    elif case.startswith("merchant cancel"):
        if case.endswith("completed"):
            shift(people.a, booking_id, PAST)
            assert patch(owner, booking_id, "completed").status_code == 200
        assert patch(owner, booking_id, "cancelled_by_merchant").status_code == 200
    elif case.startswith("client cancel"):
        client = linked(app, people.a, booking_id)
        pct = get(client).json()["engine"]["refund_pct"]
        assert post(client, "/cancel", cancel_body(booking_id, pct)).status_code == 200
        # What the client was shown is what is refunded: neither the business's terms changing
        # afterwards nor the appointment drawing closer moves it.
        assert put_settings(owner, {"free_cancellation_hours": 720 - hours}).status_code == 200
        shift(people.a, booking_id, PAST)
    elif case == "client move":
        moved = post(
            linked(app, people.a, booking_id),
            "/reschedule",
            reschedule_body(booking_id, at("15:00"), 0),
        )
        assert moved.status_code == 200, moved.text
    elif case == "merchant move":
        assert move(owner, booking_id, starts_at=at("11:00")).status_code == 200
    elif case in ("complete", "no show"):
        shift(people.a, booking_id, PAST)
        done = "completed" if case == "complete" else "no_show"
        assert patch(owner, booking_id, done).status_code == 200
    settle_all()

    refunds = [PRICE] if CASES[case] and kind == "pay" else []
    assert fake_stripe.refunded() == refunds
    assert fake_stripe.count("refund") == len(refunds)


# 2: FENCE. Kills: a per-payment or per-booking settle key without the status (the jobs dedupe
# makes the second enqueue a no-op: no refund, silently).
def test_a_placement_job_does_not_swallow_the_refund(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
    migrate_engine: Engine,
) -> None:
    _, booking_id, intent = booked(app, people, fake_stripe, ready)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    due_now(migrate_engine, booking_id)

    run("payment.settle")

    assert fake_stripe.refunded() == []
    assert patch(owner, booking_id, "cancelled_by_merchant").status_code == 200
    settle_all()
    assert fake_stripe.refunded(intent) == [PRICE]


# 3: FENCE. Kills: a key per job or per attempt; trusting only the idempotency key (24 h window);
# recording the event before the qualified UPDATE.
@pytest.mark.parametrize(
    "how", ["sequential", "in flight", "made, not yet recorded", "record fails once"]
)
def test_settling_twice_refunds_once(
    how: str,
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, booking_id, intent = booked(app, people, fake_stripe, ready)
    assert patch(owner, booking_id, "declined").status_code == 200
    job = settle_job(people, booking_id)

    if how == "sequential":
        payments.settle(job)
        payments.settle(job)
    elif how == "in flight":
        # A second job runs while the first is at Stripe: the key is in use, so it fails (and is
        # retried later) instead of refunding twice.
        refused: list[BaseException] = []

        def meanwhile(op: str) -> None:
            if op == "refund":
                fake_stripe.on_call = None
                with pytest.raises(stripe_gateway.Failed) as raised:
                    payments.settle(job)
                refused.append(raised.value)

        fake_stripe.on_call = meanwhile
        payments.settle(job)
        assert len(refused) == 1
        payments.settle(job)  # its retry finds the refund made and the row recorded
    elif how == "made, not yet recorded":
        # Stripe has refunded and the first job has not recorded it; a second job reads the
        # charge, sees nothing left to send and records first. The first must then record nothing.
        def second() -> None:
            fake_stripe.after_refund = None
            payments.settle(job)

        fake_stripe.after_refund = second
        payments.settle(job)
    else:
        with monkeypatch.context() as patched:
            patched.setattr(payments, "REFUNDED", text("SELECT 1 / 0"))
            with pytest.raises(DBAPIError):
                payments.settle(job)
        assert fake_stripe.refunded(intent) == [PRICE]
        fake_stripe.refund_keys.clear()  # Stripe forgot the key: only amount_refunded is left
        payments.settle(job)

    assert fake_stripe.refunded(intent) == [PRICE]
    row = paid_row(people, booking_id)
    assert (row["refunded_minor"], row["settled_at"] is not None) == (PRICE, True)
    assert count_events(people, booking_id, "refunded") == 1


# 4: FENCE. Kills: keeping the money for a booking that does not exist; resurrecting the slot; a
# silent refund the client cannot explain.
def test_a_payment_after_the_time_ran_out_is_refunded_in_full(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
    app_engine: Engine,
    migrate_engine: Engine,
) -> None:
    client = new_client(app)
    late = place(app, people, ready, payment="pay", client=client).json()["id"]
    fake_stripe.confirm(fake_stripe.latest(late))  # paid, but the hold has ended
    expire(people.a, late)
    due_now(migrate_engine, late)

    run("payment.settle")

    assert fake_stripe.refunded() == [PRICE]
    assert status_of(people, late) == "expired"
    assert count_events(people, late, "refunded") == 1
    assert emails(app_engine, people, late) == {"booking_expired": 1}
    (job,) = [
        j for j in jobs_for_booking(app_engine, people.a, late) if j["kind"] == "email.booking"
    ]
    assert job["payload"]["late"] == "1"
    run_send_booking(people.a, late, "booking_expired", late="1")
    text_late = mail_for(_address(people, late))["Text"]
    assert "didn't confirm" not in text_late and "40.00" in text_late

    # A pending request nobody answered: the other file.
    _, unanswered, _ = booked(app, people, fake_stripe, ready, starts_at=at("10:00"))
    expire(people.a, unanswered)
    settle_all()

    assert fake_stripe.refunded() == [PRICE, PRICE]
    (job,) = [
        j
        for j in jobs_for_booking(app_engine, people.a, unanswered)
        if j["kind"] == "email.booking" and j["payload"]["template"] == "booking_expired"
    ]
    assert "late" not in job["payload"]
    run_send_booking(people.a, unanswered, "booking_expired")
    assert "didn't confirm" in mail_for(_address(people, unanswered))["Text"]


def _address(people: People, booking_id: str) -> str:
    with tenant_context(people.a) as session:
        found: str = session.scalar(
            text(
                "SELECT c.email FROM bookings b JOIN clients c ON c.id = b.client_id "
                "WHERE b.id = :b"
            ),
            {"b": booking_id},
        )
    return found


# 4a: FENCE. Kills: marking the row canceled after a refused cancel (charged, never refunded, no
# alert); a settle that needs another job after its own reconcile.
@pytest.mark.parametrize("variant", ["webhook", "sweep"])
def test_a_payment_processing_at_the_deadline_is_refunded_once_it_lands(
    variant: str,
    people: People,
    app: FastAPI,
    ready: str,
    fake_stripe: FakeGateway,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = new_client(app)
    booking_id = place(app, people, ready, payment="pay", client=client).json()["id"]
    intent = fake_stripe.latest(booking_id)
    fake_stripe.process(intent)
    expire(people.a, booking_id)
    bookings.sweep()
    due_now(migrate_engine, booking_id)

    run("payment.settle")

    assert (pay_rows(people, booking_id)[0]["status"], status_of(people, booking_id)) == (
        "open",
        "expired",
    )
    assert fake_stripe.refunded() == []
    if variant == "webhook":
        with migrate_engine.begin() as conn:  # the earlier jobs are spent: only the event is left
            conn.execute(
                text(
                    "UPDATE jobs SET attempts = 5 WHERE kind = 'payment.settle' "
                    "AND payload->>'booking_id' = :b"
                ),
                {"b": booking_id},
            )
        fake_stripe.confirm(intent)  # the bank came through
        hook = webhook(client, "payment_intent.succeeded", account_of(people), intent)
        assert hook.status_code == 200
        run("stripe.event")
        run("payment.settle")
    else:
        for _ in range(4):  # the placement and the sweep's jobs give up (5 attempts)
            due_now(migrate_engine, booking_id)
            run("payment.settle")
        fake_stripe.confirm(intent)
        real = payments.hour
        monkeypatch.setattr(payments, "hour", lambda: str(int(real()) + 1))
        bookings.sweep()
        due_now(migrate_engine, booking_id, placement=False)
        run("payment.settle")

    assert fake_stripe.refunded(intent) == [PRICE]
    assert paid_row(people, booking_id)["status"] == "paid"
    assert paid_row(people, booking_id)["settled_at"] is not None


def cancelled_by_client(
    app: FastAPI, people: People, booking_id: str, owner: TestClient, accept: bool
) -> None:
    if accept:
        assert patch(owner, booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)
    pct = get(client).json()["engine"]["refund_pct"]
    assert post(client, "/cancel", cancel_body(booking_id, pct)).status_code == 200


# 9a: FENCE. Kills: a deduction while the flag is off (R6: unlawful until cleared); an estimated
# fee (a percentage constant); counting the application fee as Stripe's; a negative refund.
@pytest.mark.parametrize(
    ("keep", "scenario", "fee", "expected"),
    [
        (False, "pending", 162, [PRICE]),
        (False, "confirmed", 162, [PRICE]),
        (True, "pending", 162, [PRICE - 162]),
        (True, "confirmed", 162, [PRICE - 162]),
        (True, "pending", PRICE, []),
        (True, "engine 0", 162, []),
        (False, "engine 0", 162, []),
    ],
)
def test_a_client_cancel_refund_follows_the_fee_flag(
    keep: bool,
    scenario: str,
    fee: int,
    expected: list[int],
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(payments, "KEEP_CARD_FEE_ON_CLIENT_CANCEL", keep)
    hours = 720 if scenario == "engine 0" else 0
    assert put_settings(owner, {"free_cancellation_hours": hours}).status_code == 200
    _, booking_id, _ = booked(app, people, fake_stripe, ready, fee=fee)

    cancelled_by_client(app, people, booking_id, owner, scenario != "pending")
    settle_all()

    assert fake_stripe.refunded() == expected
    row = paid_row(people, booking_id)
    assert row["stripe_fee_minor"] == fee
    assert row["refunded_minor"] == (expected[0] if expected else 0)
    assert row["settled_at"] is not None


# 9b: FENCE. Kills: the client-cancel deduction applied to every refund (likely unlawful, D7).
@pytest.mark.parametrize(
    "ending",
    ["declined", "cancelled pending", "cancelled confirmed", "pending expiry", "paid too late"],
)
def test_business_endings_never_deduct(
    ending: str,
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(payments, "KEEP_CARD_FEE_ON_CLIENT_CANCEL", True)
    if ending == "paid too late":
        booking_id = place(app, people, ready, payment="pay").json()["id"]
        intent = fake_stripe.latest(booking_id)
        fake_stripe.confirm(intent)
        fake_stripe.intents[intent]["fee"] = 162
        expire(people.a, booking_id)
        due_now(migrate_engine, booking_id)
    else:
        _, booking_id, _ = booked(app, people, fake_stripe, ready, fee=162)
        if ending == "declined":
            assert patch(owner, booking_id, "declined").status_code == 200
        elif ending == "pending expiry":
            expire(people.a, booking_id)
        else:
            if ending == "cancelled confirmed":
                assert patch(owner, booking_id, "confirmed").status_code == 200
            assert patch(owner, booking_id, "cancelled_by_merchant").status_code == 200

    settle_all()

    assert fake_stripe.refunded() == [PRICE]


# 9c: FENCE. Kills: refunding the full amount or a guessed fee when Stripe has not reported it; a
# flag-off refund blocked on a fee nobody deducts.
def test_an_unknown_fee_is_never_guessed(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert put_settings(owner, {"free_cancellation_hours": 0}).status_code == 200
    _, booking_id, intent = booked(app, people, fake_stripe, ready)  # no balance transaction yet
    cancelled_by_client(app, people, booking_id, owner, False)
    job = settle_job(people, booking_id)

    with monkeypatch.context() as flag:
        flag.setattr(payments, "KEEP_CARD_FEE_ON_CLIENT_CANCEL", True)
        with pytest.raises(RuntimeError):
            payments.settle(job)
        assert (fake_stripe.refunded(), paid_row(people, booking_id)["settled_at"]) == ([], None)
        fake_stripe.intents[intent]["fee"] = 162  # Stripe reports it
        payments.settle(job)
    assert fake_stripe.refunded() == [PRICE - 162]

    _, other, _ = booked(app, people, fake_stripe, ready, starts_at=at("10:00"))
    cancelled_by_client(app, people, other, owner, False)
    payments.settle(settle_job(people, other))  # the flag is off again
    assert fake_stripe.refunded() == [PRICE - 162, PRICE]


# Review round 3: FENCE. Kills: a settle that records a refund Stripe has already failed and never
# looks again (the failure event ran while the record step was failing, so it saw nothing owed).
def test_a_refund_that_failed_before_it_was_recorded_is_flagged_when_it_is(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, booking_id, _ = booked(app, people, fake_stripe, ready)
    assert patch(owner, booking_id, "declined").status_code == 200
    job = settle_job(people, booking_id)
    with monkeypatch.context() as patched:
        patched.setattr(payments, "REFUNDED", text("SELECT 1 / 0"))
        with pytest.raises(DBAPIError):
            payments.settle(job)
    (refund_id,) = fake_stripe.refunds
    fake_stripe.fail_refund(refund_id)
    assert webhook(client, "refund.failed", account_of(people), refund_id).status_code == 200
    run("stripe.event")
    assert paid_row(people, booking_id)["refund_failed_at"] is None  # nothing recorded as owed yet

    payments.settle(job)  # the retry replays the same refund and records it

    assert fake_stripe.refunded() == [PRICE]
    assert paid_row(people, booking_id)["refund_failed_at"] is not None
    assert count_events(people, booking_id, "refund_failed") == 1


def refund_failed_count(reader: InMemoryMetricReader, kind: str = "pay") -> int:
    return sum(
        int(p.value)
        for p in points(reader, "ziftbook.payment.outcomes")
        if p.attributes["payment.outcome"] == "refund_failed"
        and p.attributes["payment.kind"] == kind
    )


def refund_failed_details(people: People, booking_id: str) -> list[dict[str, Any]]:
    with tenant_context(people.a) as session:
        return [
            r[0]
            for r in session.execute(
                text(
                    "SELECT details FROM booking_events "
                    "WHERE booking_id = :b AND event = 'refund_failed'"
                ),
                {"b": booking_id},
            )
        ]


# Refused refund: FENCE. Kills: a settle that raises on a refund Stripe refuses (5 retries, then an
# hourly "gave up" for ever, the business never told); one that settles but leaves the business
# unflagged; one that says "refunded" or mails "refunded in full" for money that did not move.
def test_a_refund_stripe_refuses_is_flagged_once_and_not_retried(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
    app_engine: Engine,
    migrate_engine: Engine,
    metric_reader: InMemoryMetricReader,
) -> None:
    client = new_client(app)
    late = place(app, people, ready, payment="pay", client=client).json()["id"]
    fake_stripe.confirm(fake_stripe.latest(late))  # paid, but the hold has ended
    expire(people.a, late)
    due_now(migrate_engine, late)
    fake_stripe.fail_next("refund", "invalid")

    run("payment.settle")  # does not raise: the job completes

    row = paid_row(people, late)
    assert (row["refunded_minor"], row["settled_at"] is not None) == (PRICE, True)
    assert row["refund_failed_at"] is not None
    assert fake_stripe.refunded() == []
    assert history(people, late).count("refund_failed") == 1
    assert refund_failed_details(people, late) == [{"amount_minor": PRICE, "refund": None}]
    assert count_events(people, late, "refunded") == 0
    assert emails(app_engine, people, late) == {}
    assert refund_failed_count(metric_reader) == 1

    payments.settle(settle_job(people, late))  # a second run, and the sweep's backstop
    settle_all()
    assert fake_stripe.count("refund") == 1
    assert history(people, late).count("refund_failed") == 1
    assert refund_failed_count(metric_reader) == 1


# Refused refund: FENCE. Kills: catching every Failed (a temporary failure must retry).
def test_a_temporary_refund_failure_still_retries(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
) -> None:
    _, booking_id, _ = booked(app, people, fake_stripe, ready)
    assert patch(owner, booking_id, "declined").status_code == 200
    fake_stripe.fail_next("refund", "unavailable")

    with pytest.raises(stripe_gateway.Failed):
        payments.settle(settle_job(people, booking_id))

    row = paid_row(people, booking_id)
    assert (row["settled_at"], row["refund_failed_at"]) == (None, None)
    payments.settle(settle_job(people, booking_id))
    assert fake_stripe.refunded() == [PRICE]


# Refused refund: FENCE. Kills: not counting the outcome, and counting a replayed refund.failed.
def test_an_async_refund_failure_is_counted_once(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
    metric_reader: InMemoryMetricReader,
) -> None:
    client, booking_id, _ = booked(app, people, fake_stripe, ready)
    assert patch(owner, booking_id, "declined").status_code == 200
    payments.settle(settle_job(people, booking_id))
    (refund_id,) = fake_stripe.refunds
    fake_stripe.fail_refund(refund_id)

    for _ in range(2):
        assert webhook(client, "refund.failed", account_of(people), refund_id).status_code == 200
        run("stripe.event")

    assert history(people, booking_id).count("refund_failed") == 1
    assert refund_failed_count(metric_reader) == 1


# Refused refund: GUARD. The flag follows the amounts: a smaller refund made by hand stays counted.
def test_a_refused_refund_after_a_partial_one_is_flagged_for_the_rest(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
) -> None:
    _, booking_id, intent = booked(app, people, fake_stripe, ready)
    fake_stripe.add_refund(intent, 1000, "succeeded")  # the business, in its dashboard
    assert patch(owner, booking_id, "declined").status_code == 200
    fake_stripe.fail_next("refund", "invalid")

    payments.settle(settle_job(people, booking_id))

    assert paid_row(people, booking_id)["refund_failed_at"] is not None
    assert refund_failed_details(people, booking_id) == [
        {"amount_minor": PRICE - 1000, "refund": None}
    ]
