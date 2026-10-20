"""The Stripe gateway for tests (ZIF-158): no network, ever.

FakeGateway is the real Gateway with its Stripe calls replaced by dicts. It keeps what Stripe would
(accounts), records every call, and honours idempotency keys the way Stripe does (same key, same
object). construct_event is inherited: webhook tests sign Stripe-shaped bodies with `signed` and
the real verification runs.
"""

import hashlib
import hmac
import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app import logs
from app.db import tenant_context
from app.stripe_gateway import (
    Account,
    Failed,
    FeesCollector,
    Gateway,
    Intent,
    IntentState,
    Kind,
    Mode,
)

WEBHOOK_SECRET = "whsec_test_secret_not_a_real_one"
# What Stripe lets a caller cancel (an intent already canceled counts: cancelling again is success).
CANCELABLE = {
    "requires_payment_method",
    "requires_confirmation",
    "requires_action",
    "requires_capture",
    "canceled",
}


class FakeGateway(Gateway):
    def __init__(self) -> None:
        super().__init__()
        self.accounts: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tenants: list[str | None] = []
        self.failures: dict[str, tuple[Kind, int]] = {}
        self._by_key: dict[str, str] = {}
        self._params: dict[str, dict[str, Any]] = {}
        # Open tenant_context sessions of the auth and payments code (conftest's fake_stripe
        # counts them), per thread: another thread may be in a transaction at that moment, which
        # says nothing about the thread making the Stripe call.
        self._sessions = threading.local()
        self.intents: dict[str, dict[str, Any]] = {}
        self._intent_by_key: dict[str, str] = {}
        # Runs at the top of every call, with the op's name (tests look at the world from there).
        self.on_call: Callable[[str], None] | None = None

    @property
    def open_sessions(self) -> int:
        return int(getattr(self._sessions, "count", 0))

    @open_sessions.setter
    def open_sessions(self, value: int) -> None:
        self._sessions.count = value

    def fail_next(self, op: str, kind: Kind = "unavailable", times: int = 1) -> None:
        self.failures[op] = (kind, times)

    def count(self, op: str) -> int:
        return sum(1 for name, _ in self.calls if name == op)

    def _enter(self, op: str, **params: Any) -> None:
        # No transaction (so no pool connection) is held across a Stripe call.
        assert self.open_sessions == 0, f"a transaction is open across stripe {op}"
        self.calls.append((op, params))
        # The tenant the gateway's own log line would carry (logs.bound in the calling context).
        self.tenants.append(logs.CONTEXT.get({}).get("tenant_id"))
        if self.on_call is not None:
            self.on_call(op)
        if op in self.failures:
            kind, times = self.failures.pop(op)
            if times > 1:
                self.failures[op] = (kind, times - 1)
            raise Failed(kind, "req_fake")

    def create_account(
        self,
        *,
        key: str,
        tenant_id: str,
        country: str,
        currency: str,
        fees_collector: FeesCollector,
    ) -> Account:
        params = {
            "tenant_id": tenant_id,
            "country": country,
            "currency": currency,
            "fees_collector": fees_collector,
        }
        self._enter("create_account", key=key, **params)
        # Stripe rejects a reused idempotency key whose params differ.
        if self._params.setdefault(key, params) != params:
            raise Failed("invalid", "req_fake")
        if key not in self._by_key:
            account_id = "acct_" + uuid.uuid4().hex[:16]
            self._by_key[key] = account_id
            self.accounts[account_id] = {"charges_enabled": False, "domains": []}
        account_id = self._by_key[key]
        return Account(account_id, self.accounts[account_id]["charges_enabled"])

    def retrieve_account(self, account: str) -> Account:
        self._enter("retrieve_account", account=account)
        return Account(account, self.accounts[account]["charges_enabled"])

    def onboarding_link(self, account: str, return_url: str, refresh_url: str) -> str:
        self._enter(
            "onboarding_link", account=account, return_url=return_url, refresh_url=refresh_url
        )
        return f"https://connect.stripe.test/setup/{account}/{uuid.uuid4().hex}"

    def login_link(self, account: str) -> str:
        self._enter("login_link", account=account)
        return f"https://connect.stripe.test/express/{account}"

    def register_domain(self, account: str, host: str) -> None:
        self._enter("register_domain", account=account, host=host)
        if host not in self.accounts[account]["domains"]:
            self.accounts[account]["domains"].append(host)

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
        self._enter(
            "create_check",
            account=account,
            mode=mode,
            tenant_id=tenant_id,
            booking_id=booking_id,
            amount=amount,
            currency=currency,
            key=idempotency_key,
        )
        if idempotency_key not in self._intent_by_key:
            payment = mode == "authorisation"
            intent_id = ("pi_" if payment else "seti_") + uuid.uuid4().hex[:20]
            self._intent_by_key[idempotency_key] = intent_id
            self.intents[intent_id] = {
                "type": "payment" if payment else "setup",
                "status": "requires_payment_method",
                "amount": amount if payment else None,
                "currency": currency if payment else None,
                "client_secret": f"{intent_id}_secret_{uuid.uuid4().hex}",
                "account": account,
                "booking_id": booking_id,
                "failed": False,
                "amount_refunded": 0,
            }
        intent_id = self._intent_by_key[idempotency_key]
        made = self.intents[intent_id]
        return Intent(intent_id, made["client_secret"], made["type"])

    def retrieve_intent(self, account: str, intent_id: str) -> IntentState:
        self._enter("retrieve_intent", account=account, intent_id=intent_id)
        got = self.intents[intent_id]
        return IntentState(
            intent_id,
            got["type"],
            got["status"],
            got["amount"],
            got["currency"],
            got["amount_refunded"],
            got["failed"],
            got["client_secret"],
        )

    def cancel_intent(self, account: str, intent_id: str) -> str:
        self._enter("cancel_intent", account=account, intent_id=intent_id)
        got = self.intents[intent_id]
        if got["status"] in CANCELABLE:
            got["status"] = "canceled"
        return str(got["status"])

    # What the browser and the bank do, for tests.
    def confirm(self, intent_id: str) -> None:
        got = self.intents[intent_id]
        got["status"] = "requires_capture" if got["type"] == "payment" else "succeeded"
        got["failed"] = False

    def decline(self, intent_id: str) -> None:
        self.intents[intent_id].update(status="requires_payment_method", failed=True)

    def process(self, intent_id: str) -> None:
        self.intents[intent_id]["status"] = "processing"

    def latest(self, booking_id: str) -> str:
        """The newest intent made for the booking."""
        return [i for i, got in self.intents.items() if got["booking_id"] == str(booking_id)][-1]


