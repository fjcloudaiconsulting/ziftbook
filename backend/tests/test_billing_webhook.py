"""ZIF-79: the platform's own Stripe events keep each business's subscriptions row in step."""
# ruff: noqa: F811  (the plan fixture is imported from test_entitlements)

import asyncio
import json
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import billing, payments
from app.db import tenant_context
from app.entitlements import entitlements
from app.jobs import run_once
from app.main import WEBHOOK_MAX_BODY, create_app
from app.stripe_gateway import Failed, Gateway, SubscriptionInfo
from app.worker import KINDS
from tests.conftest import People, jobs_as_the_worker, migrate_tenant_context, new_client
from tests.stripe_fake import WEBHOOK_SECRET, FakeGateway, signed
from tests.test_entitlements import Plan, plan  # noqa: F401 (fixture)
from tests.test_stripe_gateway import Recorder

BILLING_SECRET = "whsec_billing_test_secret_not_a_real_one"
JSON = {"content-type": "application/json"}
Lines = Callable[[], list[dict[str, Any]]]


@pytest.fixture
def stripe(fake_stripe: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    monkeypatch.setenv("ZIF_STRIPE_BILLING_WEBHOOK_SECRET", BILLING_SECRET)
    return fake_stripe


@pytest.fixture(autouse=True)
def clean_outbox(people: People, migrate_engine: Engine) -> Iterator[None]:
    yield
    # A plan change mails the owner (ZIF-78 enforcement). As the migrate role: the app role has no
    # DELETE on email_outbox (ZB-10).
    for tenant_id in (people.a, people.b):
        with migrate_tenant_context(migrate_engine, tenant_id) as conn:
            conn.execute(text("DELETE FROM email_outbox"))


@pytest.fixture
def client(people: People, stripe: FakeGateway) -> TestClient:
    return new_client(create_app())


def body_of(type_: str, obj: dict[str, Any], event_id: str | None = None, pad: int = 0) -> bytes:
    """A Stripe-shaped platform event (no `account`), indented like the real ones."""
    event = {
        "id": event_id or "evt_" + uuid.uuid4().hex[:24],
        "object": "event",
        "api_version": "2026-09-30.endive",
        "created": int(time.time()),
        "data": {"object": {**obj, "metadata": {"pad": "x" * pad}}},
        "livemode": False,
        "pending_webhooks": 1,
        "request": {"id": None, "idempotency_key": None},
        "type": type_,
    }
    return json.dumps(event, indent=2).encode()


def sub_event(sub_id: str, type_: str = "customer.subscription.updated", **kw: Any) -> bytes:
    return body_of(type_, {"id": sub_id, "object": "subscription"}, **kw)


def post(client: TestClient, body: bytes, secret: str = BILLING_SECRET) -> Any:
    return client.post(billing.WEBHOOK_PATH, content=body, headers=signed(body, secret=secret))


def billing_jobs(migrate_engine: Engine) -> dict[str, Any]:
    with migrate_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT dedupe_key, payload FROM jobs WHERE kind = 'billing.event'")
        ).all()
    return {r.dedupe_key: {k: v for k, v in r.payload.items() if k != "traceparent"} for r in rows}


def run_jobs() -> None:
    """Runs the queued jobs as the worker does, as ziftbook_worker."""

    async def drain() -> None:
        while await run_once(KINDS):
            pass

    with jobs_as_the_worker():
        asyncio.run(drain())


def row_of(tenant_id: uuid.UUID) -> Any:
    with tenant_context(tenant_id) as db:
        return db.execute(
            text("""
            SELECT stripe_customer_id, stripe_subscription_id, plan_id, status,
                   current_period_end, cancel_at_period_end
            FROM subscriptions""")
        ).first()


def price_of(migrate_engine: Engine, plan_id: uuid.UUID) -> str:
    with migrate_engine.connect() as conn:
        price: str = conn.scalar(
            text("SELECT stripe_price_id FROM plans WHERE id = :p"), {"p": plan_id}
        )
    return price


