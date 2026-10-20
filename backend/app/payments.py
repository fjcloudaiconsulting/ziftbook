"""Stripe Connect (ZIF-158): connect a business, read its account, take Stripe's events, and check
a client's card at checkout.

Stripe calls all go through app.stripe_gateway. No transaction (and so no pool connection) is held
across a Stripe call: a route reads first, calls Stripe, then writes in a short transaction. A
payment's status only moves forward from `open` (reconcile is the one writer of the move), and what
Stripe says is always re-read: an event or the browser only says "look again"."""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from opentelemetry.trace import SpanKind
from pydantic import BaseModel
from sqlalchemy import Row, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import auth, bookings, business_settings, jobs, limits, logs, stripe_gateway, tracing
from app.business_settings import BusinessSettings, Locale
from app.config import MailSettings, StripeSettings
from app.db import SessionLocal, count_after_commit, tenant_context
from app.errors import ApiError, Error
from app.jobs import Job
from app.services import STRICT, Price

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["payments"])
webhook_router = APIRouter(prefix="/api", tags=["payments"], include_in_schema=False)
# The booking page's client, no session: the booking id (a uuidv7) is the capability.
public_router = APIRouter(prefix="/api/public", tags=["bookings"])
WEBHOOK_PATH = "/api/stripe/webhook"

# D6 (ZIF-158): who pays Stripe's fees on an account created now. Stripe fixes it at creation.
NEW_ACCOUNT_FEE_MODE = "business_pays"
# ZIF-59: Stripe Connect is not offered to Brazilian businesses.
UNSUPPORTED_COUNTRIES = {"BR"}
# Stripe's smallest charge, in minor units, per currency it publishes one for.
MIN_CHARGE = {"EUR": 50, "USD": 50, "GBP": 30}
# How a card is checked (decisions: the authorisation is the default, the SetupIntent the fallback
# if Stripe or the networks refuse routine authorise-then-void). The gateway is the only place that
# acts on it.
CHECK_WITH: stripe_gateway.Mode = "authorisation"
# R6 (owner, 2026-10-19): a client's cancel keeps the card fee only once a lawyer says that is
# allowed. Off: the booking page says so, and no copy mentions a kept fee.
KEEP_CARD_FEE_ON_CLIENT_CANCEL = False
# The intent events that say "look at this payment again" (reconcile re-reads Stripe, not the body).
INTENT_EVENTS = {
    "payment_intent.amount_capturable_updated",
    "payment_intent.succeeded",
    "payment_intent.processing",
    "payment_intent.payment_failed",
    "payment_intent.canceled",
    "setup_intent.succeeded",
    "setup_intent.setup_failed",
}
# The event types with a handler. Others are accepted (200) and dropped until theirs exists.
HANDLED = {"account.updated"} | INTENT_EVENTS
# Per booking and IP, and per IP in all: the per-booking key holds a caller-chosen id and runs
# before the 404, so without the total random ids would buy unlimited budget.
PAYMENT_LIMIT, PAYMENT_TOTAL_LIMIT, PAYMENT_WINDOW = 60, 600, timedelta(hours=1)

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


def available(db: Session, settings: BusinessSettings) -> str | None:
    """The Stripe account id when the business takes a card check at all, else None (fail open)."""
    if not StripeSettings().configured() or not settings.payment_check:
        return None
    row = db.execute(ACCOUNT).first()
    return row.stripe_account_id if row is not None and row.charges_enabled else None


def offer(db: Session, settings: BusinessSettings, amount_minor: int, currency: str) -> str | None:
    """The Stripe account id when the payment step is on for this price, else None (fail open)."""
    minimum = MIN_CHARGE.get(currency)
    if minimum is None or amount_minor < minimum:
        return None
    return available(db, settings)


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
    if job.tenant_id is None:
        return
    event_type, intent = job.payload.get("type"), job.payload.get("object_id")
    if event_type == "account.updated":
        refresh_account(job.tenant_id)
    elif event_type in INTENT_EVENTS and isinstance(intent, str):
        reconcile(job.tenant_id, intent)


