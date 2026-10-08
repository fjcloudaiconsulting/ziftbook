"""ZIF-117: a booking made without signing in exists only after its booker confirms the emailed
link (owner ruling 2026-10-08).

POST .../services/{s}/holds holds the chosen time for bookings.HOLD_TTL and queues the email;
POST /api/public/booking-hold reads the held booking for the confirm page and writes nothing (an
email scanner that opens the link changes nothing); POST /api/public/booking-hold/confirm, with the
booker's name, books it through bookings.book_online, the same core as the booking page's POST.
Nothing about a person is written to clients before that click.

Every writer of booking_holds takes the tenant lock as its first statement (migration 0034's
banner). Spec S3 of booking_links extends here: never the address, the secret, the token or a hash
of either in a log line.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, Response
from psycopg.errors import ForeignKeyViolation
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from app import (
    auth,
    availability,
    booking_links,
    bookings,
    business_settings,
    clients,
    jobs,
    limits,
    passwords,
    turnstile,
)
from app.booking_page import CancellationOut
from app.business_settings import Locale
from app.clients import ClientName, Phone, PolicyVersion, Purpose
from app.db import tenant_context
from app.errors import ApiError, Error
from app.services import STRICT, Price
from app.time_off import Instant

logger = logging.getLogger(__name__)

READ_LIMIT, READ_WINDOW = 60, timedelta(minutes=1)
WRITE_LIMIT, WRITE_WINDOW = 20, timedelta(hours=1)
# Against mail bombing: one address, every business together. The per-business 5/h is create()'s
# own key, so a hold and the booking page's POST share it.
VERIFY_LIMIT = 10

INSERT_HOLD = text("""
INSERT INTO booking_holds (tenant_id, service_id, worker_id, anyone, starts_at, ends_at, email,
                           locale, secret_hash, expires_at)
VALUES (current_setting('app.tenant_id')::uuid, :service_id, :worker_id, :anyone, :starts_at,
        :starts_at + make_interval(mins => :duration_minutes), :email, :locale, :secret_hash,
        now() + CAST(:hold_ttl AS interval))
RETURNING id, starts_at, expires_at
""")

# Finds nothing for a wrong or foreign secret (row-level security keeps it to this business): the
# request then goes on exactly as if no secret had been sent. A plain SELECT, no row lock.
REPLACED = text("SELECT id FROM booking_holds WHERE secret_hash = :hash")

# A link is live for LINK_TTL from created_at, whatever its 15-minute hold says: a late click still
# books a time that is free. Everything else (used, replaced, expired, malformed, another
# business's) finds no row and is the same 404 link_expired.
LIVE = text("""
SELECT h.id, h.service_id, h.worker_id, h.anyone, h.starts_at, h.ends_at, h.email, h.locale,
       h.expires_at
FROM booking_holds h
WHERE h.token_hash = :hash AND h.created_at > now() - CAST(:link_ttl AS interval)
""")

# The confirm page's values, LIVE (design decision 8): the price, the policy and auto_confirm in
# force at the click are the ones the booking keeps. A service archived meanwhile still reads; the
# confirm then answers 404 not_found.
VIEW = text("""
SELECT t.name AS business, t.slug, s.name AS service_name, s.price_amount_minor,
       s.price_currency, m.display_name AS worker_display_name
FROM services s
JOIN tenants t ON t.id = s.tenant_id
JOIN memberships m ON m.tenant_id = s.tenant_id AND m.id = :worker_id
WHERE s.id = :service_id
""")

DELETE_HOLD = text("DELETE FROM booking_holds WHERE id = :id")


class HoldIn(BaseModel):
    # Field by field: the address and the time, nothing else about the person (no name, phone or
    # consent: those come after the click). There is no user_id field here and never will be.
    model_config = STRICT
    starts_at: Instant  # see bookings.BookingIn
    member_id: Annotated[UUID, Field(strict=False)] | None = None  # None: "anyone"
    email: passwords.Email
    locale: Locale | None = None  # the email's and the link's language
    turnstile_token: Annotated[str, StringConstraints(max_length=2048)] | None = None
    # The secret an earlier hold POST answered with: that hold (and its link) goes. Send again,
    # wrong address and another time all work this way.
    replaces: Annotated[str, StringConstraints(max_length=64)] | None = None


class HoldOut(BaseModel):
    starts_at: datetime
    expires_at: datetime
    # Only the page that asked holds it; it never reaches a log or the database (sha256 only).
    secret: str


class HoldView(BaseModel):
    """What the confirm page shows. No client data: `email` is the hold's own address, which the
    link's bearer received it at."""

    business: str
    slug: str
    timezone: str
    language: Locale
    service_id: UUID
    service_name: dict[str, str]
    starts_at: datetime
    ends_at: datetime
    price: Price
    worker_display_name: str | None  # the held worker's; null reads as "anyone available"
    cancellation: CancellationOut
    auto_confirm: bool
    policy_version: str
    held_until: datetime
    email: str


