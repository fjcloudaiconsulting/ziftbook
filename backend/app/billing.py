"""The platform's own billing (ZIF-79): an owner subscribes through Stripe Checkout and manages the
subscription in Stripe's customer portal, on the platform account.

These routes store nothing: the app role writes no billing table (0103). Checkout creates the
Stripe customer, and the billing webhook stores it with the subscription. As in app.payments, no
transaction (and so no pool connection) is held across a Stripe call: a route reads, closes the
transaction, then calls Stripe."""

import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app import auth, business_settings, limits, logs, stripe_gateway
from app.config import MailSettings, StripeSettings
from app.db import SessionLocal, tenant_context
from app.entitlements import entitlements
from app.errors import ApiError, Error
from app.services import STRICT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["billing"])

# A double click within one window gets the same Checkout Session (Stripe's idempotency key).
CHECKOUT_WINDOW_SECONDS = 300
# Stripe refuses a trial_end less than 48 hours away.
MIN_TRIAL_LEFT = timedelta(hours=48)
# Stripe rate-limits the platform: each business gets this many Checkout or portal sessions a
# minute (as app.payments does for Connect).
STRIPE_LIMIT, STRIPE_WINDOW = 20, timedelta(minutes=1)
# The statuses of a subscription that is over: anything else is still Stripe's to bill or settle,
# and a second Checkout would make a second subscription.
ENDED = {"canceled", "incomplete_expired"}

# The plan if this business may buy it: unarchived, and public or offered to it (plan_offers is
# under row-level security), with its offer's coupon.
PLAN = text("""
SELECT p.stripe_price_id, o.stripe_coupon_id
FROM plans p LEFT JOIN plan_offers o ON o.plan_id = p.id
WHERE p.id = :id AND p.archived_at IS NULL AND (p.public OR o.id IS NOT NULL)
""")
SUBSCRIPTION = text("SELECT stripe_customer_id, status FROM subscriptions")
PORTAL_PLANS = text("""
SELECT stripe_price_id FROM plans
WHERE public AND archived_at IS NULL AND stripe_price_id IS NOT NULL
ORDER BY created_at, id
""")


class CheckoutIn(BaseModel):
    model_config = STRICT
    plan_id: Annotated[UUID, Field(strict=False)]  # strict mode refuses every string


class UrlOut(BaseModel):
    url: str


def _owner(row: Annotated[Row[Any], Depends(auth.resolve)]) -> Row[Any]:
    """The session, owners only, checked in a transaction that ends before the handler calls
    Stripe. A dependency: anyone else gets 403 whatever the body."""
    with tenant_context(row.tenant_id) as db:
        auth.owner(auth.named(auth.SignedIn(row.user_id, row.tenant_id, row.role, db)))
    return row


Owner = Annotated[Row[Any], Depends(_owner)]


def _ready(tenant_id: UUID) -> None:
    if not StripeSettings().secret_ready():
        raise ApiError(503, "billing_unavailable")
    if limits.hit({f"billing:{tenant_id}": STRIPE_LIMIT}, STRIPE_WINDOW):
        raise ApiError(429, "rate_limited")


def _locale(db: Session, user_id: UUID) -> str:
    """The person's own language, else their business's."""
    own = db.scalar(text("SELECT locale FROM users WHERE id = :u"), {"u": user_id})
    return own or business_settings.read(db).language


def _settings_url(locale: str) -> str:
    return f"{MailSettings().app_url.rstrip('/')}/{locale}/settings?tab=plan"


@router.post(
    "/billing/checkout",
    name="checkout",
    responses={s: {"model": Error} for s in (401, 403, 404, 409, 415, 422, 429, 503)},
)
def checkout(body: CheckoutIn, row: Owner, response: Response) -> UrlOut:
    """A Stripe Checkout URL subscribing the business to the plan."""
    with logs.bound(tenant_id=str(row.tenant_id)):
        response.headers["Cache-Control"] = "no-store"
        with tenant_context(row.tenant_id) as db:
            plan = db.execute(PLAN, {"id": body.plan_id}).first()
            if plan is None:
                raise ApiError(404, "plan_not_found")
            if plan.stripe_price_id is None:
                raise ApiError(409, "not_purchasable")
            subscription = db.execute(SUBSCRIPTION).first()
            if subscription is not None and subscription.status not in ENDED:
                raise ApiError(409, "already_subscribed")
            found = entitlements(db)
            if found.source == "grant":
                raise ApiError(409, "plan_granted")
            locale = _locale(db, row.user_id)
        # Subscribing during the trial keeps its days: Stripe charges when it would have ended.
        trial_end = None
        if found.source == "trial" and found.trial_ends_at is not None:
            if found.trial_ends_at - datetime.now(UTC) >= MIN_TRIAL_LEFT:
                trial_end = int(found.trial_ends_at.timestamp())
        _ready(row.tenant_id)
        settings = _settings_url(locale)
        params = {
            "customer": subscription.stripe_customer_id if subscription else None,
            "price": plan.stripe_price_id,
            "coupon": plan.stripe_coupon_id,
            "trial_end": trial_end,
            "success_url": f"{settings}&billing=done",
            "cancel_url": settings,
            "locale": locale,
        }
        # Stripe refuses a key reused with other params: the params are in the key, so any change
        # (a new language, a coupon) gets a new session and a double click the same one.
        digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        bucket = int(time.time()) // CHECKOUT_WINDOW_SECONDS
        try:
            url = stripe_gateway.gateway.create_checkout(
                tenant_id=str(row.tenant_id),
                idempotency_key=f"checkout:{row.tenant_id}:{digest}:{bucket}",
                **params,
            )
        except stripe_gateway.Failed:
            raise ApiError(503, "billing_unavailable") from None
        return UrlOut(url=url)


@router.post(
    "/billing/portal",
    name="portal",
    responses={s: {"model": Error} for s in (401, 403, 409, 415, 429, 503)},
)
def portal(row: Owner, response: Response) -> UrlOut:
    """A Stripe customer-portal URL: plan changes, cancellation, card and invoices."""
    with logs.bound(tenant_id=str(row.tenant_id)):
        response.headers["Cache-Control"] = "no-store"
        with tenant_context(row.tenant_id) as db:
            subscription = db.execute(SUBSCRIPTION).first()
            if subscription is None:
                raise ApiError(409, "no_subscription")
            locale = _locale(db, row.user_id)
        _ready(row.tenant_id)
        try:
            url = stripe_gateway.gateway.create_portal(
                subscription.stripe_customer_id, _settings_url(locale), locale
            )
        except stripe_gateway.Failed:
            raise ApiError(503, "billing_unavailable") from None
        return UrlOut(url=url)


def sync_portal() -> None:
    """The customer portal offers exactly the public, unarchived plans with a Price; called after a
    plan is saved, published or archived. Does nothing where Stripe is not set up; raises
    stripe_gateway.Failed when Stripe refuses (the plan is saved either way: saving again
    retries)."""
    if not StripeSettings().secret_ready():
        return
    with SessionLocal.begin() as db:
        ids = list(db.scalars(PORTAL_PLANS))
    gateway = stripe_gateway.gateway
    found = gateway.list_prices(ids)
    products: dict[str, list[str]] = {}
    for price_id in ids:
        if price_id in found:
            products.setdefault(found[price_id].product, []).append(price_id)
        else:
            logger.warning("billing price unknown to stripe", extra={"price": price_id})
    gateway.sync_portal_configuration(products)