def enforce_jobs(migrate_engine: Engine, tenant_id: uuid.UUID) -> int:
    with migrate_engine.connect() as conn:
        found: int = conn.scalar(
            text("SELECT count(*) FROM jobs WHERE kind = 'billing.enforce' AND tenant_id = :t"),
            {"t": tenant_id},
        )
    return found


def source(tenant_id: uuid.UUID) -> str:
    with tenant_context(tenant_id) as db:
        return entitlements(db).source


# ZIF-79 fence. Kills: parsing before verifying; verifying a re-serialised body; no tolerance; an
# unset billing secret verifying anything (an empty key).
@pytest.mark.parametrize(
    "case", ["no header", "wrong secret", "changed body", "old", "signed not an event"]
)
def test_bad_signature_is_400(
    case: str, client: TestClient, people: People, stripe: FakeGateway, migrate_engine: Engine
) -> None:
    body = sub_event(stripe.add_subscription(people.a, None))
    sent, headers = body, signed(body, secret=BILLING_SECRET)
    if case == "no header":
        headers = JSON
    elif case == "wrong secret":
        headers = signed(body, secret="whsec_somebody_else")
    elif case == "changed body":
        sent = body + b" "
    elif case == "old":
        headers = signed(body, secret=BILLING_SECRET, t=int(time.time()) - 301)
    else:
        sent = b'{"hello": 1}'
        headers = signed(sent, secret=BILLING_SECRET)
    before = billing_jobs(migrate_engine)

    response = client.post(billing.WEBHOOK_PATH, content=sent, headers=headers)

    assert (response.status_code, response.json()) == (400, {"code": "bad_signature"})
    assert billing_jobs(migrate_engine) == before
    assert post(client, body).status_code == 200  # the control


# ZIF-79 fence. Kills: the Connect secret verifying platform events (or the other way round): a
# leaked Connect secret would then forge subscriptions.
def test_connect_secret_is_refused_here(
    client: TestClient, people: People, stripe: FakeGateway, migrate_engine: Engine
) -> None:
    body = sub_event(stripe.add_subscription(people.a, None))
    before = billing_jobs(migrate_engine)

    response = post(client, body, secret=WEBHOOK_SECRET)
    connect = client.post(
        payments.WEBHOOK_PATH, content=body, headers=signed(body, secret=BILLING_SECRET)
    )

    assert (response.status_code, response.json()) == (400, {"code": "bad_signature"})
    assert (connect.status_code, connect.json()) == (400, {"code": "bad_signature"})
    assert billing_jobs(migrate_engine) == before


# ZIF-79 fence. Kills: dedupe on the subscription (two events for one subscription collapse) or
# none; the subscription not found on a Checkout session or a failed invoice; an unhandled type or
# a Checkout without a subscription queued.
def test_events_queue_once_per_event_id(
    client: TestClient, people: People, stripe: FakeGateway, migrate_engine: Engine
) -> None:
    sub = stripe.add_subscription(people.a, None)
    created = sub_event(sub, "customer.subscription.created")
    session = body_of("checkout.session.completed", {"id": "cs_1", "subscription": sub})
    invoice = body_of(
        "invoice.payment_failed",
        {"id": "in_1", "parent": {"subscription_details": {"subscription": sub}}},
    )
    dropped = [
        body_of("checkout.session.completed", {"id": "cs_2", "subscription": None}),
        body_of("customer.created", {"id": "cus_1"}),
    ]
    before = billing_jobs(migrate_engine)

    answers = [post(client, b) for b in (created, created, session, invoice, *dropped)]

    assert [(a.status_code, a.json()) for a in answers] == [(200, {})] * 6
    queued = {k: v for k, v in billing_jobs(migrate_engine).items() if k not in before}
    ids = {t: json.loads(b)["id"] for t, b in (("c", created), ("s", session), ("i", invoice))}
    assert queued == {
        f"billing.event:{ids['c']}": {
            "event_id": ids["c"],
            "type": "customer.subscription.created",
            "subscription_id": sub,
        },
        f"billing.event:{ids['s']}": {
            "event_id": ids["s"],
            "type": "checkout.session.completed",
            "subscription_id": sub,
        },
        f"billing.event:{ids['i']}": {
            "event_id": ids["i"],
            "type": "invoice.payment_failed",
            "subscription_id": sub,
        },
    }


