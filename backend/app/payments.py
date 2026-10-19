"""Stripe Connect for owners (ZIF-158): connect a business, read its account, take Stripe's events.

Stripe calls all go through app.stripe_gateway. No transaction (and so no pool connection) is held
across a Stripe call: a route reads first, calls Stripe, then writes in a short transaction."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth, jobs, limits, stripe_gateway, tracing
from app.business_settings import BusinessSettings, Locale
from app.config import MailSettings, StripeSettings
from app.db import SessionLocal, tenant_context
from app.errors import ApiError, Error
from app.jobs import Job
from app.services import STRICT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["payments"])
webhook_router = APIRouter(prefix="/api", tags=["payments"], include_in_schema=False)
WEBHOOK_PATH = "/api/stripe/webhook"

# D6 (ZIF-158): who pays Stripe's fees on an account created now. Stripe fixes it at creation.
NEW_ACCOUNT_FEE_MODE = "business_pays"
# ZIF-59: Stripe Connect is not offered to Brazilian businesses.
UNSUPPORTED_COUNTRIES = {"BR"}
# Stripe's smallest charge, in minor units, per currency it publishes one for.
MIN_CHARGE = {"EUR": 50, "USD": 50, "GBP": 30}
# The event types with a handler. Others are accepted (200) and dropped until theirs exists.
HANDLED = {"account.updated"}

ACCOUNT = text("SELECT stripe_account_id, charges_enabled FROM payment_accounts")
# Stripe rate-limits the platform: each business gets this many Stripe-calling requests a minute.
STRIPE_LIMIT, STRIPE_WINDOW = 20, timedelta(minutes=1)


class PaymentAccountOut(BaseModel):
    state: Literal["off", "none", "onboarding", "ready"]
    country_supported: bool
    min_amount_minor: int | None
    currency: str


class ConnectIn(BaseModel):
    model_config = STRICT
    locale: Locale


class UrlOut(BaseModel):
    url: str


def offer(db: Session, settings: BusinessSettings, amount_minor: int, currency: str) -> str | None:
    """The Stripe account id when the payment step is on for this price, else None (fail open)."""
    minimum = MIN_CHARGE.get(currency)
    if (
        not StripeSettings().configured()
        or not settings.payment_check
        or minimum is None
        or amount_minor < minimum
    ):
        return None
    row = db.execute(ACCOUNT).first()
    return row.stripe_account_id if row is not None and row.charges_enabled else None


def refresh_account(tenant_id: object) -> None:
    """Write what Stripe says about the business's account into charges_enabled."""
    with tenant_context(tenant_id) as db:  # type: ignore[arg-type]
        row = db.execute(ACCOUNT).first()
    if row is None:
        return
    account = stripe_gateway.gateway.retrieve_account(row.stripe_account_id)
    with tenant_context(tenant_id) as db:  # type: ignore[arg-type]
        db.execute(
            text(
                "UPDATE payment_accounts SET charges_enabled = :enabled "
                "WHERE stripe_account_id = :account"
            ),
            {"enabled": account.charges_enabled, "account": account.id},
        )
    logger.info(
        "account refreshed",
        extra={"account": account.id, "charges_enabled": account.charges_enabled},
    )


def handle_event(job: Job) -> None:
    # The secret key alone: the webhook and publishable keys are the API's (fence B1-21).
    if not StripeSettings().secret_ready():
        raise RuntimeError("stripe secret key unset")
    if job.payload.get("type") == "account.updated" and job.tenant_id is not None:
        refresh_account(job.tenant_id)


def _unavailable() -> ApiError:
    return ApiError(503, "payments_unavailable")


@dataclass(frozen=True)
class Seen:
    """What a Stripe-calling route needs, read in a transaction that ends before it calls Stripe."""

    tenant_id: UUID
    user_id: UUID
    country: str
    currency: str
    name: str
    email: str
    account: str | None
    enabled: bool


def _see(request: Request, owner_only: bool) -> Seen:
    row = auth.resolve(request)
    with tenant_context(row.tenant_id) as db:
        current = auth.SignedIn(row.user_id, row.tenant_id, row.role, db)
        auth.named(current)
        if owner_only:
            auth.owner(current)
        business = db.execute(
            text(
                "SELECT t.country, t.currency, t.name, u.email FROM tenants t, users u "
                "WHERE t.id = current_setting('app.tenant_id')::uuid AND u.id = :u"
            ),
            {"u": row.user_id},
        ).one()
        account = db.execute(ACCOUNT).first()
    return Seen(
        row.tenant_id,
        row.user_id,
        business.country,
        business.currency,
        business.name,
        business.email,
        account.stripe_account_id if account else None,
        bool(account and account.charges_enabled),
    )


def _member(request: Request) -> Seen:
    return _see(request, False)


def _owner(request: Request) -> Seen:
    return _see(request, True)


AnyMember = Annotated[Seen, Depends(_member)]
Owner = Annotated[Seen, Depends(_owner)]


def _limited(seen: Seen) -> bool:
    return limits.hit({f"payments:tenant:{seen.tenant_id}": STRIPE_LIMIT}, STRIPE_WINDOW)


