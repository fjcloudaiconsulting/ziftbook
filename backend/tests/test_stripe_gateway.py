"""ZIF-158 B1: the real Gateway against a recording HTTP client (no network)."""

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote

import pytest
import stripe

from app.stripe_gateway import Failed, FeesCollector, Gateway


class Recorder(stripe.HTTPClient):
    """Answers every Stripe call from `reply` and keeps what was sent."""

    name = "recorder"

    def __init__(self, status: int = 200, active: str = "active") -> None:
        super().__init__()
        self.sent: list[tuple[str, str, Any]] = []
        self.headers: list[Mapping[str, str]] = []
        self.status = status
        self.active = active

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        post_data: Any = None,
        *,
        _usage: list[str] | None = None,
    ) -> tuple[str, int, Mapping[str, str]]:
        self.sent.append((method, unquote(url), post_data))
        self.headers.append(dict(headers or {}))
        if self.status != 200:
            body: dict[str, Any] = {"error": {"type": "invalid_request_error", "message": "no"}}
        else:
            body = {
                "id": "acct_1RecordedAccount",
                "object": "v2.core.account",
                "configuration": {
                    "merchant": {"capabilities": {"card_payments": {"status": self.active}}}
                },
            }
        return json.dumps(body), self.status, {"request-id": "req_recorded"}

    def wire(self, index: int = -1) -> str:
        method, url, post_data = self.sent[index]
        return f"{method} {url} {post_data if isinstance(post_data, str) else ''}"


@pytest.fixture(autouse=True)
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_STRIPE_SECRET_KEY", "sk_test_not_a_real_key")


def create(gateway: Gateway, fees_collector: FeesCollector = "stripe") -> Any:
    return gateway.create_account(
        key="account:t",
        country="NL",
        currency="EUR",
        name="Salon",
        email="owner@example.com",
        fees_collector=fees_collector,
    )


# B1 fence 6. Wrong impl: a retrieve (or create) without include: v2 returns a null merchant
# configuration, so the account looks "not ready" forever.
def test_account_calls_ask_for_the_merchant_configuration_and_read_card_payments() -> None:
    http = Recorder()
    gateway = Gateway(http_client=http)

    created = create(gateway)
    retrieved = gateway.retrieve_account("acct_1RecordedAccount")

    assert "configuration.merchant" in http.wire(0)
    assert "configuration.merchant" in http.wire(1)
    assert (created.id, created.charges_enabled) == ("acct_1RecordedAccount", True)
    assert retrieved.charges_enabled is True
    pending = Gateway(http_client=Recorder(active="pending"))
    assert pending.retrieve_account("acct_1RecordedAccount").charges_enabled is False


# B1 fence 16. Wrong impl: a hard-coded fee payer, so mode 3 silently never applies.
@pytest.mark.parametrize("collector", ["stripe", "application"])
def test_the_fee_payer_sent_is_the_one_asked_for(collector: FeesCollector) -> None:
    http = Recorder()

    create(Gateway(http_client=http), collector)

    sent = json.loads(http.sent[0][2])
    assert sent["defaults"]["responsibilities"]["fees_collector"] == collector
    assert sent["defaults"]["responsibilities"]["losses_collector"] == "stripe"


def test_the_stripe_account_header_and_idempotency_key_are_sent() -> None:
    http = Recorder()
    gateway = Gateway(http_client=http)

    create(gateway)
    gateway.register_domain("acct_1RecordedAccount", "app.example.test")

    assert http.headers[0]["Idempotency-Key"] == "account:t"
    assert "payment_method_domains" in http.sent[-1][1]
    assert http.headers[-1]["Stripe-Account"] == "acct_1RecordedAccount"


def test_a_stripe_error_is_one_failure_with_a_request_id() -> None:
    gateway = Gateway(http_client=Recorder(status=400))

    with pytest.raises(Failed) as raised:
        gateway.retrieve_account("acct_1RecordedAccount")

    assert (raised.value.kind, raised.value.request_id) == ("invalid", "req_recorded")
