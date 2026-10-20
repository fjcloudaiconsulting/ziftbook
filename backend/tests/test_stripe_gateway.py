"""ZIF-158 B1: the real Gateway against a recording HTTP client (no network)."""

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote

import pytest
import stripe

from app.stripe_gateway import Failed, FeesCollector, Gateway, Mode

PAYMENT_INTENT = {
    "id": "pi_1Recorded",
    "object": "payment_intent",
    "client_secret": "pi_1Recorded_secret_x",
    "status": "requires_payment_method",
}
SETUP_INTENT = {
    "id": "seti_1Recorded",
    "object": "setup_intent",
    "client_secret": "seti_1Recorded_secret_x",
    "status": "requires_payment_method",
}


class Recorder(stripe.HTTPClient):
    """Answers every Stripe call from `reply` and keeps what was sent."""

    name = "recorder"

    def __init__(
        self, status: int = 200, active: str = "active", body: dict[str, Any] | None = None
    ) -> None:
        super().__init__()
        self.body = body
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
        elif self.body is not None:
            body = self.body
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
        tenant_id="t",
        fees_collector=fees_collector,
    )


# ZIF-158 F6. fence. Kills: a retrieve (or create) without include: v2 returns a null merchant
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


# ZIF-158 F16. fence. Kills: a hard-coded fee payer, so mode 3 silently never applies.
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


def check(gateway: Gateway, mode: Mode) -> Any:
    return gateway.create_check(
        "acct_1RecordedAccount",
        mode=mode,
        tenant_id="t",
        booking_id="b",
        amount=2500,
        currency="EUR",
        idempotency_key="pi:b:1",
    )


# ZIF-158 B2 16. fence. Kills: an automatic-capture "check" (charges the client); a platform-account
# intent (direct charges broken); a saved card.
def test_an_authorisation_holds_the_card_on_the_connected_account() -> None:
    http = Recorder(body=PAYMENT_INTENT)

    made = check(Gateway(http_client=http), "authorisation")

    wire = http.wire(0)
    assert (made.id, made.type, made.client_secret) == (
        "pi_1Recorded",
        "payment",
        "pi_1Recorded_secret_x",
    )
    assert wire.startswith("post https://api.stripe.com/v1/payment_intents ")
    assert "capture_method=manual" in wire
    assert "allowed_payment_method_types[0]=card" in wire
    assert "amount=2500" in wire and "currency=eur" in wire
    assert "customer" not in wire and "application_fee" not in wire
    assert http.headers[0]["Stripe-Account"] == "acct_1RecordedAccount"
    assert http.headers[0]["Idempotency-Key"].startswith("pi:")


def test_a_setup_check_saves_nothing() -> None:
    http = Recorder(body=SETUP_INTENT)

    made = check(Gateway(http_client=http), "setup")

    wire = http.wire(0)
    assert (made.id, made.type) == ("seti_1Recorded", "setup")
    assert wire.startswith("post https://api.stripe.com/v1/setup_intents ")
    assert "usage=on_session" in wire and "allowed_payment_method_types[0]=card" in wire
    assert "customer" not in wire
    assert http.headers[0]["Stripe-Account"] == "acct_1RecordedAccount"
    assert http.headers[0]["Idempotency-Key"].startswith("pi:")


def test_retrieving_and_cancelling_an_intent_name_the_connected_account() -> None:
    http = Recorder(
        body={
            **PAYMENT_INTENT,
            "status": "requires_capture",
            "amount": 2500,
            "currency": "eur",
            "last_payment_error": None,
            "latest_charge": None,
        }
    )
    gateway = Gateway(http_client=http)

    state = gateway.retrieve_intent("acct_1RecordedAccount", "pi_1Recorded")
    after = gateway.cancel_intent("acct_1RecordedAccount", "pi_1Recorded")

    assert (state.type, state.status, state.amount, state.currency, state.failed) == (
        "payment",
        "requires_capture",
        2500,
        "eur",
        False,
    )
    assert state.amount_refunded == 0
    assert "client_secret" not in repr(state)
    assert after == "requires_capture"  # the recorder answers whatever status it was given
    assert [h["Stripe-Account"] for h in http.headers] == ["acct_1RecordedAccount"] * 2
    assert "cancel" in http.sent[1][1]
