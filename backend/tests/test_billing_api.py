"""ZIF-79: owners subscribe through Stripe Checkout and manage billing in the customer portal."""
# ruff: noqa: F811  (the plan fixtures are imported from test_entitlements)

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import billing, entitlements
from app.main import create_app
from app.stripe_gateway import CouponInfo, Failed, Gateway, PriceInfo
from tests.conftest import People, give_plan, signed_in
from tests.stripe_fake import FakeGateway
from tests.test_entitlements import (  # noqa: F401 (fixtures)
    Plan,
    grant,
    plan,
    set_trial_end,
    subscribe,
    trial_plan,
    write,
)
from tests.test_stripe_gateway import Recorder

JSON = {"content-type": "application/json"}
SETTINGS = "http://localhost:3000/nl/settings?tab=plan"
UNAVAILABLE = {"code": "billing_unavailable"}


@pytest.fixture
def app(people: People, fake_stripe: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(entitlements, "_prices", {})  # each test sees Stripe afresh
    monkeypatch.setattr(entitlements, "_coupons", {})
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)  # their own language is nl


def price_of(migrate_engine: Engine, plan_id: uuid.UUID) -> str:
    with migrate_engine.begin() as conn:
        price: str = conn.scalar(
            text("SELECT stripe_price_id FROM plans WHERE id = :p"), {"p": plan_id}
        )
    return price


def offer(
    migrate_engine: Engine, tenant_id: uuid.UUID, plan_id: uuid.UUID, coupon: str | None = None
) -> None:
    write(
        migrate_engine,
        tenant_id,
        """INSERT INTO plan_offers (tenant_id, plan_id, stripe_coupon_id, reason, admin_user_id)
           VALUES (:t, :p, :c, 'test', :t)""",
        p=plan_id,
        c=coupon,
    )


def checkout(client: TestClient, plan_id: uuid.UUID | str) -> Any:
    return client.post("/api/billing/checkout", json={"plan_id": str(plan_id)})


def sent(fake: FakeGateway, op: str) -> list[dict[str, Any]]:
    return [params for name, params in fake.calls if name == op]


