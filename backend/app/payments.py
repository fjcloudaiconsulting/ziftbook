"""Stripe Connect for owners (ZIF-158): connect a business, read its account, take Stripe's events.

Stripe calls all go through app.stripe_gateway. A call never runs under the tenant advisory lock."""

import asyncio
import logging
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth, jobs, stripe_gateway, tracing
from app.auth import CurrentOwner, CurrentSession
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


def _business(db: Session) -> tuple[str, str, str]:
    """(country, currency, name) of the transaction's business."""
    row = db.execute(
        text(
            "SELECT country, currency, name FROM tenants "
            "WHERE id = current_setting('app.tenant_id')::uuid"
        )
    ).one()
    return row.country, row.currency, row.name


@router.get(
    "/payments/account",
    name="account",
    responses={s: {"model": Error} for s in (401, 403)},
)
def read_account(current: CurrentSession, response: Response) -> PaymentAccountOut:
    response.headers["Cache-Control"] = "no-store"
    country, currency, _ = _business(current.db)
    row = current.db.execute(ACCOUNT).first()
    if not StripeSettings().configured():
        state: Literal["off", "none", "onboarding", "ready"] = "off"
    elif row is None:
        state = "none"
    else:
        enabled = row.charges_enabled
        if not enabled:
            try:
                enabled = stripe_gateway.gateway.retrieve_account(
                    row.stripe_account_id
                ).charges_enabled
            except stripe_gateway.Failed:
                pass  # a read: answer from the row (the gateway logged the WARNING)
            else:
                if enabled:
                    current.db.execute(text("UPDATE payment_accounts SET charges_enabled = true"))
        state = "ready" if enabled else "onboarding"
    return PaymentAccountOut(
        state=state,
        country_supported=country not in UNSUPPORTED_COUNTRIES,
        min_amount_minor=MIN_CHARGE.get(currency),
        currency=currency,
    )


@router.post(
    "/payments/account",
    name="connect",
    responses={s: {"model": Error} for s in (401, 403, 409, 415, 422, 503)},
)
def connect(body: ConnectIn, current: CurrentOwner, request: Request) -> UrlOut:
    if not StripeSettings().configured():
        raise _unavailable()
    country, currency, name = _business(current.db)
    if country in UNSUPPORTED_COUNTRIES:
        raise ApiError(409, "payments_unavailable_country")
    gateway = stripe_gateway.gateway
    try:
        row = current.db.execute(ACCOUNT).first()
        created = row is None
        if row is None:
            email = current.db.scalar(
                text("SELECT email FROM users WHERE id = :u"), {"u": current.user_id}
            )
            made = gateway.create_account(
                key=f"account:{current.tenant_id}",
                country=country,
                currency=currency,
                name=name,
                email=email,
                fees_collector="application"
                if NEW_ACCOUNT_FEE_MODE == "platform_pays"
                else "stripe",
            )
            # A double click returns the same account through the idempotency key, so a conflict
            # here is the same account arriving twice.
            created = (
                current.db.execute(
                    text(
                        "INSERT INTO payment_accounts (tenant_id, stripe_account_id, fee_mode) "
                        "VALUES (current_setting('app.tenant_id')::uuid, :account, :mode) "
                        "ON CONFLICT (tenant_id) DO NOTHING RETURNING 1"
                    ),
                    {"account": made.id, "mode": NEW_ACCOUNT_FEE_MODE},
                ).first()
                is not None
            )
            account = made.id
        else:
            account = row.stripe_account_id
        app_url = MailSettings().app_url.rstrip("/")
        gateway.register_domain(account, urlsplit(app_url).hostname or "")
        url = gateway.onboarding_link(
            account,
            f"{app_url}/{body.locale}/settings?stripe=return",
            f"{app_url}/{body.locale}/settings?stripe=refresh",
        )
    except stripe_gateway.Failed:
        raise _unavailable() from None
    if created:
        auth.record(
            current.db,
            request,
            "payment_account_created",
            actor_user_id=current.user_id,
            target=f"payment_account:{account}",
        )
        logger.info("payment account created", extra={"account": account})
    return UrlOut(url=url)


@router.post(
    "/payments/dashboard",
    name="dashboard",
    responses={s: {"model": Error} for s in (401, 403, 409, 503)},
)
def dashboard(current: CurrentOwner) -> UrlOut:
    if not StripeSettings().configured():
        raise _unavailable()
    row = current.db.execute(ACCOUNT).first()
    if row is None:
        raise ApiError(409, "payments_not_connected")
    try:
        return UrlOut(url=stripe_gateway.gateway.login_link(row.stripe_account_id))
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
        logger.warning("stripe signature refused")
        _count("other", "refused")
        return JSONResponse({"code": "bad_signature"}, status_code=400)
    result = await asyncio.to_thread(_accept, event)
    _count(event.type, result)
    return JSONResponse({})
