"""ZIF-158 B2: checkout checks the client's card, end to end against the fake Stripe.

Dates and the booking POST are test_bookings_api's. A job is run by kind (run()), never the whole
queue: the xdist database keeps other tests' jobs, and `jobs` is not tenant-scoped.
"""

import asyncio
import json
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import Engine, text

from app import bookings, payments, stripe_gateway
from app.db import tenant_context
from app.jobs import run_once
from app.main import create_app
from app.worker import KINDS
from tests.conftest import (
    People,
    closed,
    fresh_email,
    member_id,
    new_client,
    points,
    put_settings,
    save_setting,
    signed_in,
)
from tests.stripe_fake import FakeGateway, connect, event_body, signed
from tests.test_availability_api import assign, new_service, seed_booking, weekdays
from tests.test_booking_email import jobs_for_booking
from tests.test_bookings_api import DAY, at, post_booking
from tests.test_bookings_approval_api import expire
from tests.test_merchant_booking_api import book as merchant_book
from tests.test_payments_db import refused as refused_as_app
from tests.test_working_hours import seed

JSON = {"content-type": "application/json"}
Lines = Callable[[], list[dict[str, Any]]]


@pytest.fixture(params=["authorisation", "setup"])
def both(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Runs the test once per way of checking a card."""
    monkeypatch.setattr(payments, "CHECK_WITH", request.param)
    return str(request.param)


@pytest.fixture
def app(people: People, fake_stripe: FakeGateway) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture(autouse=True)
def _published(people: People) -> None:
    save_setting(people.a, "published", True)


def make_shop(
    people: People,
    owner: TestClient,
    fake: FakeGateway,
    *,
    price: int = 2500,
    enabled: bool = True,
    check: bool = True,
) -> str:
    """A 30-minute service the owner performs 09:00-17:00 every day, sold with a card check."""
    service_id = new_service(owner, price={"amount_minor": price})
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    connect(fake, people.a, enabled=enabled)
    if check:
        assert put_settings(owner, {"payment_check": True}).status_code == 200
    return service_id


@pytest.fixture
def ready(people: People, owner: TestClient, fake_stripe: FakeGateway) -> str:
    return make_shop(people, owner, fake_stripe)


def place(
    app: FastAPI,
    people: People,
    service_id: str,
    payment: str | None = "check",
    client: TestClient | None = None,
    **overrides: Any,
) -> Response:
    extra = {} if payment is None else {"payment": payment}
    return post_booking(client or new_client(app), people.a, service_id, **extra, **overrides)


def path(people: People, booking_id: str, tail: str = "") -> str:
    return f"/api/public/businesses/{people.a}/bookings/{booking_id}/payment{tail}"


def sync(client: TestClient, people: People, booking_id: str) -> Response:
    return client.post(path(people, booking_id, "/sync"), headers=JSON)


def resume(client: TestClient, people: People, booking_id: str, kind: str = "check") -> Response:
    return client.put(path(people, booking_id), json={"kind": kind})


def abandon(client: TestClient, people: People, booking_id: str) -> Response:
    return client.post(path(people, booking_id, "/abandon"), headers=JSON)


def status_of(people: People, booking_id: str) -> str:
    with tenant_context(people.a) as session:
        found: str = session.scalar(
            text("SELECT status FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    return found


def pay_rows(people: People, booking_id: str) -> list[dict[str, Any]]:
    with tenant_context(people.a) as session:
        rows = session.execute(
            text(
                "SELECT kind, status, intent_id, settled_at, fee_mode, application_fee_minor, "
                "amount_minor, currency FROM booking_payments WHERE booking_id = :id "
                "ORDER BY created_at, id"
            ),
            {"id": booking_id},
        ).mappings()
        return [dict(row) for row in rows]


def history(people: People, booking_id: str) -> list[str]:
    with tenant_context(people.a) as session:
        return list(
            session.scalars(
                text("SELECT event FROM booking_events WHERE booking_id = :id ORDER BY id"),
                {"id": booking_id},
            )
        )


def job_rows(app_engine: Engine, people: People, booking_id: str) -> list[dict[str, Any]]:
    return jobs_for_booking(app_engine, people.a, booking_id)


def emails(app_engine: Engine, people: People, booking_id: str) -> Counter[str]:
    """The templates of the email jobs queued for the booking, with how many of each."""
    return Counter(
        _payload(job)["template"]
        for job in job_rows(app_engine, people, booking_id)
        if job["kind"] == "email.booking"
    )


def settle_keys(app_engine: Engine, people: People, booking_id: str) -> list[str]:
    return sorted(
        job["dedupe_key"]
        for job in job_rows(app_engine, people, booking_id)
        if job["kind"] == "payment.settle"
    )


def _payload(job: dict[str, Any]) -> dict[str, Any]:
    payload = job["payload"]
    out: dict[str, Any] = json.loads(payload) if isinstance(payload, str) else payload
    return out


def run(*kinds: str) -> None:
    """Run the due jobs of these kinds until none is due."""

    async def drain() -> None:
        while await run_once({kind: KINDS[kind] for kind in kinds}):
            pass

    asyncio.run(drain())


def due_now(migrate_engine: Engine, booking_id: str, placement: bool = True) -> None:
    """Make the booking's waiting settle jobs claimable (the clock cannot be moved, the row can);
    placement=False leaves the deadline job alone."""
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET next_attempt_at = now() WHERE kind = 'payment.settle' "
                "AND completed_at IS NULL AND attempts < 5 AND payload->>'booking_id' = :b "
                "AND (:placement OR right(dedupe_key, 16) <> 'awaiting_payment')"
            ),
            {"b": booking_id, "placement": placement},
        )


def webhook(client: TestClient, type_: str, account: str, intent: str) -> Response:
    body = event_body(type_, account=account, object_id=intent)
    return client.post(payments.WEBHOOK_PATH, content=body, headers=signed(body))


def account_of(people: People) -> str:
    with tenant_context(people.a) as session:
        found: str = session.scalar(text("SELECT stripe_account_id FROM payment_accounts"))
    return found


def checked_booking(
    app: FastAPI, people: People, fake: FakeGateway, ready: str, **overrides: Any
) -> tuple[TestClient, str, str]:
    """A booking whose check landed: (the client, the booking id, the intent id)."""
    client = new_client(app)
    made = place(app, people, ready, client=client, **overrides)
    assert made.status_code == 201, made.text
    booking_id = made.json()["id"]
    intent = fake.latest(booking_id)
    fake.confirm(intent)
    assert sync(client, people, booking_id).json()["status"] == "pending"
    return client, booking_id, intent


# 1: FENCE. Kills: capturing; forgetting the release (a hold left on the card); releasing a
# SetupIntent (a Stripe 400 loop); emailing at placement.
def test_a_check_books_awaiting_then_pending(
    people: People,
    app: FastAPI,
    ready: str,
    fake_stripe: FakeGateway,
    both: str,
    app_engine: Engine,
    metric_reader: InMemoryMetricReader,
) -> None:
    client = new_client(app)

    made = place(app, people, ready, client=client)

    assert made.status_code == 201
    body = made.json()
    booking_id = body["id"]
    assert body["status"] == "awaiting_payment"
    assert body["payment"]["type"] == ("payment" if both == "authorisation" else "setup")
    intent = fake_stripe.latest(booking_id)
    assert body["payment"]["client_secret"] == fake_stripe.intents[intent]["client_secret"]
    assert emails(app_engine, people, booking_id) == Counter()
    assert settle_keys(app_engine, people, booking_id) == [
        f"payment.settle:{people.a}:{booking_id}:awaiting_payment"
    ]

    fake_stripe.confirm(intent)
    done = sync(client, people, booking_id)

    assert done.status_code == 200
    assert done.json()["status"] == "pending"
    assert done.json()["payment"] == {"kind": "check", "state": "checked", "failed": False}
    assert "client_secret" not in done.text
    assert history(people, booking_id) == ["created", "payment_checked"]
    assert emails(app_engine, people, booking_id) == Counter(
        {"booking_received": 1, "booking_request": 1}
    )
    (row,) = pay_rows(people, booking_id)
    assert row["status"] == "checked"
    if both == "authorisation":
        assert row["settled_at"] is None
        assert f"payment.settle:{people.a}:{booking_id}:checked" in settle_keys(
            app_engine, people, booking_id
        )
        run("payment.settle")
        assert fake_stripe.intents[intent]["status"] == "canceled"
        assert pay_rows(people, booking_id)[0]["settled_at"] is not None
    else:
        assert fake_stripe.count("cancel_intent") == 0
        assert row["settled_at"] is not None
        assert settle_keys(app_engine, people, booking_id) == [
            f"payment.settle:{people.a}:{booking_id}:awaiting_payment"
        ]
    outcomes = {
        (p.attributes["payment.kind"], p.attributes["payment.outcome"])
        for p in points(metric_reader, "ziftbook.payment.outcomes")
    }
    assert outcomes == {("check", "started"), ("check", "checked")} | (
        {("check", "released")} if both == "authorisation" else set()
    )
    closed(metric_reader)


# 2: FENCE. Kills: trusting the browser's "succeeded".
def test_the_client_cannot_confirm_a_payment(
    people: People,
    app: FastAPI,
    ready: str,
    fake_stripe: FakeGateway,
    both: str,
    app_engine: Engine,
) -> None:
    client = new_client(app)
    booking_id = place(app, people, ready, client=client).json()["id"]
    assert (
        fake_stripe.intents[fake_stripe.latest(booking_id)]["status"] == "requires_payment_method"
    )

    for _ in range(2):
        answer = sync(client, people, booking_id)
        assert answer.json()["status"] == "awaiting_payment"
        assert answer.json()["payment"]["state"] == "open"

    assert history(people, booking_id) == ["created"]
    assert emails(app_engine, people, booking_id) == Counter()


# 3: FENCE. Kills: reading the event payload; a reconcile that writes `canceled` from any status
# (an accepted booking would read "check canceled").
def test_an_older_event_never_moves_a_payment_back(
    people: People,
    app: FastAPI,
    ready: str,
    fake_stripe: FakeGateway,
    app_engine: Engine,
    migrate_engine: Engine,
) -> None:
    client, booking_id, intent = checked_booking(app, people, fake_stripe, ready)
    run("payment.settle")
    assert fake_stripe.intents[intent]["status"] == "canceled"
    account = account_of(people)

    for type_ in ("payment_intent.canceled", "payment_intent.amount_capturable_updated"):
        assert webhook(client, type_, account, intent).status_code == 200
    run("stripe.event")

    assert pay_rows(people, booking_id)[0]["status"] == "checked"
    assert status_of(people, booking_id) == "pending"
    assert history(people, booking_id) == ["created", "payment_checked"]
    assert emails(app_engine, people, booking_id) == Counter(
        {"booking_received": 1, "booking_request": 1}
    )


# 4: FENCE. Kills: creating the intent inside tx1 (up to ~20 s of the tenant lock per booking); a
# resume, abandon or refund that calls Stripe after LOCK.
def test_no_stripe_call_while_the_business_is_locked(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
    migrate_engine: Engine,
) -> None:
    seen: list[tuple[str, int, int]] = []

    def probe(op: str) -> None:
        with migrate_engine.connect() as conn:
            locks = conn.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = 51 "
                    "AND granted AND database = (SELECT oid FROM pg_database "
                    "WHERE datname = current_database())"
                )
            )
        with tenant_context(people.a) as session:
            held = session.scalar(
                text("SELECT count(*) FROM bookings WHERE status = 'awaiting_payment'")
            )
        seen.append((op, locks, held))

    fake_stripe.on_call = probe
    client = new_client(app)
    booking_id = place(app, people, ready, client=client).json()["id"]
    intent = fake_stripe.latest(booking_id)
    assert resume(client, people, booking_id).status_code == 200
    assert sync(client, people, booking_id).status_code == 200
    fake_stripe.confirm(intent)
    assert sync(client, people, booking_id).status_code == 200
    run("payment.settle")
    other = place(app, people, ready, starts_at=at("10:00"), client=client).json()["id"]
    assert abandon(client, people, other).status_code == 204
    payments.refresh_account(people.a)
    assert owner.post("/api/payments/account", json={"locale": "en"}).status_code == 200

    assert {op for op, _, _ in seen} >= {
        "create_check",
        "retrieve_intent",
        "cancel_intent",
        "retrieve_account",
        "register_domain",
        "onboarding_link",
    }
    assert [(op, n) for op, n, _ in seen if n] == []
    # The booking was already committed, as awaiting_payment, when the intent was made.
    assert [held for op, _, held in seen if op == "create_check"] == [1, 1]


# 5: FENCE. Kills: leaving the booking awaiting (the client's own time blocked 30 minutes against
# them, the pending place spent).
def test_a_failed_create_gives_the_time_back(
    people: People, app: FastAPI, ready: str, fake_stripe: FakeGateway
) -> None:
    fake_stripe.fail_next("create_check", "unavailable")
    client = new_client(app)
    address = fresh_email()

    refused = place(app, people, ready, client=client, email=address)

    assert (refused.status_code, refused.json()["code"]) == (503, "payments_unavailable")
    with tenant_context(people.a) as session:
        booking_id = session.scalar(text("SELECT id FROM bookings"))
        assert session.scalar(text("SELECT count(*) FROM booking_payments")) == 0
    assert status_of(people, booking_id) == "expired"
    assert history(people, booking_id) == ["created", "expired"]
    again = place(app, people, ready, client=client, email=address)
    assert again.status_code == 201


FAIL_OPEN = [
    "secret unset",
    "webhook secret unset",
    "publishable unset",
    "setting off",
    "account cannot charge",
    "price 0",
    "price 49",
]


# 6: FENCE. Kills: failing closed (bookings refused while Stripe is off); a step for a price
# Stripe refuses.
@pytest.mark.parametrize("case", FAIL_OPEN)
def test_payments_fail_open(
    case: str,
    people: People,
    app: FastAPI,
    owner: TestClient,
    fake_stripe: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
    app_engine: Engine,
) -> None:
    service_id = make_shop(
        people,
        owner,
        fake_stripe,
        price={"price 0": 0, "price 49": 49}.get(case, 2500),
        enabled=case != "account cannot charge",
        check=case != "setting off",
    )
    keys = {
        "secret unset": "ZIF_STRIPE_SECRET_KEY",
        "webhook secret unset": "ZIF_STRIPE_WEBHOOK_SECRET",
        "publishable unset": "ZIF_STRIPE_PUBLISHABLE_KEY",
    }
    if case in keys:
        monkeypatch.setenv(keys[case], "")

    page = new_client(app).get(f"/api/public/booking-pages/{_slug(people)}")
    assert page.status_code == 200
    if case in ("price 0", "price 49"):
        assert page.json()["payment"] is not None
    else:
        assert page.json()["payment"] is None
    assert [s["payment"] for s in page.json()["services"]] == [False]

    refused = place(app, people, service_id, payment="check")
    assert (refused.status_code, refused.json()["code"]) == (422, "payment_not_offered")
    made = place(app, people, service_id, payment=None)
    assert made.status_code == 201
    assert made.json()["status"] == "pending"
    assert made.json()["payment"] is None
    assert pay_rows(people, made.json()["id"]) == []
    assert emails(app_engine, people, made.json()["id"])["booking_received"] == 1
    assert fake_stripe.count("create_check") == 0


def _slug(people: People) -> str:
    with tenant_context(people.a) as session:
        slug: str = session.scalar(text("SELECT slug FROM tenants WHERE id = :t"), {"t": people.a})
    return slug


# 7: FENCE. Kills: a client bypassing the check by omitting the field.
def test_a_payment_step_cannot_be_skipped(
    people: People, app: FastAPI, ready: str, fake_stripe: FakeGateway
) -> None:
    refused = place(app, people, ready, payment=None)

    assert (refused.status_code, refused.json()["code"]) == (422, "payment_required")
    with tenant_context(people.a) as session:
        assert session.scalar(text("SELECT count(*) FROM bookings")) == 0
    assert fake_stripe.count("create_check") == 0
    pay = place(app, people, ready, payment="pay")
    assert (pay.status_code, pay.json()["code"]) == (422, "payment_not_offered")


# 8: FENCE. Kills: amounts from the request; a stale service price; a reconcile that advances a
# booking on any successful intent.
def test_the_amount_is_the_snapshot_price(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
    app_engine: Engine,
    log_lines: Lines,
) -> None:
    client = new_client(app)
    booking_id = place(app, people, ready, client=client).json()["id"]
    (call,) = [params for op, params in fake_stripe.calls if op == "create_check"]
    assert (call["amount"], call["currency"]) == (2500, "eur")

    seed_booking(
        people.a,
        ready,
        member_id(people.a, people.both),
        at("12:00"),
        at("12:30"),
    )
    with tenant_context(people.a) as session:
        free = session.scalar(
            text("SELECT id FROM bookings WHERE source = 'merchant' ORDER BY created_at DESC")
        )
    insert = (
        "INSERT INTO booking_payments (tenant_id, booking_id, kind, intent_id, amount_minor, "
        "currency, fee_mode) VALUES (:t, :b, 'check', :i, :a, :c, 'business_pays')"
    )

    def refused(amount: int, currency: str) -> tuple[str, str]:
        return refused_as_app(
            app_engine,
            people.a,
            insert,
            t=people.a,
            b=free,
            i="pi_" + uuid.uuid4().hex[:12],
            a=amount,
            c=currency,
        )

    assert refused(2501, "EUR") == ("23503", "fk_booking_payments_booking_price")
    assert refused(2500, "USD") == ("23503", "fk_booking_payments_booking_price")
    assert refused(0, "EUR") == ("23514", "ck_booking_payments_amount_minor")

    fake_stripe.intents[fake_stripe.latest(booking_id)]["amount"] = 2501
    fake_stripe.confirm(fake_stripe.latest(booking_id))
    answer = sync(client, people, booking_id)

    assert answer.json()["status"] == "awaiting_payment"
    assert pay_rows(people, booking_id)[0]["status"] == "open"
    assert [line["level"] for line in log_lines() if "does not match" in line["msg"]] == ["ERROR"]


# 9: FENCE. Kills: dedupe missing; a reconcile that writes a transition twice when the sync route
# and the job race.
def test_one_event_reconciles_once(
    people: People,
    app: FastAPI,
    ready: str,
    fake_stripe: FakeGateway,
    app_engine: Engine,
    migrate_engine: Engine,
) -> None:
    client = new_client(app)
    booking_id = place(app, people, ready, client=client).json()["id"]
    intent = fake_stripe.latest(booking_id)
    fake_stripe.confirm(intent)
    body = event_body(
        "payment_intent.amount_capturable_updated", account=account_of(people), object_id=intent
    )
    for _ in range(2):
        assert (
            client.post(payments.WEBHOOK_PATH, content=body, headers=signed(body)).status_code
            == 200
        )
    evt = json.loads(body)["id"]
    with migrate_engine.connect() as conn:
        assert (
            conn.scalar(
                text("SELECT count(*) FROM jobs WHERE dedupe_key = :k"),
                {"k": f"stripe.event:{evt}"},
            )
            == 1
        )

    # The sync route and the job both read "open", then meet at Stripe, then both go for the lock.
    meet = threading.Barrier(2, timeout=20)

    def at_stripe(op: str) -> None:
        if op == "retrieve_intent":
            meet.wait()

    fake_stripe.on_call = at_stripe
    answers: list[Any] = []
    racer = threading.Thread(
        target=lambda: answers.append(sync(new_client(app), people, booking_id).status_code)
    )
    racer.start()
    payments.reconcile(people.a, intent)
    racer.join(30)
    fake_stripe.on_call = None

    assert answers == [200]
    assert history(people, booking_id) == ["created", "payment_checked"]
    assert emails(app_engine, people, booking_id) == Counter(
        {"booking_received": 1, "booking_request": 1}
    )
    run("stripe.event")
    assert history(people, booking_id) == ["created", "payment_checked"]


# 10: FENCE. Kills: PAID without `expires_at > now()` (a late success books a slot the checkout no
# longer holds).
def test_a_late_check_on_an_expired_booking_is_released_not_booked(
    people: People,
    app: FastAPI,
    ready: str,
    fake_stripe: FakeGateway,
    app_engine: Engine,
    migrate_engine: Engine,
) -> None:
    client = new_client(app)
    booking_id = place(app, people, ready, client=client).json()["id"]
    intent = fake_stripe.latest(booking_id)
    expire(people.a, booking_id)  # the 30 minutes passed; nothing has swept it yet
    fake_stripe.confirm(intent)

    payments.reconcile(people.a, intent)

    assert status_of(people, booking_id) == "awaiting_payment"
    assert history(people, booking_id) == ["created"]
    assert emails(app_engine, people, booking_id) == Counter()
    assert pay_rows(people, booking_id)[0]["status"] == "checked"
    bookings.sweep()
    assert status_of(people, booking_id) == "expired"
    due_now(migrate_engine, booking_id)
    run("payment.settle")
    assert fake_stripe.intents[intent]["status"] == "canceled"
    assert pay_rows(people, booking_id)[0]["settled_at"] is not None
    assert status_of(people, booking_id) == "expired"


# 11: FENCE. Kills: no due-time job (an open intent left forever; a late confirm unseen until a
# sweep).
def test_an_abandoned_checkout_is_cancelled_by_the_placement_job(
    people: People, app: FastAPI, ready: str, fake_stripe: FakeGateway, migrate_engine: Engine
) -> None:
    booking_id = place(app, people, ready).json()["id"]
    intent = fake_stripe.latest(booking_id)
    expire(people.a, booking_id)
    due_now(migrate_engine, booking_id)

    run("payment.settle")

    assert fake_stripe.intents[intent]["status"] == "canceled"
    (row,) = pay_rows(people, booking_id)
    assert (row["status"], row["settled_at"] is not None) == ("canceled", True)
    bookings.sweep()
    assert status_of(people, booking_id) == "expired"


# 12: FENCE. Kills: listing a 30-minute checkout hold as a booking; freeing the slot while the
# client pays.
def test_awaiting_payment_is_not_on_the_agenda(
    people: People, app: FastAPI, owner: TestClient, ready: str, fake_stripe: FakeGateway
) -> None:
    client = new_client(app)
    booking_id = place(app, people, ready, client=client).json()["id"]
    window = {"from": at("00:00"), "to": at("23:59")}

    def agenda() -> list[str]:
        return [row["id"] for row in owner.get("/api/bookings", params=window).json()]

    assert booking_id not in agenda()
    slots = client.get(
        f"/api/public/businesses/{people.a}/services/{ready}/availability",
        params={"from": DAY.isoformat(), "to": DAY.isoformat()},
    ).json()["slots"]
    assert at("09:00") not in slots
    refused = merchant_book(owner, ready, member_id=member_id(people.a, people.both))
    assert (refused.status_code, refused.json()["code"]) == (409, "slot_unavailable")

    fake_stripe.confirm(fake_stripe.latest(booking_id))
    assert sync(client, people, booking_id).json()["status"] == "pending"
    assert booking_id in agenda()


# 15: FENCE. Kills: the default `client` actor (the merchant reads "the client checked their card").
def test_payment_events_are_the_system_s(
    people: People, app: FastAPI, owner: TestClient, ready: str, fake_stripe: FakeGateway
) -> None:
    _, booking_id, _ = checked_booking(app, people, fake_stripe, ready)

    detail = owner.get(f"/api/bookings/{booking_id}").json()

    assert {e["event"]: e["actor"] for e in detail["history"]}["payment_checked"] == "system"
    assert detail["payment"] == {
        "kind": "check",
        "status": "checked",
        "amount_minor": 2500,
        "currency": "EUR",
        "refunded_minor": 0,
    }


# 16b: FENCE. Kills: an application fee on an authorisation; a payment row that reads the live
# account policy instead of its own snapshot.
@pytest.mark.parametrize("mode", [1, 2])
def test_a_check_records_the_fee_policy_and_takes_no_fee(
    mode: int,
    people: People,
    app: FastAPI,
    ready: str,
    fake_stripe: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
    migrate_engine: Engine,
    app_engine: Engine,
) -> None:
    from tests.test_stripe_gateway import PAYMENT_INTENT, Recorder

    if mode == 2:
        with migrate_engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
            conn.execute(
                text(
                    "UPDATE payment_accounts SET fee_mode = 'business_pays_plus_fee', "
                    "application_fee_bp = 150"
                )
            )
    http = Recorder(body=PAYMENT_INTENT)
    monkeypatch.setattr(stripe_gateway, "gateway", stripe_gateway.Gateway(http_client=http))

    made = place(app, people, ready)

    assert made.status_code == 201, made.text
    assert "application_fee" not in "".join(http.wire(i) for i in range(len(http.sent)))
    (row,) = pay_rows(people, made.json()["id"])
    assert row["application_fee_minor"] == 0
    assert row["fee_mode"] == ("business_pays" if mode == 1 else "business_pays_plus_fee")
    for column, value in (("fee_mode", "'platform_pays'"), ("application_fee_minor", "1")):
        sql = f"UPDATE booking_payments SET {column} = {value}"
        assert refused_as_app(app_engine, people.a, sql)[0] == "42501"


# 18: FENCE. Kills: a sweep key without a generation (jobs.py's ON CONFLICT DO NOTHING against the
# given-up row: nothing ever retries, 30 days of silence).
def test_a_given_up_settle_is_re_armed_by_the_next_hour(
    people: People,
    app: FastAPI,
    ready: str,
    fake_stripe: FakeGateway,
    app_engine: Engine,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, booking_id, intent = checked_booking(app, people, fake_stripe, ready)
    fake_stripe.fail_next("cancel_intent", times=10)

    def exhaust() -> None:
        for _ in range(5):
            due_now(migrate_engine, booking_id, placement=False)
            run("payment.settle")

    def hourly() -> list[str]:
        return [k for k in settle_keys(app_engine, people, booking_id) if ":h" in k]

    exhaust()
    assert fake_stripe.count("cancel_intent") == 5
    assert pay_rows(people, booking_id)[0]["settled_at"] is None

    bookings.sweep()
    assert len(hourly()) == 1
    exhaust()
    bookings.sweep()
    assert len(hourly()) == 1  # the same hour: nothing new
    assert fake_stripe.intents[intent]["status"] == "requires_capture"

    real = payments.hour
    monkeypatch.setattr(payments, "hour", lambda: str(int(real()) + 1))
    bookings.sweep()
    assert len(hourly()) == 2
    due_now(migrate_engine, booking_id, placement=False)
    run("payment.settle")

    assert fake_stripe.intents[intent]["status"] == "canceled"
    assert pay_rows(people, booking_id)[0]["settled_at"] is not None


# 19: FENCE. Kills: a booking stranded in awaiting_payment with no payment row (no client_secret
# ever reaches the page).
def test_a_failed_second_transaction_gives_the_time_back(
    people: People,
    app: FastAPI,
    ready: str,
    fake_stripe: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = new_client(app)
    address = fresh_email()
    with monkeypatch.context() as patched:
        patched.setattr(
            payments,
            "INSERT_PAYMENT",
            text("DO $$ BEGIN RAISE EXCEPTION 'busy' USING ERRCODE = '55P03'; END $$"),
        )
        refused = place(app, people, ready, client=client, email=address)

    assert (refused.status_code, refused.json()["code"]) == (503, "busy")
    with tenant_context(people.a) as session:
        booking_id = session.scalar(text("SELECT id FROM bookings"))
        assert session.scalar(text("SELECT count(*) FROM booking_payments")) == 0
    assert status_of(people, booking_id) == "expired"
    assert place(app, people, ready, client=client, email=address).status_code == 201


# 20: FENCE. Kills: a refresh or Back that strands the slot and the pending place for 30 minutes; a
# resume that mints a new intent each time (two live authorisations).
def test_a_client_can_come_back_or_start_over(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    fake_stripe: FakeGateway,
    both: str,
) -> None:
    assert put_settings(owner, {"max_pending_per_email": 1}).status_code == 200
    address = fresh_email()
    client = new_client(app)
    made = place(app, people, ready, client=client, email=address).json()
    booking_id, secret = made["id"], made["payment"]["client_secret"]
    intent = fake_stripe.latest(booking_id)
    held = place(app, people, ready, email=address, starts_at=at("10:00"))
    assert (held.status_code, held.json()["code"]) == (429, "rate_limited")

    first = resume(client, people, booking_id)
    assert first.status_code == 200 and first.json()["client_secret"] == secret
    assert first.json()["type"] == made["payment"]["type"]
    fake_stripe.decline(intent)
    second = resume(client, people, booking_id)
    assert second.json()["client_secret"] == secret
    assert len(fake_stripe.intents) == 1
    declined = sync(client, people, booking_id).json()
    assert declined["payment"] == {"kind": "check", "state": "open", "failed": True}
    other = resume(client, people, booking_id, "pay")
    assert (other.status_code, other.json()["code"]) == (422, "payment_not_offered")

    gone = abandon(client, people, booking_id)

    assert gone.status_code == 204
    assert status_of(people, booking_id) == "expired"
    assert fake_stripe.intents[intent]["status"] == "canceled"
    assert pay_rows(people, booking_id)[0]["status"] == "canceled"
    assert place(app, people, ready, email=address).status_code == 201


def test_a_resume_after_a_cancelled_intent_makes_the_next_one(
    people: People, app: FastAPI, ready: str, fake_stripe: FakeGateway
) -> None:
    client = new_client(app)
    made = place(app, people, ready, client=client).json()
    booking_id = made["id"]
    first = fake_stripe.latest(booking_id)
    fake_stripe.intents[first]["status"] = "canceled"

    again = resume(client, people, booking_id)

    assert again.status_code == 200
    assert again.json()["client_secret"] != made["payment"]["client_secret"]
    second = fake_stripe.latest(booking_id)
    assert second != first
    assert [row["status"] for row in pay_rows(people, booking_id)] == ["canceled", "open"]
    fake_stripe.confirm(second)
    assert sync(client, people, booking_id).json()["status"] == "pending"
    assert resume(client, people, booking_id).status_code == 404  # no longer awaiting payment


# 21: FENCE. Kills: a sync answer the page reads as "booked" while nothing is paid.
def test_sync_never_says_booked_before_the_money(
    people: People, app: FastAPI, ready: str, fake_stripe: FakeGateway
) -> None:
    client = new_client(app)
    booking_id = place(app, people, ready, client=client).json()["id"]
    intent = fake_stripe.latest(booking_id)

    fake_stripe.process(intent)
    processing = sync(client, people, booking_id)
    fake_stripe.decline(intent)
    declined = sync(client, people, booking_id)

    assert processing.json()["status"] == "awaiting_payment"
    assert processing.json()["payment"] == {"kind": "check", "state": "processing", "failed": False}
    assert declined.json()["status"] == "awaiting_payment"
    assert declined.json()["payment"] == {"kind": "check", "state": "open", "failed": True}
    assert "client_secret" not in processing.text + declined.text


# 22: FENCE. Kills: abandon writing `canceled` over a checked row (the forward-only rule broken; the
# console reads "check canceled" on a live booking).
def test_abandon_never_undoes_a_landed_check(
    people: People, app: FastAPI, ready: str, fake_stripe: FakeGateway
) -> None:
    client = new_client(app)
    booking_id = place(app, people, ready, client=client).json()["id"]
    intent = fake_stripe.latest(booking_id)

    def land(op: str) -> None:
        if op == "cancel_intent":
            fake_stripe.on_call = None
            fake_stripe.confirm(intent)
            payments.reconcile(people.a, intent)

    fake_stripe.on_call = land
    refused = abandon(client, people, booking_id)

    assert (refused.status_code, refused.json()["code"]) == (409, "payment_settled")
    assert status_of(people, booking_id) == "pending"
    assert pay_rows(people, booking_id)[0]["status"] == "checked"


# 23: FENCE. Kills: one shared per-IP budget (a processing poll, or one salon's wifi, locks every
# client out).
def test_the_payment_limit_is_per_booking(
    people: People, app: FastAPI, ready: str, fake_stripe: FakeGateway
) -> None:
    client = new_client(app)
    a = place(app, people, ready).json()["id"]
    b = place(app, people, ready, starts_at=at("10:00")).json()["id"]

    assert [sync(client, people, a).status_code for _ in range(60)] == [200] * 60
    over = sync(client, people, a)
    assert (over.status_code, over.json()["code"]) == (429, "rate_limited")
    assert sync(client, people, b).status_code == 200


# 23a: FENCE. Kills: a per-booking key alone (unlimited budget, one rate_limits row per guessed id).
def test_random_booking_ids_hit_the_ip_total(
    people: People, app: FastAPI, ready: str, fake_stripe: FakeGateway
) -> None:
    client = new_client(app)
    real = place(app, people, ready).json()["id"]

    codes = Counter(sync(client, people, str(uuid.uuid7())).status_code for _ in range(600))

    assert codes == Counter({404: 600})
    assert sync(client, people, real).status_code == 429


def test_the_payment_routes_answer_404_for_a_booking_without_a_payment(
    people: People, app: FastAPI, ready: str, fake_stripe: FakeGateway
) -> None:
    client = new_client(app)
    nobody = place(app, people, ready, payment="check").json()["id"]
    elsewhere = f"/api/public/businesses/{people.b}/bookings/{nobody}/payment"

    assert client.post(elsewhere + "/sync", headers=JSON).status_code == 404
    assert client.put(elsewhere, json={"kind": "check"}).status_code == 404
    assert client.post(elsewhere + "/abandon", headers=JSON).status_code == 404
    assert resume(client, people, str(uuid.uuid7())).status_code == 404


def test_stripe_down_at_sync_is_a_503(
    people: People, app: FastAPI, ready: str, fake_stripe: FakeGateway
) -> None:
    client = new_client(app)
    booking_id = place(app, people, ready, client=client).json()["id"]
    fake_stripe.fail_next("retrieve_intent")

    down = sync(client, people, booking_id)

    assert (down.status_code, down.json()["code"]) == (503, "payments_unavailable")
    assert sync(client, people, booking_id).status_code == 200


def test_the_booking_page_offers_the_payment_step(
    people: People, app: FastAPI, ready: str, fake_stripe: FakeGateway
) -> None:
    page = new_client(app).get(f"/api/public/booking-pages/{_slug(people)}").json()

    assert page["payment"] == {
        "publishable_key": "pk_test_not_a_real_key",
        "stripe_account": account_of(people),
        "check_with": "authorisation",
        "min_amount_minor": 50,
        "keeps_card_fee": False,
    }
    assert [s["payment"] for s in page["services"]] == [True]


def test_a_payment_under_way_at_the_deadline_is_not_cancelled(
    people: People, app: FastAPI, ready: str, fake_stripe: FakeGateway, migrate_engine: Engine
) -> None:
    booking_id = place(app, people, ready).json()["id"]
    intent = fake_stripe.latest(booking_id)
    fake_stripe.process(intent)  # an iDEAL payment Stripe will not cancel mid-flight
    expire(people.a, booking_id)
    due_now(migrate_engine, booking_id)

    run("payment.settle")

    assert pay_rows(people, booking_id)[0]["status"] == "open"
    with migrate_engine.connect() as conn:
        assert conn.scalar(
            text(
                "SELECT last_error IS NOT NULL FROM jobs WHERE kind = 'payment.settle' "
                "AND payload->>'booking_id' = :b AND completed_at IS NULL"
            ),
            {"b": booking_id},
        )

    fake_stripe.decline(intent)  # the bank said no: now it can go
    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET next_attempt_at = now() WHERE payload->>'booking_id' = :b"),
            {"b": booking_id},
        )
    run("payment.settle")

    (row,) = pay_rows(people, booking_id)
    assert (row["status"], row["settled_at"] is not None) == ("canceled", True)
    assert fake_stripe.intents[intent]["status"] == "canceled"
