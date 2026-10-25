"""ZIF-102: the merchant chooses the business's currency, and the database holds it still once a
booking or a Stripe account depends on it (owner's ruling 1, 2026-10-25)."""

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.errors import CheckViolation, FeatureNotSupported, ForeignKeyViolation
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import tenant_context
from app.main import create_app
from tests.conftest import People, events, member_id, signed_in, wait_until_blocked
from tests.stripe_fake import FakeGateway
from tests.test_bookings_db import book, insert_booking, seed_service
from tests.test_clients_db import seed_client
from tests.test_names import set_user_name
from tests.test_payments_db import insert as insert_account


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


OPTIONS = [{"code": c, "minor_units": 2} for c in ("BRL", "EUR", "GBP", "USD")]


def prices(tenant_id: uuid.UUID) -> list[tuple[int, str]]:
    with tenant_context(tenant_id) as session:
        return [
            (r.price_amount_minor, r.price_currency)
            for r in session.execute(
                text("SELECT price_amount_minor, price_currency FROM services ORDER BY id")
            )
        ]


def archived_and_live_services(tenant_id: uuid.UUID) -> None:
    archived = seed_service(tenant_id)
    seed_service(tenant_id)
    with tenant_context(tenant_id) as session:
        session.execute(
            text("UPDATE services SET archived_at = now() WHERE id = :s"), {"s": archived}
        )


def a_booking(people: People, status: str = "cancelled_by_client") -> None:
    starts = datetime.now(UTC) + timedelta(days=3)
    book(
        people.a,
        client_id=seed_client(people.a, email=f"{uuid.uuid4()}@example.com"),
        worker_id=member_id(people.a, people.both),
        service_id=seed_service(people.a),
        starts_at=starts,
        ends_at=starts + timedelta(minutes=30),
        status=status,
    )