# ZIF-79 fence. Kills: a session the webhook cannot tie to its business (no tenant in Stripe's
# copy); another plan's Price; Stripe's page in another language than the person's; a cached URL;
# a transaction held across the Stripe call (the fake asserts it).
def test_checkout_carries_tenant_metadata_and_price(
    people: People, owner: TestClient, fake_stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    paid = plan({}, public=True, price=True)

    response = checkout(owner, paid)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["url"].startswith("https://checkout.stripe.test/")
    assert sent(fake_stripe, "create_checkout") == [
        {
            "customer": None,
            "price": price_of(migrate_engine, paid),
            "coupon": None,
            "tenant_id": str(people.a),
            "trial_end": None,
            "success_url": f"{SETTINGS}&billing=done",
            "cancel_url": SETTINGS,
            "locale": "nl",
            "key": sent(fake_stripe, "create_checkout")[0]["key"],
        }
    ]
    assert sent(fake_stripe, "create_checkout")[0]["key"].startswith(f"checkout:{people.a}:")
    assert fake_stripe.tenants == [str(people.a)]


# ZIF-79 fence. Kills: a double click making two Checkout Sessions (and, paid twice, two
# subscriptions): the same params within five minutes send one idempotency key; a changed param
# (another plan, the person's new language) reusing it, which Stripe refuses (a 503).
def test_a_double_click_gets_the_same_checkout(
    people: People,
    owner: TestClient,
    fake_stripe: FakeGateway,
    plan: Plan,
    migrate_engine: Engine,
) -> None:
    paid, other = plan({}, public=True, price=True), plan({}, public=True, price=True)

    answers = [checkout(owner, paid), checkout(owner, paid), checkout(owner, other)]
    with migrate_engine.begin() as conn:
        conn.execute(text("UPDATE users SET locale = 'pt' WHERE id = :u"), {"u": people.both})
    answers.append(checkout(owner, paid))

    assert [a.status_code for a in answers] == [200] * 4
    keys = [s["key"] for s in sent(fake_stripe, "create_checkout")]
    assert keys[0] == keys[1] and len(set(keys)) == 3
    urls = [a.json()["url"] for a in answers]
    assert urls[0] == urls[1] and len(set(urls)) == 3
    assert sent(fake_stripe, "create_checkout")[3]["locale"] == "pt"


# ZIF-79 fence. Kills: Checkout outside subscription mode; the tenant left off the session or the
# subscription (the webhook finds no business); a coupon, customer or trial sent when there is
# none (Stripe refuses an empty one).
def test_the_checkout_session_is_a_subscription_for_the_business(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZIF_STRIPE_SECRET_KEY", "sk_test_not_a_real_key")
    http = Recorder(body={"id": "cs_1", "object": "checkout.session", "url": "https://x.test/c"})
    gateway = Gateway(http_client=http)
    args: dict[str, Any] = {
        "price": "price_1",
        "tenant_id": "t",
        "success_url": "https://app.test/done",
        "cancel_url": "https://app.test/back",
        "locale": "pt",
        "idempotency_key": "checkout:t:p:1",
    }

    url = gateway.create_checkout(customer="cus_1", coupon="SPRING", trial_end=1900000000, **args)
    bare = gateway.create_checkout(customer=None, coupon=None, trial_end=None, **args)

    assert url == bare == "https://x.test/c"
    full, plain = http.wire(0), http.wire(1)
    assert full.startswith("post https://api.stripe.com/v1/checkout/sessions ")
    for part in (
        "mode=subscription",
        "client_reference_id=t",
        "subscription_data[metadata][tenant_id]=t",
        "line_items[0][price]=price_1",
        "line_items[0][quantity]=1",
        "locale=pt",
        "success_url=https%3A%2F%2Fapp.test%2Fdone",
        "cancel_url=https%3A%2F%2Fapp.test%2Fback",
    ):
        assert part in full and part in plain
    assert "customer=cus_1" in full and "discounts[0][coupon]=SPRING" in full
    assert "subscription_data[trial_end]=1900000000" in full
    assert "customer" not in plain and "discounts" not in plain and "trial_end" not in plain
    assert "Stripe-Account" not in http.headers[0]  # the platform's own account
    assert http.headers[0]["Idempotency-Key"] == "checkout:t:p:1"


# ZIF-79 fence. Kills: the offer's coupon dropped (the business pays the list price); another
# business's coupon applied.
def test_checkout_applies_the_offer_coupon(
    people: People, owner: TestClient, fake_stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    offered = plan({}, public=True, price=True)
    other = plan({}, public=True, price=True)
    offer(migrate_engine, people.a, offered, "SPRING")
    offer(migrate_engine, people.b, other, "NOT_YOURS")
    fake_stripe.coupons["SPRING"] = CouponInfo(True, 20.0, None, "forever", None, None)

    assert checkout(owner, offered).status_code == 200
    assert checkout(owner, other).status_code == 200

    assert [s["coupon"] for s in sent(fake_stripe, "create_checkout")] == ["SPRING", None]


# ZIF-79 fence. Kills: a coupon Stripe says is spent or deleted sent to Checkout (Stripe refuses
# it, the owner sees "billing unavailable"); checkout blocked while Stripe can't say (unknown is
# not invalid: the coupon goes along and Stripe decides).
def test_a_dead_coupon_is_refused_and_an_unknown_one_goes_along(
    people: People, owner: TestClient, fake_stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    spent, gone, unknown = (plan({}, price=True) for _ in range(3))
    offer(migrate_engine, people.a, spent, "SPENT")
    offer(migrate_engine, people.a, gone, "GONE")
    offer(migrate_engine, people.a, unknown, "LATER")
    fake_stripe.coupons["SPENT"] = CouponInfo(False, 50.0, None, "once", None, None)
    fake_stripe.coupons["LATER"] = CouponInfo(True, 50.0, None, "once", None, None)

    for dead in (spent, gone):
        response = checkout(owner, dead)
        assert (response.status_code, response.json()) == (409, {"code": "coupon_unavailable"})
    fake_stripe.fail_next("retrieve_coupon")
    assert checkout(owner, unknown).status_code == 200

    assert [s["coupon"] for s in sent(fake_stripe, "create_checkout")] == ["LATER"]


# ZIF-79 fence. Kills: the trial's days lost on subscribing (charged at once); a trial_end Stripe
# refuses (under 48 hours away); free days given to a business that is not in a trial (no trial
# plan set).
def test_checkout_keeps_trial_days(
    people: People,
    owner: TestClient,
    fake_stripe: FakeGateway,
    plan: Plan,
    trial_plan: uuid.UUID,
    migrate_engine: Engine,
) -> None:
    paid = plan({}, public=True, price=True)
    ends = datetime.now(UTC) + timedelta(days=10)
    set_trial_end(migrate_engine, people.a, ends)

    assert checkout(owner, paid).status_code == 200
    set_trial_end(migrate_engine, people.a, datetime.now(UTC) + timedelta(hours=47))
    assert checkout(owner, paid).status_code == 200
    set_trial_end(migrate_engine, people.a, ends)
    with migrate_engine.begin() as conn:  # the trial_plan fixture puts it back
        conn.execute(text("UPDATE platform_billing SET trial_plan_id = NULL"))
    assert checkout(owner, paid).status_code == 200

    assert [s["trial_end"] for s in sent(fake_stripe, "create_checkout")] == [
        int(ends.timestamp()),
        None,
        None,
    ]


# ZIF-79 fence. Kills: a private plan bought by anyone who learns its id; an offer to one business
# opening the plan to another.
def test_private_plan_needs_an_offer(
    people: People, owner: TestClient, fake_stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    private = plan({}, price=True)

    assert checkout(owner, private).json() == {"code": "plan_not_found"}
    offer(migrate_engine, people.b, private)
    assert checkout(owner, private).json() == {"code": "plan_not_found"}
    assert checkout(owner, uuid.uuid4()).status_code == 404
    offer(migrate_engine, people.a, private)
    assert checkout(owner, private).status_code == 200
    assert len(sent(fake_stripe, "create_checkout")) == 1


# ZIF-79 fence. Kills: an archived plan sold on, publicly or through an old offer.
def test_archived_plan_is_refused(
    people: People, owner: TestClient, fake_stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    archived = plan({}, public=True, price=True, archived=True)
    offered = plan({}, price=True, archived=True)
    offer(migrate_engine, people.a, offered)

    for plan_id in (archived, offered):
        response = checkout(owner, plan_id)
        assert (response.status_code, response.json()) == (404, {"code": "plan_not_found"})
    assert sent(fake_stripe, "create_checkout") == []


# ZIF-79 fence. Kills: a Checkout for a plan with no Price (Free), which Stripe would refuse as a
# 503 instead of the owner being told.
def test_a_plan_without_a_price_is_not_purchasable(
    owner: TestClient, fake_stripe: FakeGateway, plan: Plan
) -> None:
    free = plan({}, public=True)

    response = checkout(owner, free)

    assert (response.status_code, response.json()) == (409, {"code": "not_purchasable"})
    assert sent(fake_stripe, "create_checkout") == []


# ZIF-79 fence. Kills: a second subscription for a business that has one, also one Stripe still
# bills but does not count (unpaid); a new Stripe customer for a business that had one.
def test_subscribed_business_is_sent_to_the_portal(
    people: People, owner: TestClient, fake_stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    paid = plan({}, public=True, price=True)
    subscribe(migrate_engine, people.a, paid)

    for status in ("active", "trialing", "past_due", "unpaid", "incomplete"):
        write(migrate_engine, people.a, "UPDATE subscriptions SET status = :s", s=status)
        response = checkout(owner, paid)
        assert (response.status_code, response.json()) == (409, {"code": "already_subscribed"})
    write(migrate_engine, people.a, "UPDATE subscriptions SET status = 'canceled'")
    assert checkout(owner, paid).status_code == 200

    assert [s["customer"] for s in sent(fake_stripe, "create_checkout")] == ["cus_1"]


# ZIF-79 fence. Kills: a business given a plan free of charge paying for one anyway.
def test_granted_business_cannot_checkout(
    people: People, owner: TestClient, fake_stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    paid = plan({}, public=True, price=True)
    give_plan(people.a, {"places": 2})

    response = checkout(owner, paid)

    assert (response.status_code, response.json()) == (409, {"code": "plan_granted"})
    write(migrate_engine, people.a, "DELETE FROM plan_grants")
    grant(migrate_engine, people.a, paid, datetime.now(UTC) - timedelta(days=1))  # ended
    assert checkout(owner, paid).status_code == 200


# ZIF-79 fence. Kills: a worker subscribing the business or opening its billing.
def test_worker_gets_403(
    people: People, app: FastAPI, fake_stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    paid = plan({}, public=True, price=True)
    subscribe(migrate_engine, people.a, paid, status="canceled")
    worker = signed_in(app, people.a, people.only_a)

    for response in (
        checkout(worker, paid),
        worker.post("/api/billing/checkout", json={}),  # whatever the body
        worker.post("/api/billing/portal", headers=JSON),
    ):
        assert (response.status_code, response.json()) == (403, {"code": "owner_only"})
    assert fake_stripe.calls == []


# ZIF-79 fence. Kills: a portal session without a customer (Stripe refuses: a 503 for no reason);
# the owner sent back somewhere else or in another language.
def test_portal_needs_a_subscription(
    people: People, owner: TestClient, fake_stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    missing = owner.post("/api/billing/portal", headers=JSON)
    assert (missing.status_code, missing.json()) == (409, {"code": "no_subscription"})

    subscribe(migrate_engine, people.a, plan({}, public=True, price=True), status="canceled")
    response = owner.post("/api/billing/portal", headers=JSON)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["url"] == "https://billing.stripe.test/p/cus_1"
    assert sent(fake_stripe, "create_portal") == [
        {"customer": "cus_1", "return_url": SETTINGS, "locale": "nl"}
    ]


# ZIF-79 fence. Kills: a portal session on a connected account or without its return address.
def test_the_portal_session_is_on_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_STRIPE_SECRET_KEY", "sk_test_not_a_real_key")
    http = Recorder(body={"id": "bps_1", "object": "billing_portal.session", "url": "https://p"})

    url = Gateway(http_client=http).create_portal("cus_1", "https://app.test/back", "nl")

    assert url == "https://p"
    wire = http.wire(0)
    assert wire.startswith("post https://api.stripe.com/v1/billing_portal/sessions ")
    assert "customer=cus_1" in wire and "return_url=https%3A%2F%2Fapp.test%2Fback" in wire
    assert "locale=nl" in wire and "Stripe-Account" not in http.headers[0]


def info(product: str, interval: str = "month") -> PriceInfo:
    return PriceInfo(
        active=True,
        recurring=True,
        currency="eur",
        unit_amount=2500,
        interval=interval,
        product=product,
    )


# ZIF-79 fence. Kills: the portal offering a private, archived or unpaid plan, or missing a public
# one; two Prices of one product split across entries (Stripe wants them under their product);
# a sync that fails every plan save where Stripe is not set up (local, tests).
def test_sync_portal_offers_the_public_paid_plans(
    bound: None,
    fake_stripe: FakeGateway,
    plan: Plan,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monthly = plan({}, public=True, price=True)
    yearly = plan({}, public=True, price=True)
    other = plan({}, public=True, price=True)
    private = plan({}, price=True)
    archived = plan({}, public=True, price=True, archived=True)
    plan({}, public=True)
    prices = {p: price_of(migrate_engine, p) for p in (monthly, yearly, other, private, archived)}
    fake_stripe.prices.update(
        {
            prices[monthly]: info("prod_a"),
            prices[yearly]: info("prod_a", "year"),
            prices[other]: info("prod_b"),
            prices[private]: info("prod_c"),
            prices[archived]: info("prod_a"),
        }
    )

    billing.sync_portal()

    # Other tests' plans may be public at this moment; the fake knows none of their Prices.
    assert fake_stripe.portal == {
        "prod_a": [prices[monthly], prices[yearly]],
        "prod_b": [prices[other]],
    }
    asked = sent(fake_stripe, "list_prices")[0]["ids"]
    assert prices[private] not in asked and prices[archived] not in asked
    monkeypatch.delenv("ZIF_STRIPE_SECRET_KEY")
    fake_stripe.calls.clear()
    billing.sync_portal()
    assert fake_stripe.calls == []


# ZIF-79 fence. Kills: two plan saves syncing the portal out of order (the first save's read pushed
# after the second's), leaving an archived plan for sale in the portal.
def test_interleaved_portal_syncs_end_on_the_newer_plans(
    bound: None,
    fake_stripe: FakeGateway,
    plan: Plan,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kept = plan({}, public=True, price=True)
    archived = plan({}, public=True, price=True)
    prices = {p: price_of(migrate_engine, p) for p in (kept, archived)}
    fake_stripe.prices.update({prices[kept]: info("prod_a"), prices[archived]: info("prod_b")})
    push = fake_stripe.sync_portal_configuration
    second: list[bool] = []

    def second_save_lands_first(found: dict[str, list[str]]) -> None:
        if not second:  # the first save's push waits while the second save archives and syncs
            second.append(True)
            with migrate_engine.begin() as conn:
                conn.execute(
                    text("UPDATE plans SET archived_at = now() WHERE id = :p"), {"p": archived}
                )
            billing.sync_portal()
            assert fake_stripe.portal == {"prod_a": [prices[kept]]}
        push(found)

    monkeypatch.setattr(fake_stripe, "sync_portal_configuration", second_save_lands_first)
    billing.sync_portal()

    assert second == [True]
    assert fake_stripe.portal == {"prod_a": [prices[kept]]}


# ZIF-79 fence. Kills: the portal left offering a plan that is no longer for sale; plan changes
# enabled with no product (Stripe refuses); a silent sync when the account has no portal set up.
def test_the_portal_configuration_is_the_default_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_STRIPE_SECRET_KEY", "sk_test_not_a_real_key")
    listed = {
        "object": "list",
        "url": "/v1/billing_portal/configurations",
        "has_more": False,
        "data": [{"id": "bpc_1", "object": "billing_portal.configuration", "is_default": True}],
    }
    http = Recorder(body=listed)
    gateway = Gateway(http_client=http)

    gateway.sync_portal_configuration({"prod_a": ["price_1", "price_2"]})
    gateway.sync_portal_configuration({})

    assert "is_default=true" in http.wire(0)
    offered, none = http.wire(1), http.wire(3)
    assert offered.startswith("post https://api.stripe.com/v1/billing_portal/configurations/bpc_1 ")
    assert "features[subscription_update][enabled]=true" in offered
    assert "features[subscription_update][products][0][product]=prod_a" in offered
    assert "features[subscription_update][products][0][prices][1]=price_2" in offered
    assert "features[subscription_cancel][mode]=at_period_end" in offered
    assert "features[subscription_update][enabled]=false" in none
    assert "products" not in none
    with pytest.raises(Failed):
        Gateway(http_client=Recorder(body={**listed, "data": []})).sync_portal_configuration({})


# ZIF-79 fence. Kills: a Stripe outage (or a coupon deleted in Stripe) answered 500; billing
# reached when Stripe is not set up.
def test_stripe_down_is_503_not_500(
    people: People,
    owner: TestClient,
    fake_stripe: FakeGateway,
    plan: Plan,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paid = plan({}, public=True, price=True)
    fake_stripe.fail_next("create_checkout")
    assert (checkout(owner, paid).status_code, checkout(owner, paid).status_code) == (503, 200)
    fake_stripe.fail_next("create_checkout", "invalid")
    assert checkout(owner, paid).json() == UNAVAILABLE
    subscribe(migrate_engine, people.a, paid, status="canceled")
    fake_stripe.fail_next("create_portal")
    portal = owner.post("/api/billing/portal", headers=JSON)
    assert (portal.status_code, portal.json()) == (503, UNAVAILABLE)

    monkeypatch.delenv("ZIF_STRIPE_SECRET_KEY")
    assert checkout(owner, paid).json() == UNAVAILABLE
    assert owner.post("/api/billing/portal", headers=JSON).json() == UNAVAILABLE


# ZIF-79 fence. Kills: Stripe's per-account rate limit spent by one business's clicks.
def test_checkout_is_rate_limited_per_business(
    owner: TestClient, fake_stripe: FakeGateway, plan: Plan
) -> None:
    paid = plan({}, public=True, price=True)

    answers = [checkout(owner, paid).status_code for _ in range(billing.STRIPE_LIMIT + 1)]

    assert answers == [200] * billing.STRIPE_LIMIT + [429]


# ZIF-79 fence. Kills: plan options without their price; Stripe asked on every console load (the
# 5-minute cache); the plan list failing when Stripe is down, instead of answering without
# prices; a transaction held across the Stripe call (the fake asserts it).
def test_plan_options_carry_their_stripe_price(
    people: People, owner: TestClient, fake_stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    paid = plan({}, public=True, price=True)
    down = plan({}, public=True, price=True)
    fake_stripe.prices[price_of(migrate_engine, paid)] = info("prod_a", "year")

    def option(plan_id: uuid.UUID) -> dict[str, Any]:
        body = owner.get("/api/entitlements").json()
        (found,) = (o for o in body["offers"] if o["id"] == str(plan_id))
        return {k: found[k] for k in ("unit_amount", "currency", "interval")}

    assert option(paid) == {"unit_amount": 2500, "currency": "eur", "interval": "year"}
    calls = fake_stripe.count("list_prices")
    assert option(paid)["unit_amount"] == 2500
    assert fake_stripe.count("list_prices") == calls  # cached

    entitlements._prices.clear()
    fake_stripe.fail_next("list_prices")
    assert option(down) == {"unit_amount": None, "currency": None, "interval": None}


# ZIF-79 fence. Kills: an offer's discount missing from its plan option (the console can't show
# the discounted price); a coupon's terms missing (a first-month discount shown as forever); a
# deleted, spent or other-currency coupon shown as a discount; a dead coupon told apart from an
# unknown one no longer; Stripe asked for coupons on every console load; the plan list failing
# when Stripe is down, instead of answering without the discount.
def test_plan_options_carry_their_coupon_discount(
    people: People, owner: TestClient, fake_stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    coupons = {
        "HALF": CouponInfo(True, 12.5, None, "forever", None, None),
        "FIVE": CouponInfo(True, None, 500, "once", None, "eur"),
        "THREE": CouponInfo(True, 10.0, None, "repeating", 3, None),
        "DOLLARS": CouponInfo(True, None, 500, "once", None, "usd"),
        "SPENT": CouponInfo(False, 50.0, None, "once", None, None),
        "GONE": None,
        None: None,
    }
    plans = {}
    for coupon in coupons:
        plans[coupon] = plan({}, price=True)
        offer(migrate_engine, people.a, plans[coupon], coupon)
        fake_stripe.prices[price_of(migrate_engine, plans[coupon])] = info("prod_a")
    fake_stripe.coupons.update({k: v for k, v in coupons.items() if k and v})

    fields = ("percent_off", "amount_off", "coupon_duration", "coupon_duration_in_months")

    def discounts() -> dict[str | None, tuple[Any, ...]]:
        body = owner.get("/api/entitlements").json()
        by_id = {o["id"]: (*(o[f] for f in fields), o["coupon_valid"]) for o in body["offers"]}
        return {coupon: by_id[str(plan_id)] for coupon, plan_id in plans.items()}

    nothing = (None, None, None, None)
    assert discounts() == {
        "HALF": (12.5, None, "forever", None, True),
        "FIVE": (None, 500, "once", None, True),
        "THREE": (10.0, None, "repeating", 3, True),
        "DOLLARS": (None, None, "once", None, True),
        "SPENT": (*nothing, False),
        "GONE": (*nothing, False),
        None: (*nothing, None),
    }
    calls = fake_stripe.count("retrieve_coupon")
    assert calls == 6  # each coupon once
    assert discounts()["HALF"] == (12.5, None, "forever", None, True)
    assert fake_stripe.count("retrieve_coupon") == calls  # cached

    entitlements._coupons.clear()
    fake_stripe.fail_next("retrieve_coupon", times=6)
    assert set(discounts().values()) == {(*nothing, None)}


# ZIF-79 fence. Kills: one Stripe call per plan on every cold console load (one listing serves
# them all); an inactive or one-off Price shown as a plan's price.
def test_prices_come_from_one_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_STRIPE_SECRET_KEY", "sk_test_not_a_real_key")
    listed = {
        "object": "list",
        "url": "/v1/prices",
        "has_more": False,
        "data": [
            {
                "id": price_id,
                "object": "price",
                "product": "prod_a",
                "unit_amount": 2500,
                "currency": "eur",
                "recurring": {"interval": "month", "interval_count": 1},
            }
            for price_id in ("price_1", "price_other")
        ],
    }
    http = Recorder(body=listed)

    got = Gateway(http_client=http).list_prices(["price_1", "price_gone"])

    assert got == {"price_1": info("prod_a")}
    assert len(http.sent) == 1
    wire = http.wire(0)
    assert wire.startswith("get https://api.stripe.com/v1/prices?")
    assert "active=true" in wire and "type=recurring" in wire and "limit=100" in wire


# ZIF-79 fence. Kills: every request refreshing at once while Stripe is slow (a stampede): one
# refreshes, the others answer from what is kept, nothing on a cold cache; a known price lost
# when a refresh fails.
def test_one_price_refresh_at_a_time(
    fake_stripe: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entitlements, "_prices", {})
    fake_stripe.prices["price_1"] = info("prod_a")
    meanwhile: list[dict[str, PriceInfo]] = []

    def during(op: str) -> None:
        if op == "list_prices":
            meanwhile.append(entitlements.prices(["price_1"]))

    fake_stripe.on_call = during
    assert entitlements.prices(["price_1"]) == {"price_1": info("prod_a")}
    assert meanwhile == [{}]  # cold while the first refresh runs

    entitlements._prices["price_1"] = (0.0, info("prod_a"))  # stale
    assert entitlements.prices(["price_1"]) == {"price_1": info("prod_a")}
    assert meanwhile[1] == {"price_1": info("prod_a")}  # the stale one meanwhile
    entitlements._prices["price_1"] = (0.0, info("prod_a"))
    fake_stripe.fail_next("list_prices")
    assert entitlements.prices(["price_1"]) == {"price_1": info("prod_a")}
    assert fake_stripe.count("list_prices") == 3


# ZIF-79 fence (row-level security pin). Kills: checkout or the portal reading another business's
# subscription: its customer reused, a refusal or a portal session for the wrong business.
def test_another_business_subscription_is_never_used(
    people: People, owner: TestClient, fake_stripe: FakeGateway, plan: Plan, migrate_engine: Engine
) -> None:
    paid = plan({}, public=True, price=True)
    write(
        migrate_engine,
        people.b,
        """INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id,
             plan_id, status) VALUES (:t, 'cus_theirs', :sub, :p, 'active')""",
        sub=f"sub_{uuid.uuid4().hex}",
        p=paid,
    )

    assert checkout(owner, paid).status_code == 200
    portal = owner.post("/api/billing/portal", headers=JSON)

    assert (portal.status_code, portal.json()) == (409, {"code": "no_subscription"})
    assert [s["customer"] for s in sent(fake_stripe, "create_checkout")] == [None]
    assert sent(fake_stripe, "create_portal") == []