@router.get(
    "/payments/account",
    name="account",
    responses={s: {"model": Error} for s in (401, 403)},
)
def read_account(seen: AnyMember, response: Response) -> PaymentAccountOut:
    response.headers["Cache-Control"] = "no-store"
    if not StripeSettings().configured():
        state: Literal["off", "none", "onboarding", "ready"] = "off"
    elif seen.account is None:
        state = "none"
    else:
        enabled = seen.enabled
        # A read: limited or failed, it answers from the row (the gateway logged the WARNING).
        if not enabled and not _limited(seen):
            try:
                enabled = stripe_gateway.gateway.retrieve_account(seen.account).charges_enabled
            except stripe_gateway.Failed:
                pass
            else:
                if enabled:
                    with tenant_context(seen.tenant_id) as db:
                        db.execute(
                            text(
                                "UPDATE payment_accounts SET charges_enabled = true "
                                "WHERE stripe_account_id = :account"
                            ),
                            {"account": seen.account},
                        )
        state = "ready" if enabled else "onboarding"
    return PaymentAccountOut(
        state=state,
        country_supported=seen.country not in UNSUPPORTED_COUNTRIES,
        min_amount_minor=MIN_CHARGE.get(seen.currency),
        currency=seen.currency,
    )


@router.post(
    "/payments/account",
    name="connect",
    responses={s: {"model": Error} for s in (401, 403, 409, 415, 422, 429, 503)},
)
def connect(body: ConnectIn, seen: Owner, request: Request, response: Response) -> UrlOut:
    response.headers["Cache-Control"] = "no-store"
    if not StripeSettings().configured():
        raise _unavailable()
    if seen.country in UNSUPPORTED_COUNTRIES:
        raise ApiError(409, "payments_unavailable_country")
    if _limited(seen):
        raise ApiError(429, "rate_limited")
    gateway = stripe_gateway.gateway
    account = seen.account
    try:
        if account is None:
            made = gateway.create_account(
                key=f"account:{seen.tenant_id}",
                country=seen.country,
                currency=seen.currency,
                name=seen.name,
                email=seen.email,
                fees_collector="application"
                if NEW_ACCOUNT_FEE_MODE == "platform_pays"
                else "stripe",
            )
            account = made.id
            # The row is written before anything else can fail, so Stripe's events for the account
            # always find their business. A double click returns the same account through the
            # idempotency key, so a conflict here is the same account arriving twice.
            with tenant_context(seen.tenant_id) as db:
                created = (
                    db.execute(
                        text(
                            "INSERT INTO payment_accounts (tenant_id, stripe_account_id, fee_mode) "
                            "VALUES (current_setting('app.tenant_id')::uuid, :account, :mode) "
                            "ON CONFLICT (tenant_id) DO NOTHING RETURNING 1"
                        ),
                        {"account": account, "mode": NEW_ACCOUNT_FEE_MODE},
                    ).first()
                    is not None
                )
                if created:
                    auth.record(
                        db,
                        request,
                        "payment_account_created",
                        actor_user_id=seen.user_id,
                        target=f"payment_account:{account}",
                    )
            if created:
                logger.info("payment account created", extra={"account": account})
        app_url = MailSettings().app_url.rstrip("/")
        try:
            gateway.register_domain(account, urlsplit(app_url).hostname or "")
        except stripe_gateway.Failed:
            logger.warning("apple pay domain not registered", extra={"account": account})
        url = gateway.onboarding_link(
            account,
            f"{app_url}/{body.locale}/settings?stripe=return",
            f"{app_url}/{body.locale}/settings?stripe=refresh",
        )
    except stripe_gateway.Failed:
        raise _unavailable() from None
    return UrlOut(url=url)


@router.post(
    "/payments/dashboard",
    name="dashboard",
    responses={s: {"model": Error} for s in (401, 403, 409, 429, 503)},
)
def dashboard(seen: Owner, response: Response) -> UrlOut:
    response.headers["Cache-Control"] = "no-store"
    if not StripeSettings().configured():
        raise _unavailable()
    if seen.account is None:
        raise ApiError(409, "payments_not_connected")
    if _limited(seen):
        raise ApiError(429, "rate_limited")
    try:
        return UrlOut(url=stripe_gateway.gateway.login_link(seen.account))
    except stripe_gateway.Failed:
        raise _unavailable() from None


def _count(event: str, result: str) -> None:
    tracing.STRIPE_WEBHOOKS.add(
        1, {"stripe.event": event if event in HANDLED else "other", "stripe.result": result}
    )


def _accept(event: stripe_gateway.Event) -> str:
    """Enqueue the event for its business: the result, as the metric names it."""
    with SessionLocal.begin() as session:
        tenant = (
            session.scalar(
                text("SELECT stripe_account_tenant(:account)"), {"account": event.account}
            )
            if event.account
            else None
        )
    if tenant is None:
        logger.debug("stripe event ignored", extra={"reason": "unknown_account"})
        return "ignored"
    if event.type not in HANDLED:
        logger.debug("stripe event ignored", extra={"reason": "unhandled_type"})
        return "ignored"
    with SessionLocal.begin() as session:
        added = jobs.enqueue(
            session,
            "stripe.event",
            f"stripe.event:{event.id}",
            {"event_id": event.id, "type": event.type, "object_id": event.object_id},
            tenant_id=tenant,
        )
    logger.info("stripe event accepted", extra={"event_type": event.type, "event_id": event.id})
    return "enqueued" if added else "duplicate"


@webhook_router.post("/stripe/webhook", name="webhook")
async def webhook(request: Request) -> JSONResponse:
    if not StripeSettings().configured():
        return JSONResponse({"code": "payments_unavailable"}, status_code=503)
    body = await request.body()
    try:
        event = stripe_gateway.gateway.construct_event(
            body, request.headers.get("stripe-signature")
        )
    except stripe_gateway.BadSignature:
        logger.debug("stripe signature refused")  # the metric counts it
        _count("other", "refused")
        return JSONResponse({"code": "bad_signature"}, status_code=400)
    result = await asyncio.to_thread(_accept, event)
    _count(event.type, result)
    return JSONResponse({})