# F1: FENCE. Wrong impl: no route; the currency fixed at sign-up as before.
def test_the_owner_changes_the_currency_of_a_business_with_no_prices(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    response = owner.put("/api/settings/currency", json={"currency": "USD"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "currency": "USD",
        "minor_units": 2,
        "options": OPTIONS,
        "change": "open",
        "locked_by": [],
        "prices": 0,
    }
    assert owner.get("/api/session").json()["currency"] == "USD"
    assert currency_of(migrate_engine, people.b) == "EUR"  # the other business is untouched


# F2: FENCE. Wrong impl: relabelling saved prices without the owner's confirmation.
def test_saved_prices_need_a_confirmation(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    archived_and_live_services(people.a)

    response = owner.put("/api/settings/currency", json={"currency": "USD"})

    assert (response.status_code, response.json()) == (
        409,
        {"code": "currency_prices_exist", "total": 2},
    )
    assert currency_of(migrate_engine, people.a) == "EUR"
    assert prices(people.a) == [(2500, "EUR"), (2500, "EUR")]


# F3: FENCE. Wrong impl: a yes/no confirmation, which relabels a price added after the owner was
# told the count.
def test_a_confirmation_of_another_count_is_refused(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    archived_and_live_services(people.a)

    response = owner.put("/api/settings/currency", json={"currency": "USD", "confirmed_prices": 1})

    assert (response.status_code, response.json()) == (
        409,
        {"code": "currency_prices_exist", "total": 2},
    )
    assert currency_of(migrate_engine, people.a) == "EUR"


# F4: FENCE. Wrong impl: converting or rounding the amounts. A saved price keeps its number and
# takes the new currency, archived ones too.
def test_confirmed_prices_keep_their_numbers(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    archived_and_live_services(people.a)

    response = owner.put("/api/settings/currency", json={"currency": "GBP", "confirmed_prices": 2})

    assert response.status_code == 200
    assert response.json()["prices"] == 2
    assert currency_of(migrate_engine, people.a) == "GBP"
    assert prices(people.a) == [(2500, "GBP"), (2500, "GBP")]


# F5: FENCE. Wrong impl: counting only live bookings. Any booking is evidence in the currency it was
# made in, a cancelled one too.
def test_any_booking_locks_the_currency(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    a_booking(people, status="cancelled_by_client")

    response = owner.put("/api/settings/currency", json={"currency": "USD", "confirmed_prices": 1})

    assert (response.status_code, response.json()) == (409, {"code": "currency_locked_bookings"})
    assert currency_of(migrate_engine, people.a) == "EUR"
    assert prices(people.a) == [(2500, "EUR")]
    state = owner.get("/api/settings/currency").json()
    assert (state["change"], state["locked_by"]) == ("locked", ["bookings"])


# F6: FENCE. Wrong impl: a migration that also cascades the bookings foreign key: a booking would be
# rewritten into a currency it was never sold in.
def test_bookings_hold_the_currency_in_the_database(people: People, migrate_engine: Engine) -> None:
    a_booking(people)

    with pytest.raises(IntegrityError) as error:
        set_currency(migrate_engine, people.a, "USD")

    assert isinstance(error.value.orig, ForeignKeyViolation)
    assert error.value.orig.diag.constraint_name == "fk_bookings_tenant_id_price_currency_tenants"


# F7: FENCE. Wrong impl: checking charges_enabled (a ready account) instead of any account: one
# still onboarding was made in the business's currency too. The row here has no currency, as one the
# previous release inserts during the rollout would: the function's check covers what the key
# cannot.
def test_a_stripe_account_locks_the_currency(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    insert_account(migrate_engine, people.a, "acct_onboarding1")

    response = owner.put("/api/settings/currency", json={"currency": "USD"})

    assert (response.status_code, response.json()) == (409, {"code": "currency_locked_payments"})
    assert currency_of(migrate_engine, people.a) == "EUR"
    state = owner.get("/api/settings/currency").json()
    assert (state["change"], state["locked_by"]) == ("locked", ["payment_account"])


# F10: FENCE. Wrong impl: the route on CurrentSession: a worker could change how the business is
# paid. The name is asked first, as everywhere.
def test_only_an_owner_changes_it(people: People, app: FastAPI, migrate_engine: Engine) -> None:
    worker = signed_in(app, people.a, people.only_a)
    refused = worker.put("/api/settings/currency", json={"currency": "USD"})
    assert (refused.status_code, refused.json()) == (403, {"code": "owner_only"})

    set_user_name(migrate_engine, people.both, None)
    nameless = signed_in(app, people.a, people.both).put(
        "/api/settings/currency", json={"currency": "USD"}
    )
    assert (nameless.status_code, nameless.json()) == (403, {"code": "name_required"})
    assert currency_of(migrate_engine, people.a) == "EUR"


# F11: FENCE. Wrong impl: a format-only check (three capitals), which takes JPY or XXX.
@pytest.mark.parametrize(
    "body",
    [
        {"currency": "JPY"},
        {"currency": "eur"},
        {"currency": "XXX"},
        {"currency": "USD", "confirmed_prices": True},
        {"currency": "USD", "keep_prices": True},
    ],
    ids=["unsupported", "lowercase", "unknown", "bool", "extra"],
)
def test_only_the_four_currencies(
    people: People, owner: TestClient, migrate_engine: Engine, body: dict[str, Any]
) -> None:
    assert owner.put("/api/settings/currency", json=body).status_code == 422
    assert currency_of(migrate_engine, people.a) == "EUR"


def change(tenant_id: uuid.UUID, currency: str, confirmed: int | None = None) -> tuple[str, int]:
    with tenant_context(tenant_id) as session:
        row = session.execute(
            text("SELECT outcome, prices FROM change_currency(:c, :n)"),
            {"c": currency, "n": confirmed},
        ).one()
    return row.outcome, row.prices


# F12: FENCE. Wrong impl: the list kept only in Python: any caller of the function (the app role,
# the worker, an operator) could set a currency the app cannot format.
def test_the_database_refuses_a_currency_outside_the_four(
    people: People, migrate_engine: Engine
) -> None:
    with pytest.raises(IntegrityError) as error:
        change(people.a, "JPY")
    assert isinstance(error.value.orig, CheckViolation)
    assert error.value.orig.diag.constraint_name == "ck_tenants_currency_supported"
    with pytest.raises(IntegrityError):
        set_currency(migrate_engine, people.a, "CHF")
    assert currency_of(migrate_engine, people.a) == "EUR"


# F13: FENCE. Wrong impl: GRANT UPDATE (currency) ON tenants: tenants has no row-level security, so
# the app role could change any business's currency. change_currency changes only the caller's.
def test_change_currency_changes_only_the_caller_s_business(
    people: People, migrate_engine: Engine
) -> None:
    assert change(people.a, "USD") == ("changed", 0)
    assert (currency_of(migrate_engine, people.a), currency_of(migrate_engine, people.b)) == (
        "USD",
        "EUR",
    )
    with migrate_engine.connect() as conn:
        assert (
            conn.scalar(
                text("SELECT has_column_privilege('ziftbook_app', 'tenants', 'currency', 'UPDATE')")
            )
            is False
        )


# F14: FENCE. Wrong impl: no isolation guard. Under REPEATABLE READ the checks would read a snapshot
# taken before the lock wait and miss a booking committed meanwhile.
def test_change_currency_needs_read_committed(people: People, app_engine: Engine) -> None:
    with (
        pytest.raises(DBAPIError) as error,
        app_engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
    ):
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(text("SELECT * FROM change_currency('USD', NULL)"))
    assert isinstance(error.value.orig, FeatureNotSupported)


# F15: FENCE. Wrong impl: no audit, or an event for a change that changed nothing.
def test_the_change_is_in_the_settings_history(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    assert owner.put("/api/settings/currency", json={"currency": "EUR"}).status_code == 200
    assert owner.put("/api/settings/currency", json={"currency": "BRL"}).status_code == 200

    changed = [
        (e["action"], e["target"], e["details"], e["actor_user_id"])
        for e in events(migrate_engine, tenant_id=people.a)
        if e["target"] == "setting:currency"
    ]
    assert changed == [
        ("setting_changed", "setting:currency", {"old": "EUR", "new": "BRL"}, people.both)
    ]


# F16: FENCE. Wrong impl: the checks before the lock. A booking whose insert is under way (its
# foreign key check holds the business's row) is waited for, then seen: the change answers
# locked_bookings instead of failing on the bookings foreign key or slipping past it.
def test_a_booking_committed_while_the_change_waits_locks_it(
    people: People, migrate_engine: Engine
) -> None:
    service_id = seed_service(people.a)
    client_id = seed_client(people.a, email=f"{uuid.uuid4()}@example.com")
    starts = datetime.now(UTC) + timedelta(days=3)
    outcome: list[object] = []

    def run_change() -> None:
        try:
            outcome.append(change(people.a, "USD", confirmed=1))
        except Exception as error:  # the wrong implementation fails on the foreign key
            outcome.append(error)

    with tenant_context(people.a) as session:
        insert_booking(
            session,
            client_id=client_id,
            worker_id=member_id(people.a, people.both),
            service_id=service_id,
            starts_at=starts,
            ends_at=starts + timedelta(minutes=30),
        )
        racer = threading.Thread(target=run_change)
        racer.start()
        wait_until_blocked(migrate_engine, 1)  # the change waits on the business's row
    racer.join(timeout=10)

    assert outcome == [("locked_bookings", 0)]
    assert currency_of(migrate_engine, people.a) == "EUR"


# F17: FENCE. Wrong impl: an owner-only read: the settings screen is readable by every member.
def test_a_worker_reads_the_currency_state(people: People, app: FastAPI) -> None:
    archived_and_live_services(people.a)

    response = signed_in(app, people.a, people.only_a).get("/api/settings/currency")

    assert response.status_code == 200
    assert response.json() == {
        "currency": "EUR",
        "minor_units": 2,
        "options": OPTIONS,
        "change": "open",
        "locked_by": [],
        "prices": 2,
    }


# F18: FENCE. Wrong impl: the three constraints left NOT VALID at head: rows older than them would
# never have been checked.
def test_the_currency_constraints_are_validated(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        validated: dict[str, bool] = dict(
            conn.execute(
                text("""
                SELECT conname, convalidated FROM pg_constraint
                WHERE conname IN ('ck_tenants_currency_supported',
                                  'fk_services_tenant_id_price_currency_tenants',
                                  'fk_payment_accounts_tenant_id_currency_tenants')
                """)
            ).all()
        )
    assert validated == {
        "ck_tenants_currency_supported": True,
        "fk_services_tenant_id_price_currency_tenants": True,
        "fk_payment_accounts_tenant_id_currency_tenants": True,
    }


def stored_elsewhere(
    migrate_engine: Engine, tenant_id: uuid.UUID, account: str, currency: str
) -> None:
    """Another request's account row, committed while this one waits on Stripe."""
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO payment_accounts (tenant_id, stripe_account_id, currency) "
                "VALUES (:t, :a, :c)"
            ),
            {"t": tenant_id, "a": account, "c": currency},
        )


# FENCE (code review: security 1, correctness 3). Wrong impl: a conflict read as "the same account
# twice". A connect that read EUR loses to one that, after a change to USD, stored its USD account
# first: its own EUR account is skipped by ON CONFLICT (no foreign key check runs), and it must hand
# the owner the onboarding link of the account the business holds, not of the one it made.
def test_a_stale_connect_onboards_the_account_the_business_holds(
    people: People, owner: TestClient, fake_stripe: FakeGateway, migrate_engine: Engine
) -> None:
    def other_request_wins(op: str) -> None:
        if op == "create_account":
            set_currency(migrate_engine, people.a, "USD")
            stored_elsewhere(migrate_engine, people.a, "acct_heldusd0001", "USD")

    fake_stripe.on_call = other_request_wins
    fake_stripe.accounts["acct_heldusd0001"] = {"charges_enabled": False, "domains": []}

    response = owner.post("/api/payments/account", json={"locale": "en"})

    assert response.status_code == 200
    assert accounts(migrate_engine, people.a) == [("acct_heldusd0001", "USD")]
    linked = [
        p["account"] for op, p in fake_stripe.calls if op in ("onboarding_link", "register_domain")
    ]
    assert linked == ["acct_heldusd0001", "acct_heldusd0001"]


# FENCE (code review, correctness 3). Wrong impl: the same. A double click split across the old
# release (key account:{t}) and this one (account:{t}:EUR) makes two Stripe accounts in one
# currency; the request that loses the insert must onboard the stored one, not its own orphan.
def test_a_split_double_click_onboards_the_account_the_business_holds(
    people: People, owner: TestClient, fake_stripe: FakeGateway, migrate_engine: Engine
) -> None:
    def old_release_wins(op: str) -> None:
        if op == "create_account":
            stored_elsewhere(migrate_engine, people.a, "acct_oldrelease01", "EUR")

    fake_stripe.on_call = old_release_wins
    fake_stripe.accounts["acct_oldrelease01"] = {"charges_enabled": False, "domains": []}

    response = owner.post("/api/payments/account", json={"locale": "en"})

    assert response.status_code == 200
    assert accounts(migrate_engine, people.a) == [("acct_oldrelease01", "EUR")]
    linked = [
        p["account"] for op, p in fake_stripe.calls if op in ("onboarding_link", "register_domain")
    ]
    assert linked == ["acct_oldrelease01", "acct_oldrelease01"]