def signed(body: bytes, secret: str = WEBHOOK_SECRET, t: int | None = None) -> dict[str, str]:
    """The Stripe-Signature header for body, computed by hand (not by the code under test)."""
    stamp = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={stamp},v1={mac}", "content-type": "application/json"}


def event_body(
    type_: str = "account.updated",
    account: str | None = "acct_unknown",
    event_id: str | None = None,
    pad: int = 0,
    object_id: str | None = None,
) -> bytes:
    """A Stripe-shaped event, indented like the real ones: re-serialising it changes its bytes."""
    event: dict[str, Any] = {
        "id": event_id or "evt_" + uuid.uuid4().hex[:24],
        "object": "event",
        "api_version": "2026-09-30.endive",
        "created": int(time.time()),
        "data": {
            "object": {
                "id": object_id or account,
                "object": "account",
                "metadata": {"pad": "x" * pad},
            }
        },
        "livemode": False,
        "pending_webhooks": 1,
        "request": {"id": None, "idempotency_key": None},
        "type": type_,
    }
    if account is not None:
        event["account"] = account
    return json.dumps(event, indent=2).encode()


def connect(fake: FakeGateway, tenant_id: UUID, enabled: bool = False) -> str:
    """A business that already has a Stripe account: the row, and the account in the fake."""
    account_id = "acct_" + uuid.uuid4().hex[:16]
    fake.accounts[account_id] = {"charges_enabled": enabled, "domains": []}
    with tenant_context(tenant_id) as db:
        db.execute(
            text("INSERT INTO payment_accounts (tenant_id, stripe_account_id) VALUES (:t, :a)"),
            {"t": tenant_id, "a": account_id},
        )
        db.execute(text("UPDATE payment_accounts SET charges_enabled = :e"), {"e": enabled})
    return account_id