class TokenIn(BaseModel):
    model_config = STRICT
    token: str


class ConfirmIn(BaseModel):
    model_config = STRICT
    token: str
    name: ClientName
    phone: Phone | None = None
    policy_version: PolicyVersion
    consents: dict[Purpose, bool] = Field(default_factory=dict)


router = APIRouter(prefix="/api/public", tags=["booking-holds"])


@router.post(
    "/businesses/{tenant_id}/services/{service_id}/holds",
    name="create",
    status_code=202,
    responses={s: {"model": Error} for s in (403, 404, 409, 415, 422, 429, 503)},
)
def create(  # sync def: turnstile.verify blocks, as bookings.create
    tenant_id: UUID, service_id: UUID, new: HoldIn, request: Request, response: Response
) -> HoldOut:
    """Hold a free time for the booker's email to confirm, and email them the link. Public. The
    gates and their order are bookings.create's; it never reads clients, so its answers say nothing
    about whether an address is known here."""
    ip = request.client.host if request.client else None
    if limits.hit({limits.ip_key("booking", ip): bookings.IP_LIMIT}, bookings.LIMIT_WINDOW):
        raise ApiError(429, "rate_limited")
    if not turnstile.verify(new.turnstile_token, ip):
        raise ApiError(403, "turnstile_failed")
    # After Turnstile, for the reason bookings.create gives.
    if limits.hit(
        {
            limits.email_key("booking", f"{tenant_id}:{new.email}"): bookings.EMAIL_LIMIT,
            limits.email_key("booking_verify", new.email): VERIFY_LIMIT,
        },
        bookings.LIMIT_WINDOW,
    ):
        raise ApiError(429, "rate_limited")
    request.state.tenant_id = tenant_id
    response.headers["Cache-Control"] = "no-store"
    secret = secrets.token_urlsafe(32)
    try:
        with tenant_context(tenant_id) as db:
            db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})  # first, always (0034)
            row = db.execute(bookings.SERVICE, {"service_id": service_id}).first()
            if row is None:
                raise ApiError(404, "not_found")
            settings = business_settings.read(db)
            if not settings.published:
                raise ApiError(404, "not_found")
            # The hold this one replaces: left out of re-derivation, so it doesn't block a new one
            # at the same time, and deleted LAST. Deleting it here would row-lock it before the
            # INSERT's foreign-key wait on the worker's membership, which a member removal's
            # cascade onto this row could deadlock. A 409 leaves it, and its link, as they were.
            replaced = (
                None
                if new.replaces is None
                else db.scalar(REPLACED, {"hash": hashlib.sha256(new.replaces.encode()).digest()})
            )
            db.execute(bookings.EXPIRE, {"expiring": list(availability.EXPIRING)})
            hours, _names = availability.candidates(db, service_id, new.member_id)
            # The one "is this bookable" there is (bookings.eligible_for over availability.offered).
            eligible, booked_rows = bookings.eligible_for(
                db,
                settings=settings,
                now=row.now,
                hours=hours,
                starts_at=new.starts_at,
                duration=row.duration_minutes,
                buffer_minutes=row.buffer_minutes,
                exclude=replaced,
            )
            if not eligible:
                raise ApiError(409, "slot_unavailable")
            day = new.starts_at.astimezone(ZoneInfo(settings.timezone)).date()
            worker_id = bookings.by_load(eligible, booked_rows, day, settings.timezone)[0]
            try:
                with db.begin_nested():
                    hold = db.execute(
                        INSERT_HOLD,
                        {
                            "service_id": service_id,
                            "worker_id": worker_id,
                            "anyone": new.member_id is None,
                            "starts_at": new.starts_at,
                            "duration_minutes": row.duration_minutes,
                            "email": new.email,
                            "locale": new.locale,
                            "secret_hash": hashlib.sha256(secret.encode()).digest(),
                            "hold_ttl": bookings.HOLD_TTL,
                        },
                    ).one()
            except IntegrityError as error:
                # The worker was REMOVED meanwhile (bookings.place's B5): this time went away.
                if isinstance(error.orig, ForeignKeyViolation):
                    raise ApiError(409, "slot_unavailable") from None
                raise
            if replaced is not None:
                db.execute(DELETE_HOLD, {"id": replaced})
            jobs.enqueue(
                db,
                "email.booking_verify",
                f"email.booking_verify:{tenant_id}:{hold.id}",
                {"hold_id": str(hold.id)},
                tenant_id=tenant_id,
            )
    except OperationalError:
        raise ApiError(503, "busy") from None
    logger.info("booking hold placed", extra={"hold_id": str(hold.id)})
    return HoldOut(starts_at=hold.starts_at, expires_at=hold.expires_at, secret=secret)


class ReleaseIn(BaseModel):
    model_config = STRICT
    secret: Annotated[str, StringConstraints(max_length=64)]


