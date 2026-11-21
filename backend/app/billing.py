"""The platform's own billing (ZIF-79): an owner subscribes through Stripe Checkout and manages the
subscription in Stripe's customer portal, on the platform account.

These routes store nothing: the app role writes no billing table (0103). Checkout creates the
Stripe customer, and the billing webhook's job (reconcile, as the worker) stores it with the
subscription, from Stripe's copy, never the event's. As in app.payments, no transaction (and so no
pool connection) is held across a Stripe call: a route reads, closes the transaction, then calls
Stripe."""

import asyncio
import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app import auth, business_settings, enforcement, jobs, limits, logs, stripe_gateway
from app.config import MailSettings, StripeSettings
from app.db import SessionLocal, tenant_context
from app.entitlements import coupons, entitlements
from app.errors import ApiError, Error
from app.jobs import Job
from app.services import STRICT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["billing"])
webhook_router = APIRouter(prefix="/api", tags=["billing"], include_in_schema=False)
# The platform's own events: a separate endpoint and secret from the Connect one (Stripe sends
# connected-account and platform events to different endpoints).
WEBHOOK_PATH = "/api/stripe/billing-webhook"
# Each says "look at this subscription again"; others are accepted (200) and dropped.
HANDLED = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_failed",
}
# The statuses that give a plan, as entitlements.READ counts them.
LIVE = {"active", "trialing", "past_due"}
# The ticket-number advisory lock convention (app/schedule.py): one business's events take turns,
# so two new subscriptions cannot both find no row.
LOCK_KEY = 79
# How many times reconcile writes a subscription that keeps changing before the job retries.
RECONCILE_TRIES = 3

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
LOCK = text("SELECT pg_advisory_xact_lock(:key, hashtext(current_setting('app.tenant_id')))")
TENANT = text("SELECT EXISTS (SELECT 1 FROM tenants WHERE id = :t)")
CURRENT = text("SELECT stripe_subscription_id, status FROM subscriptions")
PLAN_FOR_PRICE = text("SELECT id FROM plans WHERE stripe_price_id = :price")
PRICE_OF = text("SELECT stripe_price_id FROM plans WHERE id = :p")
# R11: an unknown Price (plan NULL) keeps the plan the same subscription had (a replaced, ended
# one's plan is not this one's); everything else is Stripe's now.
UPSERT = text("""
INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id, plan_id, status,
  current_period_end, cancel_at_period_end)
VALUES (:t, :customer, :sub, :plan, :status, :period_end, :cancel)
ON CONFLICT (tenant_id) DO UPDATE SET
  stripe_customer_id = excluded.stripe_customer_id,
  stripe_subscription_id = excluded.stripe_subscription_id,
  plan_id = CASE WHEN subscriptions.stripe_subscription_id = excluded.stripe_subscription_id
    THEN coalesce(excluded.plan_id, subscriptions.plan_id) ELSE excluded.plan_id END,
  status = excluded.status,
  current_period_end = excluded.current_period_end,
  cancel_at_period_end = excluded.cancel_at_period_end,
  updated_at = clock_timestamp()
""")
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
        # A coupon Stripe says is gone or spent would fail the session: say so. Unknown (Stripe
        # not answering) goes ahead, and Stripe decides.
        if plan.stripe_coupon_id is not None:
            coupon = coupons([plan.stripe_coupon_id]).get(plan.stripe_coupon_id)
            if coupon is not None and not coupon.valid:
                raise ApiError(409, "coupon_unavailable")
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


def sync_portal() -> bool:
    """The customer portal offers exactly the public, unarchived plans with a Price; called after a
    plan is saved, published or archived. False, asking nothing, where Stripe is not set up; raises
    stripe_gateway.Failed when Stripe refuses (the plan is saved either way: saving again
    retries)."""
    if not StripeSettings().secret_ready():
        return False
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
    return True


def _accept(event: stripe_gateway.BillingEvent) -> None:
    with SessionLocal.begin() as session:
        jobs.enqueue(
            session,
            "billing.event",
            f"billing.event:{event.id}",
            {"event_id": event.id, "type": event.type, "subscription_id": event.subscription_id},
        )
    logger.info("billing event accepted", extra={"event_type": event.type, "event_id": event.id})


@webhook_router.post("/stripe/billing-webhook", name="billing_webhook")
async def webhook(request: Request) -> JSONResponse:
    settings = StripeSettings()
    if not (settings.secret_ready() and settings.stripe_billing_webhook_secret.get_secret_value()):
        return JSONResponse({"code": "billing_unavailable"}, status_code=503)
    body = await request.body()
    try:
        event = stripe_gateway.gateway.construct_billing_event(
            body, request.headers.get("stripe-signature")
        )
    except stripe_gateway.BadSignature:
        logger.debug("stripe billing signature refused")
        return JSONResponse({"code": "bad_signature"}, status_code=400)
    if event.type in HANDLED and event.subscription_id:
        await asyncio.to_thread(_accept, event)
    else:
        logger.debug("stripe billing event ignored", extra={"event_type": event.type})
    return JSONResponse({})


