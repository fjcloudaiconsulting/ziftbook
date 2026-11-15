"""ZIF-158 B1: when the payment step is on for a price."""

import pytest
from sqlalchemy import text

from app import payments
from app.business_settings import BusinessSettings
from app.db import tenant_context
from tests.conftest import People
from tests.stripe_fake import FakeGateway, connect


# ZIF-158 F14. fence. Kills: an offer() that returns an account for a price Stripe refuses, or for a
# currency it has no minimum for.
def test_offer_is_the_account_only_when_everything_allows_it(
    people: People, fake_stripe: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    on = BusinessSettings(payment_check=True)
    off = BusinessSettings(payment_check=False)

    def offer(settings: BusinessSettings, amount: int, currency: str = "EUR") -> str | None:
        with tenant_context(people.a) as db:
            return payments.offer(db, settings, amount, currency)

    assert offer(on, 5000) is None  # no account yet
    account = connect(fake_stripe, people.a, enabled=False)
    assert offer(on, 5000) is None  # not enabled
    with tenant_context(people.a) as db:
        db.execute(text("UPDATE payment_accounts SET charges_enabled = true"))

    assert offer(off, 5000) is None
    assert offer(on, 0) is None
    assert offer(on, 49) is None
    assert offer(on, 5000, "BRL") is None
    monkeypatch.delenv("ZIF_STRIPE_SECRET_KEY")
    assert offer(on, 5000) is None
    monkeypatch.setenv("ZIF_STRIPE_SECRET_KEY", "sk_test_not_a_real_key")

    assert offer(on, 50) == account


# ZIF-62 FENCE. Kills: a deposit rounded down or to even (a float round()), one under Stripe's
# minimum (Stripe refuses the charge), one over the price, or a deposit's share on another rule.
@pytest.mark.parametrize(
    ("rule", "pct", "price", "currency", "due"),
    [
        ("deposit", 30, 2500, "EUR", 750),
        ("deposit", 30, 1255, "EUR", 377),  # 376.5: half up
        ("deposit", 30, 1254, "EUR", 376),  # 376.2
        ("deposit", 50, 1001, "EUR", 501),  # 500.5
        ("deposit", 10, 100, "EUR", 50),  # 10, raised to the minimum
        ("deposit", 10, 100, "GBP", 30),
        ("deposit", 10, 40, "EUR", 40),  # the minimum is more than the price: the price
        ("deposit", 99, 2500, "EUR", 2475),
        ("choice", None, 2500, "EUR", 2500),
        ("prepaid", None, 2500, "EUR", 2500),
    ],
)
def test_what_a_payment_charges_under_each_rule(
    rule: str, pct: int | None, price: int, currency: str, due: int
) -> None:
    assert payments.due(rule, pct, price, currency) == due


# ZIF-62 FENCE. Kills: a step for an in_person service, for a business that takes no payments, or
# for a price under Stripe's minimum.
def test_rule_for_is_offer_s_conditions_and_a_rule_paid_online() -> None:
    assert payments.rule_for(None, "choice", None, 2500, "EUR") is None
    assert payments.rule_for("acct_1", "in_person", None, 2500, "EUR") is None
    assert payments.rule_for("acct_1", "deposit", 30, 49, "EUR") is None
    assert payments.rule_for("acct_1", "deposit", 30, 2500, "BRL") is None
    assert payments.rule_for("acct_1", "deposit", 30, 2500, "EUR") == ("acct_1", "deposit", 750)
    assert payments.rule_for("acct_1", "choice", None, 50, "EUR") == ("acct_1", "choice", 50)