def _unavailable() -> ApiError:
    return ApiError(503, "payments_unavailable")


@dataclass(frozen=True)
class Seen:
    """What a Stripe-calling route needs, read in a transaction that ends before it calls Stripe."""

    tenant_id: UUID
    user_id: UUID
    country: str
    currency: str
    account: str | None
    enabled: bool


# The session is a dependency (auth.resolve), not a call in a body, so the name-gate walk in
# tests/test_names.py sees these routes.
Resolved = Annotated[Row[Any], Depends(auth.resolve)]


def _see(row: Resolved, owner_only: bool) -> Seen:
    with tenant_context(row.tenant_id) as db:
        current = auth.SignedIn(row.user_id, row.tenant_id, row.role, db)
        auth.named(current)
        if owner_only:
            auth.owner(current)
        business = db.execute(
            text(
                "SELECT country, currency FROM tenants "
                "WHERE id = current_setting('app.tenant_id')::uuid"
            )
        ).one()
        account = db.execute(ACCOUNT).first()
    seen = Seen(
        row.tenant_id,
        row.user_id,
        business.country,
        business.currency,
        account.stripe_account_id if account else None,
        bool(account and account.charges_enabled),
    )
    return seen


def _member(row: Resolved) -> Seen:
    return _see(row, False)


def _owner(row: Resolved) -> Seen:
    return _see(row, True)


AnyMember = Annotated[Seen, Depends(_member)]
Owner = Annotated[Seen, Depends(_owner)]


def _limited(seen: Seen, kind: Literal["read", "write"]) -> bool:
    # Separate budgets: a member polling the read cannot push the owner's POSTs into 429.
    return limits.hit({f"payments:{kind}:{seen.tenant_id}": STRIPE_LIMIT}, STRIPE_WINDOW)


