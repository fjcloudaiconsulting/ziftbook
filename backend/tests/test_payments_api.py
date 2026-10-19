"""ZIF-158 B1: the owner's routes that connect a business to Stripe."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import payments
from app.main import create_app
from tests.conftest import People, email_of, events, new_client, put_settings, signed_in
from tests.stripe_fake import FakeGateway, connect
from tests.test_payments_db import refused

Lines = Callable[[], list[dict[str, Any]]]
UNAVAILABLE = {"code": "payments_unavailable"}
JSON = {"content-type": "application/json"}


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


def rows(migrate_engine: Engine, tenant_id: uuid.UUID) -> list[Any]:
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        return list(
            conn.execute(
                text(
                    "SELECT stripe_account_id, charges_enabled, fee_mode, application_fee_bp, "
                    "application_fee_fixed_minor FROM payment_accounts"
                )
            )
        )


def test_the_account_state_walks_off_none_onboarding_ready(
    people: People, app: FastAPI, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    worker = signed_in(app, people.a, people.only_a)

    none = worker.get("/api/payments/account")
    assert (none.status_code, none.json()) == (
        200,
        {"state": "none", "country_supported": True, "min_amount_minor": 50, "currency": "EUR"},
    )
    assert none.headers["Cache-Control"] == "no-store"

    assert owner.post("/api/payments/account", json={"locale": "en"}).status_code == 200
    assert worker.get("/api/payments/account").json()["state"] == "onboarding"
    assert worker.post("/api/payments/account", json={"locale": "en"}).status_code == 403
    assert worker.post("/api/payments/dashboard", headers=JSON).status_code == 403


def test_the_links_and_the_audit_event(
    people: People, owner: TestClient, fake_stripe: FakeGateway, migrate_engine: Engine
) -> None:
    first = owner.post("/api/payments/account", json={"locale": "nl"})
    owner.post("/api/payments/account", json={"locale": "nl"})

    assert first.headers["Cache-Control"] == "no-store"
    assert first.status_code == 200 and first.json()["url"].startswith(
        "https://connect.stripe.test/"
    )
    (acct,) = (a for (a, *_) in rows(migrate_engine, people.a))
    link = [p for op, p in fake_stripe.calls if op == "onboarding_link"][0]
    assert link["return_url"] == "http://localhost:3000/nl/settings?stripe=return"
    assert link["refresh_url"] == "http://localhost:3000/nl/settings?stripe=refresh"
    created = [e for e in events(migrate_engine, tenant_id=people.a) if "payment" in e["action"]]
    assert [(e["action"], e["target"]) for e in created] == [
        ("payment_account_created", f"payment_account:{acct}")
    ]
    sent = [p for op, p in fake_stripe.calls if op == "create_account"][0]
    assert (sent["country"], sent["currency"], sent["email"], sent["fees_collector"]) == (
        "NL",
        "EUR",
        email_of(people.both),
        "stripe",
    )

    login = owner.post("/api/payments/dashboard", headers=JSON)
    assert login.json() == {"url": f"https://connect.stripe.test/express/{acct}"}
    assert login.headers["Cache-Control"] == "no-store"


def test_the_dashboard_needs_a_connected_account(
    owner: TestClient, fake_stripe: FakeGateway
) -> None:
    response = owner.post("/api/payments/dashboard", headers=JSON)

    assert (response.status_code, response.json()) == (409, {"code": "payments_not_connected"})


def test_a_stripe_error_on_connect_is_a_503(owner: TestClient, fake_stripe: FakeGateway) -> None:
    fake_stripe.fail_next("create_account")

    response = owner.post("/api/payments/account", json={"locale": "en"})

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)


# ZIF-158 F10. fence. Kills: ZIF-59's "not in Brazil" ignored.
def test_brazil_cannot_start_onboarding(
    people: People, owner: TestClient, fake_stripe: FakeGateway, migrate_engine: Engine
) -> None:
    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET country = 'BR', currency = 'BRL' WHERE id = :a"),
            {"a": people.a},
        )

    response = owner.post("/api/payments/account", json={"locale": "pt"})

    assert (response.status_code, response.json()) == (
        409,
        {"code": "payments_unavailable_country"},
    )
    assert fake_stripe.count("create_account") == 0
    state = owner.get("/api/payments/account").json()
    assert (state["country_supported"], state["min_amount_minor"]) == (False, None)


# ZIF-158 F11. fence. Kills: a second Stripe account per click (payouts split); the Apple Pay domain
# on the platform instead of the connected account (it never shows with direct charges).
def test_connecting_twice_keeps_one_account(
    people: People, owner: TestClient, fake_stripe: FakeGateway, migrate_engine: Engine
) -> None:
    one = owner.post("/api/payments/account", json={"locale": "en"})
    two = owner.post("/api/payments/account", json={"locale": "en"})

    assert (one.status_code, two.status_code) == (200, 200)
    ((acct, *_),) = rows(migrate_engine, people.a)
    assert list(fake_stripe.accounts) == [acct]
    links = [p["account"] for op, p in fake_stripe.calls if op == "onboarding_link"]
    assert links == [acct, acct]
    assert one.json()["url"] != two.json()["url"]
    domains = [(p["account"], p["host"]) for op, p in fake_stripe.calls if op == "register_domain"]
    assert domains and all(d == (acct, "localhost") for d in domains)
    keys = [p["key"] for op, p in fake_stripe.calls if op == "create_account"]
    assert set(keys) == {f"account:{people.a}"}


KEYS = ["ZIF_STRIPE_SECRET_KEY", "ZIF_STRIPE_WEBHOOK_SECRET", "ZIF_STRIPE_PUBLISHABLE_KEY"]


# ZIF-158 F12 (routes). fence. Kills: treating a missing publishable key as on (D1).
@pytest.mark.parametrize("name", KEYS)
def test_payments_routes_are_off_without_keys(
    name: str,
    people: People,
    app: FastAPI,
    fake_stripe: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(name)
    owner = signed_in(app, people.a, people.both)

    state = owner.get("/api/payments/account")
    connect_ = owner.post("/api/payments/account", json={"locale": "en"})
    dashboard = owner.post("/api/payments/dashboard", headers=JSON)

    assert state.json()["state"] == "off"
    assert (connect_.status_code, connect_.json()) == (503, UNAVAILABLE)
    assert (dashboard.status_code, dashboard.json()) == (503, UNAVAILABLE)
    assert fake_stripe.calls == []


# ZIF-158 F12 (startup). fence. Kills: no WARNING while any key is unset.
@pytest.mark.parametrize("name", KEYS)
def test_startup_warns_while_any_key_is_unset(
    name: str,
    app_engine: Engine,
    fake_stripe: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
    log_lines: Lines,
) -> None:
    monkeypatch.delenv(name)
    with new_client(create_app()) as client:
        assert client.get("/api/healthz").status_code == 200

    lines = log_lines()
    assert [line["payments"] for line in lines if line["msg"] == "api started"] == ["off"]
    assert [line["level"] for line in lines if line["msg"] == "stripe unset: payments off"] == [
        "WARNING"
    ]


def test_startup_with_all_three_keys_logs_no_warning(
    app_engine: Engine, fake_stripe: FakeGateway, log_lines: Lines
) -> None:
    with new_client(create_app()) as client:
        assert client.get("/api/healthz").status_code == 200

    lines = log_lines()
    assert [line["payments"] for line in lines if line["msg"] == "api started"] == ["on"]
    assert not [line for line in lines if line["msg"] == "stripe unset: payments off"]


# ZIF-158 F13. fence. Kills: trusting the row until a webhook arrives (the
# return_url page is wrong).
def test_the_account_page_reads_stripe_until_ready(
    people: People, owner: TestClient, fake_stripe: FakeGateway, migrate_engine: Engine
) -> None:
    acct = connect(fake_stripe, people.a)
    fake_stripe.accounts[acct]["charges_enabled"] = True

    assert owner.get("/api/payments/account").json()["state"] == "ready"
    assert rows(migrate_engine, people.a)[0].charges_enabled is True
    assert fake_stripe.count("retrieve_account") == 1

    assert owner.get("/api/payments/account").json()["state"] == "ready"
    assert fake_stripe.count("retrieve_account") == 1


def test_a_stripe_error_on_the_account_page_answers_from_the_row(
    people: People, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    connect(fake_stripe, people.a)
    fake_stripe.fail_next("retrieve_account")

    response = owner.get("/api/payments/account")

    assert (response.status_code, response.json()["state"]) == (200, "onboarding")


# ZIF-158 F17. fence. Kills: an owner setting the platform's fee on their own business.
def test_no_business_route_writes_the_fee_policy(
    people: People,
    owner: TestClient,
    fake_stripe: FakeGateway,
    migrate_engine: Engine,
    app_engine: Engine,
) -> None:
    for column in ("fee_mode", "application_fee_bp", "application_fee_fixed_minor"):
        sql = f"UPDATE payment_accounts SET {column} = {column}"
        assert refused(app_engine, people.a, sql)[0] == "42501"
    for body in ({"fee_mode": "platform_pays"}, {"application_fee_bp": 500}):
        assert put_settings(owner, body).status_code == 422
    owner.post("/api/payments/account", json={"locale": "en"})

    ((_, _, mode, bp, fixed),) = rows(migrate_engine, people.a)
    assert (mode, bp, fixed) == ("business_pays", 0, 0)


# ZIF-158 F16 (route). fence. Kills: connect() hard-coding the fee payer, or mapping
# platform_pays to "stripe": the mode picked at creation must reach Stripe and the row.
@pytest.mark.parametrize(
    ("mode", "collector"),
    [
        ("business_pays", "stripe"),
        ("business_pays_plus_fee", "stripe"),
        ("platform_pays", "application"),
    ],
)
def test_the_fee_mode_reaches_stripe_and_the_row(
    mode: str,
    collector: str,
    people: People,
    owner: TestClient,
    fake_stripe: FakeGateway,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(payments, "NEW_ACCOUNT_FEE_MODE", mode)

    assert owner.post("/api/payments/account", json={"locale": "en"}).status_code == 200

    sent = [p for op, p in fake_stripe.calls if op == "create_account"]
    assert [p["fees_collector"] for p in sent] == [collector]
    assert [r.fee_mode for r in rows(migrate_engine, people.a)] == [mode]


# ZIF-158 F22. fence. Kills: the account row written after the calls that can fail, or in a
# transaction they roll back (Stripe's events for the account would find no business).
@pytest.mark.parametrize("op", ["register_domain", "onboarding_link"])
def test_a_failure_after_the_account_is_made_keeps_its_row(
    op: str,
    people: People,
    owner: TestClient,
    fake_stripe: FakeGateway,
    migrate_engine: Engine,
) -> None:
    fake_stripe.fail_next(op)

    response = owner.post("/api/payments/account", json={"locale": "en"})

    assert response.status_code == (200 if op == "register_domain" else 503)
    assert [r.stripe_account_id for r in rows(migrate_engine, people.a)] == list(
        fake_stripe.accounts
    )


# ZIF-158 F23. fence. Kills: a Stripe call per request however many come (Stripe's own limit hit by
# one business polling), or a 429 on the read.
def test_reads_stop_calling_stripe_past_the_limit(
    people: People, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    connect(fake_stripe, people.a)

    states = [owner.get("/api/payments/account") for _ in range(payments.STRIPE_LIMIT + 1)]

    assert {(r.status_code, r.json()["state"]) for r in states} == {(200, "onboarding")}
    assert fake_stripe.count("retrieve_account") == payments.STRIPE_LIMIT


# ZIF-158 F23. fence. Kills: unlimited Stripe-calling POSTs.
def test_the_stripe_posts_answer_429_past_the_limit(
    people: People,
    owner: TestClient,
    fake_stripe: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect(fake_stripe, people.a)
    monkeypatch.setattr(payments, "STRIPE_LIMIT", 2)

    first = owner.post("/api/payments/account", json={"locale": "en"})
    second = owner.post("/api/payments/dashboard", headers=JSON)
    third = owner.post("/api/payments/account", json={"locale": "en"})
    fourth = owner.post("/api/payments/dashboard", headers=JSON)

    assert [r.status_code for r in (first, second, third, fourth)] == [200, 200, 429, 429]
    assert fourth.json() == {"code": "rate_limited"}
    assert fake_stripe.count("onboarding_link") == 1 and fake_stripe.count("login_link") == 1
