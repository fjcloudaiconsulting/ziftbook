"""ZIF-102: the merchant chooses the business's currency, and the database holds it still once a
booking or a Stripe account depends on it (owner's ruling 1, 2026-10-25)."""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.errors import ForeignKeyViolation
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app.main import create_app
from tests.conftest import People, signed_in
from tests.stripe_fake import FakeGateway


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


def set_currency(migrate_engine: Engine, tenant_id: uuid.UUID, currency: str) -> None:
    """What any role that owns tenants could run: the database is what must refuse it."""
    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET currency = :c WHERE id = :t"), {"c": currency, "t": tenant_id}
        )


def currency_of(migrate_engine: Engine, tenant_id: uuid.UUID) -> str:
    with migrate_engine.connect() as conn:
        found: str = conn.scalar(
            text("SELECT currency FROM tenants WHERE id = :t"), {"t": tenant_id}
        )
    return found


def accounts(migrate_engine: Engine, tenant_id: uuid.UUID) -> list[tuple[str, str | None]]:
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        return [
            (r.stripe_account_id, r.currency)
            for r in conn.execute(text("SELECT stripe_account_id, currency FROM payment_accounts"))
        ]


# F8: FENCE. Wrong impl: the Stripe lock checked only by the API or a function: any role that can
# update tenants could still move a connected business to another currency than its Stripe account.
def test_a_stripe_account_holds_the_currency_in_the_database(
    people: People, owner: TestClient, fake_stripe: FakeGateway, migrate_engine: Engine
) -> None:
    assert owner.post("/api/payments/account", json={"locale": "en"}).status_code == 200
    ((_, currency),) = accounts(migrate_engine, people.a)
    assert currency == "EUR"

    with pytest.raises(IntegrityError) as error:
        set_currency(migrate_engine, people.a, "USD")
    assert isinstance(error.value.orig, ForeignKeyViolation)
    assert currency_of(migrate_engine, people.a) == "EUR"


# F9: FENCE. Wrong impl: today's connect, which reads the currency, calls Stripe, then inserts the
# account with no currency: a change committed meanwhile leaves a USD business with an EUR account,
# and the idempotency key without the currency hands every retry that same EUR account.
def test_a_currency_changed_while_stripe_is_called_is_refused_and_a_retry_starts_afresh(
    people: People, owner: TestClient, fake_stripe: FakeGateway, migrate_engine: Engine
) -> None:
    def change_once(op: str) -> None:
        if op == "create_account" and fake_stripe.count("create_account") == 1:
            set_currency(migrate_engine, people.a, "USD")

    fake_stripe.on_call = change_once

    first = owner.post("/api/payments/account", json={"locale": "en"})

    assert (first.status_code, first.json()) == (409, {"code": "currency_changed"})
    assert accounts(migrate_engine, people.a) == []

    retry = owner.post("/api/payments/account", json={"locale": "en"})

    assert retry.status_code == 200
    ((account, currency),) = accounts(migrate_engine, people.a)
    assert currency == "USD"
    made = [p for op, p in fake_stripe.calls if op == "create_account"]
    assert [(p["key"], p["currency"]) for p in made] == [
        (f"account:{people.a}:EUR", "EUR"),
        (f"account:{people.a}:USD", "USD"),
    ]
    assert len(set(fake_stripe.accounts)) == 2 and account in fake_stripe.accounts