@router.get(
    "/payments/account",
    name="account",
    responses={s: {"model": Error} for s in (401, 403)},
)
def read_account(seen: AnyMember, response: Response) -> PaymentAccountOut:
    # Bound in the handler: a value a dependency sets never reaches the handler's context.
    with logs.bound(tenant_id=str(seen.tenant_id)):
        response.headers["Cache-Control"] = "no-store"
        if not StripeSettings().configured():
            state: Literal["off", "none", "onboarding", "ready"] = "off"
        elif seen.account is None:
            state = "none"
        else:
            enabled = seen.enabled
            # A read: limited or failed, it answers from the row (the gateway logged the WARNING).
            if not enabled and not _limited(seen, "read"):
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
    with logs.bound(tenant_id=str(seen.tenant_id)):
        response.headers["Cache-Control"] = "no-store"
        if not StripeSettings().configured():
            raise _unavailable()
        if seen.country in UNSUPPORTED_COUNTRIES:
            raise ApiError(409, "payments_unavailable_country")
        if _limited(seen, "write"):
            raise ApiError(429, "rate_limited")
        gateway = stripe_gateway.gateway
        account = seen.account
        try:
            if account is None:
                made = gateway.create_account(
                    key=f"account:{seen.tenant_id}",
                    tenant_id=str(seen.tenant_id),
                    country=seen.country,
                    currency=seen.currency,
                    fees_collector="application"
                    if NEW_ACCOUNT_FEE_MODE == "platform_pays"
                    else "stripe",
                )
                account = made.id
                # The row is written before anything else can fail, so Stripe's events for the
                # account always find their business. A double click returns the same account
                # through the idempotency key, so a conflict here is the same account twice.
                with tenant_context(seen.tenant_id) as db:
                    created = (
                        db.execute(
                            text(
                                "INSERT INTO payment_accounts "
                                "(tenant_id, stripe_account_id, fee_mode) "
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
    with logs.bound(tenant_id=str(seen.tenant_id)):
        response.headers["Cache-Control"] = "no-store"
        if not StripeSettings().configured():
            raise _unavailable()
        if seen.account is None:
            raise ApiError(409, "payments_not_connected")
        if _limited(seen, "write"):
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


READ_PAYMENT = text("""
SELECT p.id, p.booking_id, p.kind, p.amount_minor, p.currency,
       (SELECT stripe_account_id FROM payment_accounts) AS account
FROM booking_payments p WHERE p.intent_id = :intent
""")
# The row, locked: its status is re-read here, after the tenant lock, so a second reconcile of the
# same intent finds the first one's answer.
LOCK_PAYMENT = text("SELECT status FROM booking_payments WHERE id = :id FOR UPDATE")
# Both writes qualify on `open`: nothing ever moves a payment back (events arrive out of order).
MARK_CHECKED = text("""
UPDATE booking_payments
SET status = 'checked', settled_at = CASE WHEN :setup THEN now() END
WHERE id = :id AND status = 'open'
RETURNING 1
""")
MARK_CANCELED = text("""
UPDATE booking_payments SET status = 'canceled', settled_at = now()
WHERE id = :id AND status = 'open'
RETURNING 1
""")
# The check is the card's, never the price's: no application fee (nothing is captured), and the row
# keeps the fee policy that applied (D6) from the account as it is now.
INSERT_PAYMENT = text("""
INSERT INTO booking_payments
  (tenant_id, booking_id, kind, intent_id, amount_minor, currency, fee_mode, application_fee_minor)
SELECT current_setting('app.tenant_id')::uuid, :booking_id, 'check', :intent_id, :amount_minor,
       :currency, fee_mode, 0
FROM payment_accounts
RETURNING 1
""")
# D2: money still owed an answer, re-armed every hour whatever happened to earlier jobs.
UNSETTLED = text("""
SELECT DISTINCT b.id, b.status FROM booking_payments p
JOIN bookings b ON b.tenant_id = p.tenant_id AND b.id = p.booking_id
WHERE p.settled_at IS NULL
  AND ((p.status = 'open' AND NOT (b.status = 'awaiting_payment' AND b.expires_at > now()))
       OR p.status = 'checked')
""")
SETTLE_ROWS = text("""
SELECT p.id, p.intent_id, p.kind, p.status,
       (b.status = 'awaiting_payment' AND b.expires_at > now()) AS live,
       (SELECT stripe_account_id FROM payment_accounts) AS account
FROM booking_payments p
JOIN bookings b ON b.tenant_id = p.tenant_id AND b.id = p.booking_id
WHERE p.booking_id = :booking_id AND p.settled_at IS NULL
ORDER BY p.created_at
""")
RELEASED = text("""
UPDATE booking_payments SET settled_at = now() WHERE id = :id AND settled_at IS NULL
RETURNING 1
""")


def hour() -> str:
    """The sweep's generation: a settle job per booking per hour (UTC YYYYMMDDHH)."""
    return datetime.now(UTC).strftime("%Y%m%d%H")


def _outcome(kind: str, outcome: str) -> None:
    tracing.PAYMENT_OUTCOMES.add(1, {"payment.kind": kind, "payment.outcome": outcome})


def start(
    tenant_id: UUID, account: str, booking: bookings.BookingOut, expires_at: datetime
) -> bookings.PaymentStart:
    """The rest of a create that placed an awaiting_payment booking: the intent at Stripe (no lock,
    no transaction), then its row. If either fails the checkout hold is given back at once."""
    with logs.bound(tenant_id=str(tenant_id)):
        try:
            made = stripe_gateway.gateway.create_check(
                account,
                mode=CHECK_WITH,
                tenant_id=str(tenant_id),
                booking_id=str(booking.id),
                amount=booking.price.amount_minor,
                currency=booking.price.currency,
                idempotency_key=f"pi:{booking.id}:1",
            )
        except stripe_gateway.Failed as failure:
            logger.warning(
                "payment start failed",
                extra={
                    "booking_id": str(booking.id),
                    "stripe_request_id": failure.request_id,
                    "failure": failure.kind,
                },
            )
            _outcome("check", "failed")
            _give_back(tenant_id, booking.id)
            raise _unavailable() from None
        try:
            _insert(tenant_id, booking, made.id)
        except OperationalError:
            # The intent never reached a browser: nobody can confirm it, and no row names it.
            _give_back(tenant_id, booking.id)
            raise ApiError(503, "busy") from None
        logger.info(
            "payment started",
            extra={
                "booking_id": str(booking.id),
                "intent": made.id,
                "kind": "check",
                "check_with": CHECK_WITH,
            },
        )
        _outcome("check", "started")
        return bookings.PaymentStart(
            client_secret=made.client_secret, type=made.type, expires_at=expires_at
        )


def _insert(tenant_id: UUID, booking: bookings.BookingOut, intent_id: str) -> None:
    with tenant_context(tenant_id) as db:
        db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})
        stored = db.execute(
            INSERT_PAYMENT,
            {
                "booking_id": booking.id,
                "intent_id": intent_id,
                "amount_minor": booking.price.amount_minor,
                "currency": booking.price.currency,
            },
        ).first()
        assert stored is not None  # an account row exists once offer() named it


def _give_back(tenant_id: UUID, booking_id: UUID) -> None:
    """Expire the checkout hold now. Best effort: if this fails too, the 30-minute TTL ends it."""
    try:
        with tenant_context(tenant_id) as db:
            db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})
            db.execute(bookings.EXPIRE_ONE, {"id": booking_id})
    except Exception as error:
        logger.warning(
            "checkout hold not released",
            extra={"booking_id": str(booking_id), "error": type(error).__name__},
        )


