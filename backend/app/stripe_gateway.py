"""The only module that imports `stripe` (ZIF-158). Everything else asks the module-level `gateway`
and gets small frozen dataclasses back, never Stripe objects; tests swap `gateway` for
tests/stripe_fake.py. Nothing here logs a body, a link, a secret or a client_secret."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypeVar

import stripe
from opentelemetry.trace import SpanKind
from stripe import RequestOptions
from stripe.params import PaymentIntentCreateParams, RefundCreateParams
from stripe.params.v2.core import AccountCreateParams

from app import tracing
from app.config import StripeSettings

# The account API with `responsibilities` (fee payer) on v2 Accounts: the one this SDK release
# (stripe 16.0.0) is generated for.
PINNED = "2026-09-30.endive"
TIMEOUT_SECONDS = 10
TOLERANCE_SECONDS = 300
logger = logging.getLogger(__name__)
T = TypeVar("T")
Kind = Literal["card", "invalid", "unavailable"]
FeesCollector = Literal["stripe", "application"]
Mode = Literal["authorisation", "setup"]
# What Stripe answers to cancelling an intent that is already canceled or past cancelling.
UNEXPECTED_STATE = {"payment_intent_unexpected_state", "setup_intent_unexpected_state"}


class Failed(Exception):
    """A Stripe call that did not work. `kind` tells the caller what to do about it; the message is
    only ever the kind and Stripe's request id."""

    def __init__(self, kind: Kind, request_id: str | None) -> None:
        super().__init__(f"{kind} {request_id}")
        self.kind = kind
        self.request_id = request_id


class BadSignature(Exception):
    """The webhook body is not what Stripe signed (or is not an event at all)."""


@dataclass(frozen=True)
class Account:
    id: str
    charges_enabled: bool


@dataclass(frozen=True)
class Intent:
    """A payment check just created. The client_secret goes to the browser and nowhere else."""

    id: str
    client_secret: str = field(repr=False)
    type: Literal["payment", "setup"]


@dataclass(frozen=True)
class IntentState:
    """What Stripe says about an intent now. A SetupIntent has no amount or currency."""

    id: str
    type: Literal["payment", "setup"]
    status: str
    amount: int | None
    currency: str | None
    amount_refunded: int
    failed: bool  # the last attempt was declined or abandoned at the bank
    client_secret: str = field(repr=False)  # Stripe hands it back to the secret key
    # Stripe's processing fee on the charge (not the application fee), None until its balance
    # transaction exists (an authorisation has none).
    stripe_fee: int | None = None


@dataclass(frozen=True)
class RefundState:
    id: str
    status: str  # succeeded, pending, failed, canceled, requires_action
    payment_intent: str | None
    amount: int


@dataclass(frozen=True)
class Event:
    id: str
    type: str
    account: str | None
    object_id: str | None