@router.post(
    "/businesses/{tenant_id}/holds/release",
    name="release",
    status_code=204,
    responses={s: {"model": Error} for s in (415, 422, 429, 503)},
)
def release(tenant_id: UUID, body: ReleaseIn, request: Request) -> None:
    """ "Choose another time" (design §2): the page that placed a hold lets its time go, and its
    link dies with it. Only that page's secret does anything; every answer is the same 204, so it
    says nothing about which secrets exist."""
    ip = request.client.host if request.client else None
    if limits.hit({limits.ip_key("booking_hold_release", ip): WRITE_LIMIT}, WRITE_WINDOW):
        raise ApiError(429, "rate_limited")
    request.state.tenant_id = tenant_id
    hold_hash = hashlib.sha256(body.secret.encode()).digest()
    try:
        with tenant_context(tenant_id) as db:
            db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})  # first, always (0034)
            db.execute(
                text("DELETE FROM booking_holds WHERE secret_hash = :hash"), {"hash": hold_hash}
            )
    except OperationalError:
        raise ApiError(503, "busy") from None


link_router = APIRouter(prefix="/api/public/booking-hold", tags=["booking-holds"])


@link_router.post("", name="read", responses={s: {"model": Error} for s in (404, 415, 422, 429)})
def read(body: TokenIn, request: Request, response: Response) -> HoldView:
    """The held booking behind an emailed link, for the confirm page. Writes nothing (no lock, no
    update): opening the link, or a mail scanner fetching the page, changes nothing."""
    tenant_id, token_hash = booking_links._enter(
        request, "booking_hold_read", READ_LIMIT, READ_WINDOW, token=body.token
    )
    response.headers["Cache-Control"] = "no-store"
    with tenant_context(tenant_id) as db:
        hold = db.execute(LIVE, {"hash": token_hash, "link_ttl": bookings.LINK_TTL}).first()
        if hold is None:
            raise ApiError(404, "link_expired")
        view = db.execute(VIEW, {"service_id": hold.service_id, "worker_id": hold.worker_id}).one()
        settings = business_settings.read(db)
    return HoldView(
        business=view.business,
        slug=view.slug,
        timezone=settings.timezone,
        language=settings.language,
        service_id=hold.service_id,
        service_name=view.service_name,
        starts_at=hold.starts_at,
        ends_at=hold.ends_at,
        price=Price(amount_minor=view.price_amount_minor, currency=view.price_currency),
        worker_display_name=view.worker_display_name,
        cancellation=CancellationOut(
            text=settings.cancellation_policy_text or None,
            free_cancellation_hours=settings.free_cancellation_hours,
            reschedule_cutoff_hours=settings.reschedule_cutoff_hours,
            max_reschedules=settings.max_reschedules,
        ),
        auto_confirm=settings.auto_confirm,
        policy_version=clients.current_policy_version(),
        held_until=hold.expires_at,
        email=hold.email,
    )


@link_router.post(
    "/confirm",
    name="confirm",
    status_code=201,
    responses={s: {"model": Error} for s in (404, 409, 415, 422, 429, 503)},
)
def confirm(body: ConfirmIn, request: Request, response: Response) -> bookings.BookingOut:
    """The click that books: the held time, the hold's address, the name (and phone) typed now.
    Re-derived like any booking, so a click after the 15-minute hold still books a time that is
    free (409 if not). Anything refused rolls back whole, and the link keeps working until it
    expires."""
    tenant_id, token_hash = booking_links._enter(
        request, "booking_hold_write", WRITE_LIMIT, WRITE_WINDOW, token=body.token
    )
    response.headers["Cache-Control"] = "no-store"
    origin_ip, user_agent = auth.origin(request)
    try:
        with tenant_context(tenant_id) as db:
            db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})  # first, always (0034)
            hold = db.execute(LIVE, {"hash": token_hash, "link_ttl": bookings.LINK_TTL}).first()
            if hold is None:
                raise ApiError(404, "link_expired")
            booked = bookings.book_online(
                db,
                tenant_id=tenant_id,
                service_id=hold.service_id,
                # A named worker never changes; "anyone" may land on another free worker.
                member_id=None if hold.anyone else hold.worker_id,
                starts_at=hold.starts_at,
                name=body.name,
                address=hold.email,
                phone=body.phone,
                locale=hold.locale,
                # Opening the emailed link proves the address as a verified account email does:
                # what they type now is what they own (app.clients.find_or_create).
                user_id=None,
                refresh=True,
                policy_version=body.policy_version,
                consents=body.consents,
                origin_ip=origin_ip,
                user_agent=user_agent,
                exclude=hold.id,
                prefer=hold.worker_id,
            )
            # Last: deleting first would lock the hold row before place()'s foreign-key wait on
            # the membership, which a member removal's cascade onto this row could deadlock.
            db.execute(DELETE_HOLD, {"id": hold.id})
    except OperationalError:
        raise ApiError(503, "busy") from None
    logger.info("booking confirmed by link", extra={"booking_id": str(booked.id)})
    return booked