def reconcile(tenant_id: UUID, intent_id: str) -> stripe_gateway.IntentState | None:
    """Look at the payment again, at Stripe, and write what it says (the sync route, a webhook
    event and the settle job all end here). Returns what Stripe said, None for an unknown intent."""
    with tenant_context(tenant_id) as db:
        row = db.execute(READ_PAYMENT, {"intent": intent_id}).first()
    if row is None:
        logger.debug("payment unknown", extra={"intent": intent_id})
        return None
    with tracing.span("payments.reconcile", SpanKind.INTERNAL, {"payment.kind": row.kind}):
        state = stripe_gateway.gateway.retrieve_intent(row.account, intent_id)
        with tenant_context(tenant_id) as db:
            db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})
            if db.execute(LOCK_PAYMENT, {"id": row.id}).one().status != "open":
                return state
            if state.type == "payment" and (
                state.amount != row.amount_minor or (state.currency or "").upper() != row.currency
            ):
                logger.error("intent does not match its payment", extra={"intent": intent_id})
                return state
            held = "requires_capture" if state.type == "payment" else "succeeded"
            if row.kind == "check" and state.status == held:
                _checked(db, tenant_id, row, state)
            elif state.status == "canceled" and db.execute(MARK_CANCELED, {"id": row.id}).first():
                count_after_commit(
                    db,
                    tracing.PAYMENT_OUTCOMES,
                    {"payment.kind": row.kind, "payment.outcome": "canceled"},
                )
                logger.info(
                    "payment reconciled",
                    extra={"intent": intent_id, "from": "open", "to": "canceled"},
                )
    return state


def _checked(
    db: Session, tenant_id: UUID, row: Row[Any], state: stripe_gateway.IntentState
) -> None:
    """The card is checked: the row moves, the hold is released (an authorisation), and the booking,
    if its checkout is still live, becomes a request (announced now, not at placement)."""
    setup = state.type == "setup"
    db.execute(MARK_CHECKED, {"id": row.id, "setup": setup})
    if not setup:
        _release_job(db, tenant_id, row.booking_id)
    paid = db.execute(
        bookings.PAID,
        {
            "id": row.booking_id,
            "pending_ttl_hours": business_settings.read(db).pending_ttl_hours,
        },
    ).first()
    if paid is not None:
        db.execute(
            bookings.INSERT_EVENT,
            {
                "booking_id": row.booking_id,
                "event": "payment_checked",
                "ip": None,
                "user_agent": None,
                "policy_version": None,
                "consent_purposes": None,
                "actor_user_id": None,
                "details": json.dumps({"amount_minor": row.amount_minor, "currency": row.currency}),
            },
        )
        bookings.announce(
            db, tenant_id, paid.id, paid.worker_id, paid.status, paid.starts_at, paid.now
        )
    # Not live (the hold ran out first): the booking stays expired and the release still runs.
    count_after_commit(
        db, tracing.PAYMENT_OUTCOMES, {"payment.kind": row.kind, "payment.outcome": "checked"}
    )
    logger.info(
        "payment reconciled",
        extra={
            "intent": state.id,
            "from": "open",
            "to": "checked",
            "booking_status": paid.status if paid is not None else None,
        },
    )


