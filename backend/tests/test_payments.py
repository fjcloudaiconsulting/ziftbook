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