def _business(found: stripe_gateway.SubscriptionInfo) -> UUID | None:
    try:
        return UUID(found.tenant_id or "")
    except ValueError:
        return None


def reconcile(job: Job) -> None:
    """The subscriptions row as Stripe has the subscription now, whatever the event said or the
    order events came in. A business's second live subscription is cancelled and refunded (R20)."""
    if not StripeSettings().secret_ready():
        raise RuntimeError("stripe secret key unset")
    sub_id = job.payload.get("subscription_id")
    if not isinstance(sub_id, str):
        return
    found = stripe_gateway.gateway.retrieve_subscription(sub_id)
    # Stripe is read outside the lock (no transaction stays open across a Stripe call), so two
    # jobs for one subscription can write in the other order than they read. Whoever writes last
    # reads Stripe again after its commit and writes again until the row matches: an older
    # snapshot never outlives a newer one.
    for _ in range(RECONCILE_TRIES):
        tenant_id, kept, wrote = _apply(found)
        if not wrote:
            break
        again = stripe_gateway.gateway.retrieve_subscription(sub_id)
        if again == found:
            break
        found = again
    else:
        raise RuntimeError("subscription kept changing during reconcile")
    if tenant_id is None:
        logger.error("billing subscription names no business", extra={"subscription": found.id})
    elif kept is not None:
        with logs.bound(tenant_id=str(tenant_id)):
            # The row may lag Stripe: if the kept one has ended, its own event will say so, and the
            # retry then lets this one replace it instead of leaving the business with neither.
            if stripe_gateway.gateway.retrieve_subscription(kept).status not in LIVE:
                raise RuntimeError("kept subscription no longer live")
            # Refund first: a retry after the cancel would find it ended and refund nothing. A
            # trial has paid nothing.
            if found.status != "trialing":
                stripe_gateway.gateway.refund_latest_invoice(found.id)
            stripe_gateway.gateway.cancel_subscription(found.id, prorate=False)
            logger.error(
                "billing duplicate subscription cancelled",
                extra={"subscription": found.id, "kept": kept},
            )


def _apply(found: stripe_gateway.SubscriptionInfo) -> tuple[UUID | None, str | None, bool]:
    """One locked look at the business's row: its tenant (None if it names none), the kept
    subscription when this one is a duplicate, and whether the row was written."""
    tenant_id = _business(found)
    if tenant_id is None:
        return None, None, False
    with tenant_context(tenant_id) as db:
        if not db.scalar(TENANT, {"t": tenant_id}):
            return None, None, False
        db.execute(LOCK, {"key": LOCK_KEY})
        current = db.execute(CURRENT).first()
        if (
            current is not None
            and current.stripe_subscription_id != found.id
            and current.status not in ENDED
        ):
            # The business already has one Stripe still bills or settles. A live newcomer next to
            # a live one is a duplicate; any other newcomer that isn't live is an older one (its
            # event late) and leaves the row alone; a live one replaces an unpaid or paused row.
            if found.status in LIVE and current.status in LIVE:
                return tenant_id, current.stripe_subscription_id, False
            if found.status not in LIVE:
                return tenant_id, None, False
        _write(db, tenant_id, found)
    return tenant_id, None, True


def _write(db: Session, tenant_id: UUID, found: stripe_gateway.SubscriptionInfo) -> None:
    # In the upsert's transaction: the plan's foreign-key share lock pairs with an admin's lock on
    # the plan row, so its Price cannot change while a subscription lands on it (PRICE_OF below
    # closes the gap between this read and that lock).
    plan_id = db.scalar(PLAN_FOR_PRICE, {"price": found.price_id}) if found.price_id else None
    if plan_id is None:
        logger.error("billing price matches no plan", extra={"price": found.price_id})
    db.execute(
        UPSERT,
        {
            "t": tenant_id,
            "customer": found.customer,
            "sub": found.id,
            "plan": plan_id,
            "status": found.status,
            "period_end": found.current_period_end,
            "cancel": found.cancel_at_period_end,
        },
    )
    # Read after the lock: a Price an admin moved since the lookup fails the job, and the retry
    # looks again.
    if plan_id is not None and db.scalar(PRICE_OF, {"p": plan_id}) != found.price_id:
        raise RuntimeError("plan price changed during reconcile")
    enforcement.enqueue(db, tenant_id)