def _release_job(db: Session, tenant_id: UUID, booking_id: UUID) -> None:
    jobs.enqueue(
        db,
        "payment.settle",
        f"payment.settle:{tenant_id}:{booking_id}:checked",
        {"booking_id": str(booking_id)},
        tenant_id=tenant_id,
    )


def enqueue_unsettled(db: Session, tenant_id: UUID) -> None:
    """The sweep's backstop (in its tenant transaction, after EXPIRE): a settle job for every
    booking still owed an answer, keyed by the hour so a given-up job is tried again."""
    for row in db.execute(UNSETTLED).all():
        jobs.enqueue(
            db,
            "payment.settle",
            f"payment.settle:{tenant_id}:{row.id}:{row.status}:h{hour()}",
            {"booking_id": str(row.id)},
            tenant_id=tenant_id,
        )


def settle(job: Job) -> None:
    """Cancel what nobody will finish and release what was only checked. Repeatable: every write is
    qualified, so a second run finds nothing to do. A late success is handled in the same run."""
    if not StripeSettings().secret_ready():
        raise RuntimeError("stripe secret key unset")
    if job.tenant_id is None:
        return
    booking_id = UUID(job.payload["booking_id"])
    with logs.bound(tenant_id=str(job.tenant_id)):
        # A reconcile can move a row (a late success), which the second pass then releases.
        if _settle_pass(job.tenant_id, booking_id, final=False):
            _settle_pass(job.tenant_id, booking_id, final=True)


def _settle_pass(tenant_id: UUID, booking_id: UUID, *, final: bool) -> bool:
    """One look at the booking's unsettled payments. True when a reconcile ran (look again)."""
    with tenant_context(tenant_id) as db:
        rows = db.execute(SETTLE_ROWS, {"booking_id": booking_id}).all()
    reconciled = False
    gateway = stripe_gateway.gateway
    for row in rows:
        with tracing.span("payments.settle", SpanKind.INTERNAL, {"payment.kind": row.kind}):
            if row.status == "open" and not row.live:
                status = gateway.cancel_intent(row.account, row.intent_id)
                if status == "canceled":
                    _write(tenant_id, MARK_CANCELED, row, "canceled")
                elif final:
                    # Stripe refuses while a payment is under way (iDEAL, Bancontact): the job
                    # retries, and the hourly sweep re-arms it.
                    raise RuntimeError(f"payment cannot be cancelled yet ({status})")
                else:
                    reconcile(tenant_id, row.intent_id)
                    reconciled = True
            elif row.status == "checked" and row.intent_id.startswith("pi_"):
                status = gateway.cancel_intent(row.account, row.intent_id)
                if status != "canceled":
                    raise RuntimeError(f"the hold cannot be released ({status})")
                _write(tenant_id, RELEASED, row, "released")
    return reconciled


def _write(tenant_id: UUID, statement: Any, row: Row[Any], action: str) -> None:
    with tenant_context(tenant_id) as db:
        if db.execute(statement, {"id": row.id}).first() is None:
            return  # another run got there first: it counted and logged
        count_after_commit(
            db, tracing.PAYMENT_OUTCOMES, {"payment.kind": row.kind, "payment.outcome": action}
        )
    logger.info("payment settled", extra={"intent": row.intent_id, "action": action})


class SyncPayment(BaseModel):
    kind: Literal["check", "pay"]
    # The row's status, except `processing` while Stripe says a payment is under way.
    state: Literal["open", "processing", "checked", "paid", "canceled"]
    failed: bool  # the open intent's last attempt was declined or abandoned at the bank


class SyncOut(bookings.BookingCore):
    payment: SyncPayment