class Gateway:
    def __init__(self, http_client: stripe.HTTPClient | None = None) -> None:
        self._http_client = http_client
        self._stripe: stripe.StripeClient | None = None

    def _client(self) -> stripe.StripeClient:
        if self._stripe is None:
            key = StripeSettings().stripe_secret_key.get_secret_value()
            if not key:
                raise Failed("unavailable", None)
            self._stripe = stripe.StripeClient(
                key,
                stripe_version=PINNED,
                max_network_retries=1,
                http_client=self._http_client or stripe.RequestsClient(timeout=TIMEOUT_SECONDS),
            )
        return self._stripe

    def _call(
        self, op: str, call: Callable[[stripe.StripeClient], T], account: str | None = None
    ) -> T:
        with tracing.span(f"stripe.{op}", SpanKind.CLIENT, {"stripe.op": op}):
            try:
                result = call(self._client())
            except Failed:
                raise
            except stripe.StripeError as error:
                request_id = getattr(error, "request_id", None)
                kind: Kind
                if isinstance(error, stripe.CardError):
                    kind = "card"
                elif isinstance(error, stripe.InvalidRequestError | stripe.IdempotencyError):
                    kind = "invalid"
                else:
                    kind = "unavailable"
                logger.warning(
                    "stripe call",
                    extra={
                        "op": op,
                        "account": account,
                        "object_id": None,
                        "outcome": kind,
                        "stripe_request_id": request_id,
                    },
                )
                raise Failed(kind, request_id) from None
            logger.info(
                "stripe call",
                extra={
                    "op": op,
                    "account": account,
                    "object_id": getattr(result, "id", None),
                    "outcome": "ok",
                    "stripe_request_id": None,
                },
            )
            return result

    def create_account(
        self,
        *,
        key: str,
        tenant_id: str,
        country: str,
        currency: str,
        fees_collector: FeesCollector,
    ) -> Account:
        # Stable tenant data only: Stripe rejects a reused idempotency key with other params, and
        # onboarding collects the name and email itself.
        def call(client: stripe.StripeClient) -> Account:
            params: AccountCreateParams = {
                "dashboard": "express",
                "identity": {"country": country},
                "defaults": {
                    "responsibilities": {
                        "fees_collector": fees_collector,
                        "losses_collector": "stripe",
                    },
                    "currency": currency.lower(),
                },
                "metadata": {"tenant_id": tenant_id},
                "configuration": {
                    "merchant": {
                        "capabilities": {
                            "card_payments": {"requested": True},
                            "ideal_payments": {"requested": True},
                            "bancontact_payments": {"requested": True},
                        }
                    }
                },
                "include": ["configuration.merchant"],
            }
            return _account(client.v2.core.accounts.create(params, {"idempotency_key": key}))

        return self._call("create_account", call)

    def retrieve_account(self, account: str) -> Account:
        return self._call(
            "retrieve_account",
            lambda client: _account(
                client.v2.core.accounts.retrieve(account, {"include": ["configuration.merchant"]})
            ),
            account,
        )

    def onboarding_link(self, account: str, return_url: str, refresh_url: str) -> str:
        return self._call(
            "onboarding_link",
            lambda client: (
                client.v2.core.account_links.create(
                    {
                        "account": account,
                        "use_case": {
                            "type": "account_onboarding",
                            "account_onboarding": {
                                "return_url": return_url,
                                "refresh_url": refresh_url,
                            },
                        },
                    }
                ).url
            ),
            account,
        )

    def login_link(self, account: str) -> str:
        return self._call(
            "login_link",
            lambda client: client.v1.accounts.login_links.create(account).url,
            account,
        )

    def register_domain(self, account: str, host: str) -> None:
        """The Apple Pay domain, on the connected account (direct charges show it nowhere else)."""

        def call(client: stripe.StripeClient) -> None:
            try:
                client.v1.payment_method_domains.create(
                    {"domain_name": host}, {"stripe_account": account}
                )
            except stripe.InvalidRequestError as error:
                if getattr(error, "code", None) != "resource_already_exists":
                    raise

        self._call("register_domain", call, account)

    def create_check(
        self,
        account: str,
        *,
        mode: Mode,
        tenant_id: str,
        booking_id: str,
        amount: int,
        currency: str,
        idempotency_key: str,
    ) -> Intent:
        """The funds check on the business's own account (a direct charge), the only place that
        picks how: a manual-capture authorisation for the price, or a SetupIntent that validates
        the card. Neither takes an application fee (nothing is captured) or keeps a card."""
        metadata = {"booking_id": booking_id, "tenant_id": tenant_id}
        options: RequestOptions = {"stripe_account": account, "idempotency_key": idempotency_key}

        def call(client: stripe.StripeClient) -> Intent:
            if mode == "setup":
                setup = client.v1.setup_intents.create(
                    {
                        "allowed_payment_method_types": ["card"],
                        "usage": "on_session",
                        "metadata": metadata,
                    },
                    options,
                )
                return Intent(setup.id, setup.client_secret or "", "setup")
            payment = client.v1.payment_intents.create(
                {
                    "amount": amount,
                    "currency": currency.lower(),
                    "capture_method": "manual",
                    "allowed_payment_method_types": ["card"],
                    "metadata": metadata,
                },
                options,
            )
            return Intent(payment.id, payment.client_secret or "", "payment")

        return self._call("create_check", call, account)

    def create_payment(
        self,
        account: str,
        *,
        tenant_id: str,
        booking_id: str,
        amount: int,
        currency: str,
        application_fee: int,
        idempotency_key: str,
    ) -> Intent:
        """The client's payment in advance, a direct charge on the business's own account. The
        methods offered are the account's payment method configuration's, not a list here. The
        application fee (D6, mode 2 only) is sent when there is one."""
        metadata = {"booking_id": booking_id, "tenant_id": tenant_id}
        options: RequestOptions = {"stripe_account": account, "idempotency_key": idempotency_key}

        def call(client: stripe.StripeClient) -> Intent:
            params: PaymentIntentCreateParams = {
                "amount": amount,
                "currency": currency.lower(),
                "capture_method": "automatic",
                "automatic_payment_methods": {"enabled": True},
                "metadata": metadata,
            }
            if application_fee > 0:
                params["application_fee_amount"] = application_fee
            payment = client.v1.payment_intents.create(params, options)
            return Intent(payment.id, payment.client_secret or "", "payment")

        return self._call("create_payment", call, account)

    def refund(
        self,
        account: str,
        intent_id: str,
        amount: int,
        *,
        refund_application_fee: bool | None,
        idempotency_key: str,
    ) -> str:
        """Send `amount` of the payment back; the refund's id. refund_application_fee is sent only
        when there is an application fee to decide about (None: nothing is sent)."""
        options: RequestOptions = {"stripe_account": account, "idempotency_key": idempotency_key}

        def call(client: stripe.StripeClient) -> str:
            params: RefundCreateParams = {"payment_intent": intent_id, "amount": amount}
            if refund_application_fee is not None:
                params["refund_application_fee"] = refund_application_fee
            return client.v1.refunds.create(params, options).id

        return self._call("refund", call, account)

    def retrieve_refund(self, account: str, refund_id: str) -> RefundState:
        options: RequestOptions = {"stripe_account": account}

        def call(client: stripe.StripeClient) -> RefundState:
            got = client.v1.refunds.retrieve(refund_id, {}, options)
            made = got.payment_intent
            return RefundState(
                got.id,
                got.status or "",
                made if isinstance(made, str) or made is None else made.id,
                got.amount,
            )

        return self._call("retrieve_refund", call, account)

    def retrieve_intent(self, account: str, intent_id: str) -> IntentState:
        options: RequestOptions = {"stripe_account": account}

        def call(client: stripe.StripeClient) -> IntentState:
            if intent_id.startswith("seti_"):
                setup = client.v1.setup_intents.retrieve(intent_id, {}, options)
                return IntentState(
                    setup.id,
                    "setup",
                    setup.status,
                    None,
                    None,
                    0,
                    setup.last_setup_error is not None,
                    setup.client_secret or "",
                )
            payment = client.v1.payment_intents.retrieve(
                intent_id, {"expand": ["latest_charge.balance_transaction"]}, options
            )
            charge = payment.latest_charge
            if charge is None or isinstance(charge, str):
                refunded, fee = 0, None
            else:
                refunded, fee = charge.amount_refunded, _processing_fee(charge)
            return IntentState(
                payment.id,
                "payment",
                payment.status,
                payment.amount,
                payment.currency,
                refunded,
                payment.last_payment_error is not None,
                payment.client_secret or "",
                fee,
            )

        return self._call("retrieve_intent", call, account)

    def cancel_intent(self, account: str, intent_id: str) -> str:
        """Cancel the intent; the status it has afterwards. "canceled" also when it already was.
        Anything else is Stripe refusing: the intent is past cancelling (processing, succeeded)."""
        options: RequestOptions = {"stripe_account": account}

        def call(client: stripe.StripeClient) -> str:
            setup = intent_id.startswith("seti_")
            try:
                if setup:
                    return client.v1.setup_intents.cancel(
                        intent_id, {"cancellation_reason": "abandoned"}, options
                    ).status
                return client.v1.payment_intents.cancel(
                    intent_id, {"cancellation_reason": "abandoned"}, options
                ).status
            except stripe.InvalidRequestError as error:
                if getattr(error, "code", None) not in UNEXPECTED_STATE:
                    raise
            if setup:
                return client.v1.setup_intents.retrieve(intent_id, {}, options).status
            return client.v1.payment_intents.retrieve(intent_id, {}, options).status

        return self._call("cancel_intent", call, account)

    def construct_event(self, body: bytes, header: str | None) -> Event:
        """The event, once Stripe's signature over these exact bytes checks out."""
        secret = StripeSettings().stripe_webhook_secret.get_secret_value()
        try:
            stripe.WebhookSignature.verify_header(body, header, secret, TOLERANCE_SECONDS)
            event = json.loads(body)
            made = Event(
                id=event["id"],
                type=event["type"],
                account=event.get("account"),
                object_id=event["data"]["object"].get("id"),
            )
        except stripe.SignatureVerificationError, ValueError, KeyError, TypeError, AttributeError:
            raise BadSignature from None
        # Signed, but the fields must still be what the handlers assume.
        if not (
            isinstance(made.id, str)
            and isinstance(made.type, str)
            and isinstance(made.account, str | None)
            and isinstance(made.object_id, str | None)
        ):
            raise BadSignature
        return made


def _processing_fee(charge: stripe.Charge) -> int | None:
    """Stripe's own fee on the charge (never the application fee), None until it has a balance
    transaction."""
    transaction = charge.balance_transaction
    if transaction is None or isinstance(transaction, str):
        return None
    return sum(d.amount for d in transaction.fee_details if d.type == "stripe_fee")


def _account(made: stripe.v2.core.Account) -> Account:
    merchant = made.configuration.merchant if made.configuration else None
    card = merchant.capabilities.card_payments if merchant and merchant.capabilities else None
    return Account(made.id, card is not None and card.status == "active")


gateway = Gateway()
