"""The Stripe gateway for tests (ZIF-158): no network, ever.

FakeGateway is the real Gateway with its Stripe calls replaced by dicts. It keeps what Stripe would
(accounts), records every call, and honours idempotency keys the way Stripe does (same key, same
object). construct_event is inherited: webhook tests sign Stripe-shaped bodies with `signed` and
the real verification runs.
"""

import hashlib
import hmac
import json
import time
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app import logs
from app.db import tenant_context
from app.stripe_gateway import Account, Failed, FeesCollector, Gateway, Kind

WEBHOOK_SECRET = "whsec_test_secret_not_a_real_one"


class FakeGateway(Gateway):
    def __init__(self) -> None:
        super().__init__()
        self.accounts: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tenants: list[str | None] = []
        self.failures: dict[str, Kind] = {}
        self._by_key: dict[str, str] = {}
        self._params: dict[str, dict[str, Any]] = {}

    def fail_next(self, op: str, kind: Kind = "unavailable") -> None:
        self.failures[op] = kind

    def count(self, op: str) -> int:
        return sum(1 for name, _ in self.calls if name == op)

    def _enter(self, op: str, **params: Any) -> None:
        # No transaction (so no pool connection) is held across a Stripe call.
        from tests.test_service_photos_api import (
            idle_in_transaction,  # conftest imports this module
        )

        assert idle_in_transaction() == 0, f"a transaction is open across stripe {op}"
        self.calls.append((op, params))
        # The tenant the gateway's own log line would carry (logs.bound in the calling context).
        self.tenants.append(logs.CONTEXT.get({}).get("tenant_id"))
        if kind := self.failures.pop(op, None):
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
) -> bytes:
    """A Stripe-shaped event, indented like the real ones: re-serialising it changes its bytes."""
    event: dict[str, Any] = {
        "id": event_id or "evt_" + uuid.uuid4().hex[:24],
        "object": "event",
        "api_version": "2026-09-30.endive",
        "created": int(time.time()),
        "data": {
            "object": {
                "id": account,
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