class ResumeIn(BaseModel):
    model_config = STRICT
    kind: Literal["check", "pay"]


# The booking and its live payment (else its newest), read together; `n` numbers the next intent.
CHECKOUT = text("""
SELECT b.id,
       CASE WHEN b.status = 'awaiting_payment' AND b.expires_at <= now() THEN 'expired'
            ELSE b.status END AS status,
       b.starts_at, b.ends_at, b.duration_minutes, b.service_name,
       b.price_amount_minor, b.price_currency, b.worker_id, b.worker_display_name,
       b.cancellation_policy_text, b.expires_at,
       (b.status = 'awaiting_payment' AND b.expires_at > now()) AS live
FROM bookings b WHERE b.id = :id AND b.source = 'booking_page'
""")
LIVE_PAYMENT = text("""
SELECT p.id, p.kind, p.status, p.intent_id,
       (SELECT count(*) FROM booking_payments WHERE booking_id = :id) AS n,
       (SELECT stripe_account_id FROM payment_accounts) AS account
FROM booking_payments p WHERE p.booking_id = :id
ORDER BY (p.status <> 'canceled') DESC, p.created_at DESC LIMIT 1
""")


def _checkout(tenant_id: UUID, booking_id: UUID) -> tuple[Row[Any] | None, Row[Any] | None]:
    with tenant_context(tenant_id) as db:
        return (
            db.execute(CHECKOUT, {"id": booking_id}).first(),
            db.execute(LIVE_PAYMENT, {"id": booking_id}).first(),
        )


def _over(booking_id: UUID, request: Request) -> bool:
    """One budget per booking and IP, and the IP's total, in one call (see PAYMENT_TOTAL_LIMIT)."""
    ip = request.client.host if request.client else None
    return limits.hit(
        {
            limits.ip_key(f"booking_payment:{booking_id}", ip): PAYMENT_LIMIT,
            limits.ip_key("booking_payment", ip): PAYMENT_TOTAL_LIMIT,
        },
        PAYMENT_WINDOW,
    )


def _enter(tenant_id: UUID, booking_id: UUID, request: Request, response: Response) -> None:
    # Limit first: before any lookup, so a guessed id costs a request, not a 404 oracle for free.
    if _over(booking_id, request):
        raise ApiError(429, "rate_limited")
    request.state.tenant_id = tenant_id
    response.headers["Cache-Control"] = "no-store"


def _out(booking: Row[Any], payment: SyncPayment) -> SyncOut:
    return SyncOut(
        id=booking.id,
        status=booking.status,
        starts_at=booking.starts_at,
        ends_at=booking.ends_at,
        duration_minutes=booking.duration_minutes,
        service_name=booking.service_name,
        price=Price(amount_minor=booking.price_amount_minor, currency=booking.price_currency),
        worker_id=booking.worker_id,
        worker_display_name=booking.worker_display_name,
        cancellation_policy_text=booking.cancellation_policy_text,
        payment=payment,
    )


@public_router.post(
    "/businesses/{tenant_id}/bookings/{booking_id}/payment/sync",
    name="sync",
    responses={s: {"model": Error} for s in (404, 422, 429, 503)},
)
def sync(tenant_id: UUID, booking_id: UUID, request: Request, response: Response) -> SyncOut:
    """Where the booking's payment stands, after looking at Stripe. Takes nothing from the caller:
    the page decides from `status` and `payment.state` alone."""
    _enter(tenant_id, booking_id, request, response)
    with logs.bound(tenant_id=str(tenant_id)):
        booking, payment = _checkout(tenant_id, booking_id)
        if booking is None or payment is None:
            raise ApiError(404, "not_found")
        state = None
        if payment.status == "open":
            try:
                state = reconcile(tenant_id, payment.intent_id)
            except stripe_gateway.Failed:
                raise _unavailable() from None
            booking, payment = _checkout(tenant_id, booking_id)
            assert booking is not None and payment is not None
        shown = payment.status
        if payment.status == "open" and state is not None and state.status == "processing":
            shown = "processing"
        return _out(
            booking,
            SyncPayment(
                kind=payment.kind,
                state=shown,
                failed=payment.status == "open" and state is not None and state.failed,
            ),
        )