# ZIF-79 fence. Kills: the row written from the event body instead of Stripe's copy; a plan not
# taken from the Price; an update that inserts a second row or leaves a field behind; no
# enforcement check after a plan change; a job kind the worker does not run, or cannot write as
# its role.
def test_reconcile_creates_and_updates_the_row(
    client: TestClient, people: People, stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    first, second = plan({"places": 7}, price=True), plan({"places": 9}, price=True)
    sub = stripe.add_subscription(people.a, price_of(migrate_engine, first), status="trialing")
    customer = stripe.subscriptions[sub]["customer"]
    enforced = enforce_jobs(migrate_engine, people.a)

    post(client, sub_event(sub, "customer.subscription.created"))
    run_jobs()

    end = stripe.subscriptions[sub]["current_period_end"]
    assert tuple(row_of(people.a)) == (customer, sub, first, "trialing", end, False)
    assert source(people.a) == "subscription"
    assert enforce_jobs(migrate_engine, people.a) == enforced + 1

    later = datetime(2031, 2, 28, tzinfo=end.tzinfo)
    stripe.subscriptions[sub].update(
        status="active",
        price_id=price_of(migrate_engine, second),
        current_period_end=later,
        cancel_at_period_end=True,
    )
    post(client, sub_event(sub))
    run_jobs()

    assert tuple(row_of(people.a)) == (customer, sub, second, "active", later, True)
    assert enforce_jobs(migrate_engine, people.a) == enforced + 2
    assert row_of(people.b) is None


# ZIF-79 fence (Review Focus 3). Kills: trusting the event's order or body (a late `created` after
# an `updated` puts the old state back); a redelivered event applied twice.
def test_out_of_order_and_duplicate_events_end_identical(
    client: TestClient, people: People, stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    paid = plan({}, price=True)
    price = price_of(migrate_engine, paid)
    subs = {t: stripe.add_subscription(t, price, status="incomplete") for t in (people.a, people.b)}
    events = {
        t: [
            sub_event(s, "customer.subscription.created"),
            body_of("checkout.session.completed", {"id": "cs_x", "subscription": s}),
            sub_event(s),
        ]
        for t, s in subs.items()
    }
    for s in subs.values():  # what Stripe says by the time the worker looks
        stripe.subscriptions[s].update(status="active", cancel_at_period_end=True)

    for body in events[people.a]:
        post(client, body)
    for body in [*reversed(events[people.b]), events[people.b][0], events[people.b][2]]:
        post(client, body)
    run_jobs()
    post(client, events[people.b][0])  # redelivered after it ran
    run_jobs()

    a, b = row_of(people.a), row_of(people.b)
    assert (a.plan_id, a.status, a.current_period_end, a.cancel_at_period_end) == (
        b.plan_id,
        b.status,
        b.current_period_end,
        b.cancel_at_period_end,
    )
    assert (a.plan_id, a.status, a.cancel_at_period_end) == (paid, "active", True)


# ZIF-79 fence. Kills: a deleted subscription still giving its plan (kept active, or the row
# removed so nothing records it).
def test_deleted_subscription_falls_back(
    client: TestClient, people: People, stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    paid = plan({"places": 9}, price=True)
    sub = stripe.add_subscription(people.a, price_of(migrate_engine, paid))
    post(client, sub_event(sub, "customer.subscription.created"))
    run_jobs()
    assert source(people.a) == "subscription"

    stripe.subscriptions[sub]["status"] = "canceled"
    post(client, sub_event(sub, "customer.subscription.deleted"))
    run_jobs()

    assert row_of(people.a).status == "canceled"
    assert row_of(people.a).plan_id == paid
    assert source(people.a) != "subscription"


# ZIF-79 fence (R11). Kills: a dashboard mistake dropping a paying business to Free (plan_id
# overwritten with NULL); the status not updated because the plan was unknown; no ERROR for the
# operator, or one carrying more than the price.
def test_unknown_price_keeps_the_previous_plan(
    client: TestClient,
    people: People,
    stripe: FakeGateway,
    plan: Plan,
    migrate_engine: Engine,
    log_lines: Lines,
) -> None:
    paid = plan({}, price=True)
    sub = stripe.add_subscription(people.a, price_of(migrate_engine, paid), status="trialing")
    post(client, sub_event(sub, "customer.subscription.created"))
    run_jobs()

    stripe.subscriptions[sub].update(status="active", price_id="price_nobodyknows1")
    post(client, sub_event(sub))
    fresh = stripe.add_subscription(people.b, "price_nobodyknows2")
    post(client, sub_event(fresh, "customer.subscription.created"))
    run_jobs()

    assert (row_of(people.a).plan_id, row_of(people.a).status) == (paid, "active")
    assert source(people.a) == "subscription"
    assert (row_of(people.b).plan_id, row_of(people.b).status) == (None, "active")
    errors = [line for line in log_lines() if line["msg"] == "billing price matches no plan"]
    assert sorted(line["price"] for line in errors) == ["price_nobodyknows1", "price_nobodyknows2"]
    assert all(line["level"] == "ERROR" for line in errors)
    assert not any(sub in json.dumps(line) or fresh in json.dumps(line) for line in errors)


# ZIF-79 fence. Kills: a subscription landing on a plan whose Price an admin changed between the
# lookup and the upsert (the lookup's answer is stale here: it names another plan's id).
def test_a_price_moved_during_reconcile_fails_the_job(
    client: TestClient,
    people: People,
    stripe: FakeGateway,
    plan: Plan,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bought, moved = plan({}, price=True), plan({}, price=True)
    sub = stripe.add_subscription(people.a, price_of(migrate_engine, bought))
    stale = text("SELECT CAST(:moved AS uuid) WHERE CAST(:price AS text) IS NOT NULL")
    monkeypatch.setattr(billing, "PLAN_FOR_PRICE", stale.bindparams(moved=moved))
    body = sub_event(sub, "customer.subscription.created")
    post(client, body)

    run_jobs()

    with migrate_engine.connect() as conn:
        error = conn.scalar(
            text("SELECT last_error FROM jobs WHERE dedupe_key = :k"),
            {"k": f"billing.event:{json.loads(body)['id']}"},
        )
    assert error is not None and error.startswith("RuntimeError")
    assert row_of(people.a) is None


# ZIF-79 fence. Kills: a subscription with no business, or a forged one, raising (retried for days,
# then given up) or written anywhere.
@pytest.mark.parametrize("metadata", ["none", "not a uuid", "unknown business"])
def test_missing_tenant_metadata_is_logged_not_raised(
    metadata: str,
    client: TestClient,
    people: People,
    stripe: FakeGateway,
    plan: Plan,
    migrate_engine: Engine,
    log_lines: Lines,
) -> None:
    sub = stripe.add_subscription(people.a, price_of(migrate_engine, plan({}, price=True)))
    stripe.subscriptions[sub]["tenant_id"] = {
        "none": None,
        "not a uuid": "a; DROP TABLE subscriptions",
        "unknown business": str(uuid.uuid4()),
    }[metadata]
    body = sub_event(sub, "customer.subscription.created")
    post(client, body)

    run_jobs()

    with migrate_engine.connect() as conn:
        job = conn.execute(
            text("SELECT completed_at, last_error FROM jobs WHERE dedupe_key = :k"),
            {"k": f"billing.event:{json.loads(body)['id']}"},
        ).one()
    assert job.completed_at is not None and job.last_error is None
    assert row_of(people.a) is None
    errors = [line for line in log_lines() if line["level"] == "ERROR"]
    assert [line["msg"] for line in errors] == ["billing subscription names no business"]
    assert errors[0]["subscription"] == sub


# ZIF-79 fence (R20). Kills: a second live subscription replacing the first (one business billed
# twice, the first orphaned) or kept running; its payment kept; its cancellation event then
# replacing the live one; a Stripe call inside a transaction (the fake asserts it).
def test_a_second_live_subscription_is_cancelled_and_refunded(
    client: TestClient,
    people: People,
    stripe: FakeGateway,
    plan: Plan,
    migrate_engine: Engine,
    log_lines: Lines,
) -> None:
    price = price_of(migrate_engine, plan({}, price=True))
    kept = stripe.add_subscription(people.a, price)
    post(client, sub_event(kept, "customer.subscription.created"))
    run_jobs()
    before = tuple(row_of(people.a))

    extra = stripe.add_subscription(people.a, price)
    post(client, sub_event(extra, "customer.subscription.created"))
    run_jobs()

    assert tuple(row_of(people.a)) == before
    assert stripe.subscriptions[extra]["status"] == "canceled"
    assert [p for op, p in stripe.calls if op == "cancel_subscription"] == [
        {"sub_id": extra, "prorate": False}
    ]
    assert stripe.refunded_subscriptions == [extra]
    errors = [line for line in log_lines() if line["level"] == "ERROR"]
    assert [line["msg"] for line in errors] == ["billing duplicate subscription cancelled"]
    assert (errors[0]["subscription"], errors[0]["kept"]) == (extra, kept)

    post(client, sub_event(extra, "customer.subscription.deleted"))
    run_jobs()

    assert tuple(row_of(people.a)) == before
    assert stripe.count("cancel_subscription") == 1


# ZIF-79 fence (R20). Kills: a trial's newcomer refunded (it paid nothing) or left running.
def test_a_trialing_duplicate_is_cancelled_without_a_refund(
    client: TestClient, people: People, stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    price = price_of(migrate_engine, plan({}, price=True))
    kept = stripe.add_subscription(people.a, price)
    post(client, sub_event(kept, "customer.subscription.created"))
    run_jobs()

    extra = stripe.add_subscription(people.a, price, status="trialing")
    post(client, sub_event(extra, "customer.subscription.created"))
    run_jobs()

    assert row_of(people.a).stripe_subscription_id == kept
    assert stripe.subscriptions[extra]["status"] == "canceled"
    assert stripe.count("cancel_subscription") == 1
    assert stripe.count("refund_latest_invoice") == 0


# ZIF-79 fence (R20). Kills: a newcomer cancelled against a row that lags Stripe (the kept one
# already ended, its event not yet run): the business would be left with neither.
def test_a_duplicate_waits_while_the_kept_one_has_ended(
    client: TestClient, people: People, stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    price = price_of(migrate_engine, plan({}, price=True))
    kept = stripe.add_subscription(people.a, price)
    post(client, sub_event(kept, "customer.subscription.created"))
    run_jobs()
    stripe.subscriptions[kept]["status"] = "canceled"  # its event is still on the way

    new = stripe.add_subscription(people.a, price)
    body = sub_event(new, "customer.subscription.created")
    post(client, body)
    run_jobs()

    key = f"billing.event:{json.loads(body)['id']}"
    with migrate_engine.connect() as conn:
        error = conn.scalar(text("SELECT last_error FROM jobs WHERE dedupe_key = :k"), {"k": key})
    assert error is not None and error.startswith("RuntimeError")
    assert (stripe.count("cancel_subscription"), stripe.count("refund_latest_invoice")) == (0, 0)
    assert row_of(people.a).stripe_subscription_id == kept

    post(client, sub_event(kept, "customer.subscription.deleted"))
    run_jobs()
    with migrate_engine.begin() as conn:  # the retry's backoff, skipped
        conn.execute(
            text("UPDATE jobs SET next_attempt_at = now() WHERE dedupe_key = :k"), {"k": key}
        )
    run_jobs()

    assert (row_of(people.a).stripe_subscription_id, row_of(people.a).status) == (new, "active")
    assert stripe.count("cancel_subscription") == 0


# ZIF-79 fence (R11). Kills: a new subscription with an unknown Price inheriting the plan of the
# ended one it replaces (a plan nobody pays for).
def test_a_new_subscription_with_an_unknown_price_does_not_inherit_the_ended_plan(
    client: TestClient,
    people: People,
    stripe: FakeGateway,
    plan: Plan,
    migrate_engine: Engine,
    log_lines: Lines,
) -> None:
    old = stripe.add_subscription(people.a, price_of(migrate_engine, plan({}, price=True)))
    stripe.subscriptions[old]["status"] = "canceled"
    post(client, sub_event(old, "customer.subscription.created"))
    run_jobs()

    new = stripe.add_subscription(people.a, "price_nobodyknows3")
    post(client, sub_event(new, "customer.subscription.created"))
    run_jobs()

    row = row_of(people.a)
    assert (row.stripe_subscription_id, row.plan_id, row.status) == (new, None, "active")
    assert source(people.a) != "subscription"
    errors = [line for line in log_lines() if line["msg"] == "billing price matches no plan"]
    assert [(line["level"], line["price"]) for line in errors] == [("ERROR", "price_nobodyknows3")]


# ZIF-79 fence (R20). Kills: an ended subscription blocking the business's new one (cancelled as a
# duplicate, the business paying for nothing).
@pytest.mark.parametrize("ended", ["canceled", "incomplete_expired", "unpaid"])
def test_a_new_subscription_replaces_an_ended_one(
    ended: str,
    client: TestClient,
    people: People,
    stripe: FakeGateway,
    plan: Plan,
    migrate_engine: Engine,
) -> None:
    paid = plan({}, price=True)
    price = price_of(migrate_engine, paid)
    old = stripe.add_subscription(people.a, price, status=ended)
    post(client, sub_event(old, "customer.subscription.created"))
    run_jobs()

    new = stripe.add_subscription(people.a, price, customer=stripe.subscriptions[old]["customer"])
    post(client, sub_event(new, "customer.subscription.created"))
    run_jobs()

    assert (row_of(people.a).stripe_subscription_id, row_of(people.a).status) == (new, "active")
    assert stripe.count("cancel_subscription") == 0


# ZIF-79 fence. Kills: the billing webhook left under the browser's 64 KiB cap (a silent 413 until
# Stripe disables the endpoint).
def test_the_billing_webhook_takes_256_kib(
    client: TestClient, people: People, stripe: FakeGateway
) -> None:
    body = sub_event(stripe.add_subscription(people.a, None), pad=200 * 1024)
    assert 200 * 1024 < len(body) < WEBHOOK_MAX_BODY

    assert post(client, body).status_code == 200


# ZIF-79 fence. Kills: treating an unset billing secret (or secret key) as on.
@pytest.mark.parametrize("name", ["ZIF_STRIPE_SECRET_KEY", "ZIF_STRIPE_BILLING_WEBHOOK_SECRET"])
def test_the_billing_webhook_answers_503_while_unset(
    name: str, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(name)
    body = sub_event("sub_1")

    response = post(client, body)

    assert (response.status_code, response.json()) == (503, {"code": "billing_unavailable"})


def test_the_billing_webhook_is_not_in_the_api_contract(client: TestClient) -> None:
    assert billing.WEBHOOK_PATH not in create_app().openapi()["paths"]


class Replies(Recorder):
    """Answers each Stripe call with the next body; an int status before a body sends it as that
    status (an error body)."""

    def __init__(self, *bodies: dict[str, Any] | tuple[int, dict[str, Any]]) -> None:
        super().__init__()
        self.bodies = list(bodies)

    def request(self, *args: Any, **kwargs: Any) -> tuple[str, int, Any]:
        reply = self.bodies.pop(0)
        status, self.body = reply if isinstance(reply, tuple) else (200, reply)
        _, _, headers = super().request(*args, **kwargs)
        return json.dumps(self.body), status, headers


def subscription_body(**extra: Any) -> dict[str, Any]:
    return {
        "id": "sub_1",
        "object": "subscription",
        "customer": "cus_1",
        "status": "past_due",
        "cancel_at_period_end": True,
        "metadata": {"tenant_id": "t"},
        "latest_invoice": "in_1",
        "items": {
            "object": "list",
            "data": [
                {
                    "id": "si_1",
                    "object": "subscription_item",
                    "current_period_end": 1900000000,
                    "price": {"id": "price_1", "object": "price"},
                }
            ],
        },
        **extra,
    }


# ZIF-79 fence. Kills: the period read off the subscription (gone since API 2025-03-31: always
# None); the tenant or Price not found where Checkout put them; a call on a connected account.
def test_retrieve_subscription_reads_stripes_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_STRIPE_SECRET_KEY", "sk_test_not_a_real_key")
    http = Replies(subscription_body(), subscription_body(metadata={}))
    gateway = Gateway(http_client=http)

    found, bare = gateway.retrieve_subscription("sub_1"), gateway.retrieve_subscription("sub_1")

    end = datetime.fromtimestamp(1900000000, tz=UTC)
    assert found == SubscriptionInfo("sub_1", "cus_1", "past_due", "price_1", end, True, "t")
    assert bare.tenant_id is None
    assert http.wire(0).startswith("get https://api.stripe.com/v1/subscriptions/sub_1")
    assert "Stripe-Account" not in http.headers[0]


# ZIF-79 fence (R20). Kills: a duplicate cancelled at period end or with a proration credit; its
# payment not refunded, refunded twice on a retry (no idempotency key), or a refund tried for a
# trial that paid nothing.
def test_cancel_and_refund_a_duplicate_at_stripe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_STRIPE_SECRET_KEY", "sk_test_not_a_real_key")
    paid = {
        "object": "list",
        "url": "/v1/invoice_payments",
        "has_more": False,
        "data": [
            {
                "id": "inpay_1",
                "object": "invoice_payment",
                "status": "paid",
                "payment": {"type": "payment_intent", "payment_intent": "pi_1"},
            }
        ],
    }
    http = Replies(
        subscription_body(),
        paid,
        {"id": "re_1", "object": "refund"},
        subscription_body(status="canceled"),
        subscription_body(),
        {**paid, "data": []},
    )
    gateway = Gateway(http_client=http)

    gateway.refund_latest_invoice("sub_1")
    gateway.cancel_subscription("sub_1", prorate=False)
    gateway.refund_latest_invoice("sub_1")  # a trial: nothing paid

    assert [http.wire(i).split(" ")[:2] for i in range(len(http.sent))] == [
        ["get", "https://api.stripe.com/v1/subscriptions/sub_1"],
        ["get", "https://api.stripe.com/v1/invoice_payments?invoice=in_1&status=paid"],
        ["post", "https://api.stripe.com/v1/refunds"],
        ["delete", "https://api.stripe.com/v1/subscriptions/sub_1?prorate=false"],
        ["get", "https://api.stripe.com/v1/subscriptions/sub_1"],
        ["get", "https://api.stripe.com/v1/invoice_payments?invoice=in_1&status=paid"],
    ]
    assert http.wire(2).endswith("payment_intent=pi_1")
    assert http.headers[2]["Idempotency-Key"] == "billing-refund:pi_1"


# ZIF-79 fence (R20). Kills: a payment already refunded in full (an earlier run past the key's 24
# hours, or by hand) failing every retry, so the duplicate is never cancelled; any other refusal
# swallowed.
def test_an_already_refunded_payment_counts_as_done(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_STRIPE_SECRET_KEY", "sk_test_not_a_real_key")
    paid = {
        "object": "list",
        "url": "/v1/invoice_payments",
        "has_more": False,
        "data": [
            {
                "id": "inpay_1",
                "object": "invoice_payment",
                "status": "paid",
                "payment": {"type": "charge", "charge": "ch_1", "payment_intent": None},
            }
        ],
    }

    def refused(code: str) -> tuple[int, dict[str, Any]]:
        error = {"type": "invalid_request_error", "code": code, "message": "no"}
        return 400, {"error": error}

    http = Replies(
        subscription_body(),
        paid,
        refused("charge_already_refunded"),
        subscription_body(),
        paid,
        refused("amount_too_large"),
    )
    gateway = Gateway(http_client=http)

    gateway.refund_latest_invoice("sub_1")
    with pytest.raises(Failed):
        gateway.refund_latest_invoice("sub_1")

    assert http.wire(2).endswith("charge=ch_1")