@public_router.put(
    "/businesses/{tenant_id}/bookings/{booking_id}/payment",
    name="resume",
    responses={s: {"model": Error} for s in (404, 409, 415, 422, 429, 503)},
)
def resume(
    tenant_id: UUID, booking_id: UUID, body: ResumeIn, request: Request, response: Response
) -> bookings.PaymentStart:
    """Pick the payment up again (a refresh, Back from a bank page, a retry after a decline): the
    open intent's secret, or a new intent when Stripe canceled the old one."""
    _enter(tenant_id, booking_id, request, response)
    with logs.bound(tenant_id=str(tenant_id)):
        booking, payment = _checkout(tenant_id, booking_id)
        if booking is None or payment is None or not booking.live:
            raise ApiError(404, "not_found")
        if payment.status in ("checked", "paid"):
            raise ApiError(409, "payment_settled")
        if body.kind != payment.kind:  # B3 opens the switch to paying in advance
            raise ApiError(422, "payment_not_offered")
        gateway = stripe_gateway.gateway
        try:
            if payment.status == "open":
                state = gateway.retrieve_intent(payment.account, payment.intent_id)
                if state.status != "canceled":
                    return bookings.PaymentStart(
                        client_secret=state.client_secret,
                        type=state.type,
                        expires_at=booking.expires_at,
                    )
            made = gateway.create_check(
                payment.account,
                mode=CHECK_WITH,
                tenant_id=str(tenant_id),
                booking_id=str(booking_id),
                amount=booking.price_amount_minor,
                currency=booking.price_currency,
                idempotency_key=f"pi:{booking_id}:{payment.n + 1}",
            )
        except stripe_gateway.Failed:
            raise _unavailable() from None
        try:
            with tenant_context(tenant_id) as db:
                db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})
                db.execute(MARK_CANCELED, {"id": payment.id})
                db.execute(
                    INSERT_PAYMENT,
                    {
                        "booking_id": booking_id,
                        "intent_id": made.id,
                        "amount_minor": booking.price_amount_minor,
                        "currency": booking.price_currency,
                    },
                )
        except OperationalError, IntegrityError:
            raise ApiError(503, "busy") from None
        logger.info(
            "payment started",
            extra={
                "booking_id": str(booking_id),
                "intent": made.id,
                "kind": "check",
                "check_with": CHECK_WITH,
            },
        )
        _outcome("check", "started")
        return bookings.PaymentStart(
            client_secret=made.client_secret, type=made.type, expires_at=booking.expires_at
        )


@public_router.post(
    "/businesses/{tenant_id}/bookings/{booking_id}/payment/abandon",
    name="abandon",
    status_code=204,
    responses={s: {"model": Error} for s in (404, 409, 422, 429, 503)},
)
def abandon(tenant_id: UUID, booking_id: UUID, request: Request, response: Response) -> None:
    """The client changes the time or the details, or starts over: give the slot and the pending
    place back at once. Never undoes a check that landed (409: show the sync state instead)."""
    _enter(tenant_id, booking_id, request, response)
    with logs.bound(tenant_id=str(tenant_id)):
        booking, payment = _checkout(tenant_id, booking_id)
        if booking is None or not booking.live:
            raise ApiError(404, "not_found")
        try:
            if payment is not None and payment.status == "open":
                if stripe_gateway.gateway.cancel_intent(payment.account, payment.intent_id) != (
                    "canceled"
                ):
                    # Past cancelling (a payment under way, a check that landed): look again.
                    reconcile(tenant_id, payment.intent_id)
                    raise ApiError(409, "payment_settled")
        except stripe_gateway.Failed:
            raise _unavailable() from None
        with tenant_context(tenant_id) as db:
            db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})
            # No row: the check or payment landed in between. The raise rolls everything back.
            if db.execute(bookings.EXPIRE_ONE, {"id": booking_id}).first() is None:
                raise ApiError(409, "payment_settled")
            if payment is not None:
                db.execute(MARK_CANCELED, {"id": payment.id})
        response.status_code = 204
