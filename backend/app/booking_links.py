"""ZIF-54: a client views, cancels or reschedules one booking from the link in their booking
emails, with no account.

D3/D4. The link is `{app_url}/{locale}/booking#{tenant_id}.{token}`, the invite shape: the
fragment never reaches a server. The page exchanges it for a cookie at POST /session; every other
route here authenticates from that cookie alone. Every route parses the token the same way
(`UUID(tenant)` plus `invites.SECRET.fullmatch`), opens `tenant_context(tenant)` and looks the
SHA-256 hash up under that tenant's row-level security. Every failure is the SAME 404
`link_expired`: bad shape, unknown tenant, unknown hash, not live, or no cookie at all -- one
oracle, not several (D4, test 6).

D2. Liveness is derived, never stored: `resolve()` is the one place that predicate lives.

D5/D6. The id is an assertion only (a stale cookie vs a fresh body is `409 link_changed`); the
cancel and reschedule echoes (`refund_pct`, `reschedule_count`) guard a stale click against a term
or a count that has since moved (`409 terms_changed` / `409 changed`).
"""

import hashlib
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request, Response
from psycopg.errors import ExclusionViolation
from pydantic import BaseModel, Field
from sqlalchemy import Row, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import (
    auth,
    availability,
    bookings,
    business_settings,
    cancellation,
    clients,
    invites,
    limits,
    schedule,
)
from app.bookings import CLIENT_CANCEL, INSERT_EVENT, RESCHEDULE
from app.cancellation import Decision
from app.clients import Purpose
from app.db import tenant_context
from app.errors import ApiError, Error
from app.services import STRICT, Price
from app.time_off import Instant

router = APIRouter(prefix="/api/public/booking-link", tags=["booking-link"])
# Spec S3: never the token, its hash, the client id or an address in a log line.
logger = logging.getLogger(__name__)

COOKIE = "__Host-booking-link"
COOKIE_MAX_AGE = 3600

OPEN_LIMIT, OPEN_WINDOW = 10, timedelta(minutes=15)
READ_LIMIT, READ_WINDOW = 60, timedelta(minutes=1)
WRITE_LIMIT, WRITE_WINDOW = 20, timedelta(hours=1)

MAX_DAYS = 14  # the picker loads a week at a time, same bound as availability.MAX_DAYS


def _parse(token: str) -> tuple[UUID, str]:
    """The token's tenant and secret, or 404 link_expired for any shape that isn't exactly what
    the worker mints (D4): never a distinct code for a distinct failure (test 6)."""
    tenant_part, _, secret = token.partition(".")
    try:
        tenant_id = UUID(tenant_part)
    except ValueError:
        raise ApiError(404, "link_expired") from None
    if not invites.SECRET.fullmatch(secret):
        raise ApiError(404, "link_expired")
    return tenant_id, secret


def _cookie_token(request: Request) -> tuple[UUID, str]:
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise ApiError(404, "link_expired")
    return _parse(raw)


RESOLVE = text("""
SELECT b.id AS booking_id, b.status, b.starts_at, b.ends_at, b.original_starts_at,
       b.earliest_starts_at, b.reschedule_count, b.max_reschedules, b.free_cancellation_hours,
       b.reschedule_cutoff_hours, b.service_id, b.worker_id, b.worker_display_name,
       b.service_name, b.price_amount_minor, b.price_currency, b.cancellation_policy_text,
       b.client_id, t.name AS business, now() AS now
FROM booking_links l
JOIN bookings b ON b.tenant_id = l.tenant_id AND b.id = l.booking_id
JOIN tenants t ON t.id = b.tenant_id
WHERE l.token_hash = :hash
  -- D2, verbatim: live only while the appointment hasn't started and the booking is still
  -- confirmed, or still a pending that hasn't expired. Merchant cancel/decline, client cancel,
  -- expiry and the appointment starting all revoke every link of this booking at once, with no
  -- write here at all -- this predicate is the only thing that changes.
  AND b.starts_at > now()
  AND (b.status = 'confirmed' OR (b.status = 'pending' AND b.expires_at > now()))
""")


def resolve(db: Session, token_hash: bytes) -> Row[Any] | None:
    """The live booking a link's hash names, or None (D2/D4: the caller answers 404 link_expired,
    the only failure code this whole feature has)."""
    return db.execute(RESOLVE, {"hash": token_hash}).first()


class BookingOut(BaseModel):
    id: UUID
    status: str
    business: str
    timezone: str
    starts_at: datetime
    ends_at: datetime
    original_starts_at: datetime | None
    service_name: dict[str, str]
    price: Price
    worker_display_name: str | None
    cancellation_policy_text: str | None


class EngineOut(BaseModel):
    can_cancel: bool
    can_reschedule: bool
    refund_pct: int
    copy_key: str
    reschedule_count: int
    reschedules_left: int


class LinkedBooking(BaseModel):
    """Never the client's name, email, phone or ids, nor the worker id, source or events: the
    link is a bearer credential (spec S4, test 12)."""

    booking: BookingOut
    engine: EngineOut
    pending_consents: list[Purpose]


CONSENT_EVENT = text("""
SELECT policy_version, consent_purposes, created_at
FROM booking_events WHERE tenant_id = current_setting('app.tenant_id')::uuid
  AND booking_id = :id AND event = 'created'
""")

ALREADY_CONFIRMED = text("""
SELECT true FROM booking_events
WHERE tenant_id = current_setting('app.tenant_id')::uuid
  AND booking_id = :id AND event = 'consent_confirmed'
""")

# D11. A purpose's grant is overridden by a LATER withdrawal: `consents` rows whose created_at is
# after the `created` event's own created_at. Both columns default to clock_timestamp() (0024,
# 0026), so this compares insertion clocks, not transaction clocks.
WITHDRAWN_SINCE = text("""
SELECT DISTINCT purpose FROM consents
WHERE tenant_id = current_setting('app.tenant_id')::uuid
  AND client_id = :client_id AND granted = false AND created_at > :since
""")


def pending_consents(db: Session, booking_id: UUID, client_id: UUID) -> list[Purpose]:
    """The purposes still worth asking to confirm: granted at booking, not since withdrawn, and
    nothing to ask again once the booking has already been confirmed once (D11)."""
    if db.execute(ALREADY_CONFIRMED, {"id": booking_id}).first() is not None:
        return []
    event = db.execute(CONSENT_EVENT, {"id": booking_id}).first()
    if event is None or not event.consent_purposes:
        return []
    granted = {p for p, was_granted in event.consent_purposes.items() if was_granted}
    if not granted:
        return []
    withdrawn = set(
        db.execute(WITHDRAWN_SINCE, {"client_id": client_id, "since": event.created_at}).scalars()
    )
    return sorted(granted - withdrawn)


def _decide(row: object, policy: cancellation.Policy) -> Decision:
    decision = cancellation.decide(
        policy,
        row.starts_at,  # type: ignore[attr-defined]
        row.now,  # type: ignore[attr-defined]
        reschedule_count=row.reschedule_count,  # type: ignore[attr-defined]
        original_starts_at=row.original_starts_at,  # type: ignore[attr-defined]
        earliest_starts_at=row.earliest_starts_at,  # type: ignore[attr-defined]
    )
    if row.status == "pending":  # type: ignore[attr-defined]
        # D7/route rule: nothing has been paid on a pending booking, so there is nothing to
        # refund, and it is never reschedulable (D7: a pending booking is never rescheduled).
        decision = Decision(decision.can_cancel, False, 0, 0, "pending", 0)
    return decision


def _policy(row: object) -> cancellation.Policy:
    return cancellation.Policy(
        row.free_cancellation_hours,  # type: ignore[attr-defined]
        row.reschedule_cutoff_hours,  # type: ignore[attr-defined]
        row.max_reschedules,  # type: ignore[attr-defined]
    )


def _view(row: object, decision: Decision, zone: str, consents: list[Purpose]) -> LinkedBooking:
    return LinkedBooking(
        booking=BookingOut(
            id=row.booking_id,  # type: ignore[attr-defined]
            status=row.status,  # type: ignore[attr-defined]
            business=row.business,  # type: ignore[attr-defined]
            timezone=zone,
            starts_at=row.starts_at,  # type: ignore[attr-defined]
            ends_at=row.ends_at,  # type: ignore[attr-defined]
            original_starts_at=row.original_starts_at,  # type: ignore[attr-defined]
            service_name=row.service_name,  # type: ignore[attr-defined]
            price=Price(amount_minor=row.price_amount_minor, currency=row.price_currency),  # type: ignore[attr-defined]
            worker_display_name=row.worker_display_name,  # type: ignore[attr-defined]
            cancellation_policy_text=row.cancellation_policy_text,  # type: ignore[attr-defined]
        ),
        engine=EngineOut(
            can_cancel=decision.can_cancel,
            can_reschedule=decision.can_reschedule,
            refund_pct=decision.refund_pct,
            copy_key=decision.copy_key,
            reschedule_count=row.reschedule_count,  # type: ignore[attr-defined]
            reschedules_left=decision.reschedules_left,
        ),
        pending_consents=consents,
    )


class TokenIn(BaseModel):
    model_config = STRICT
    token: str


@router.post(
    "/session",
    status_code=204,
    name="session",
    responses={s: {"model": Error} for s in (404, 415, 422, 429)},
)
def open_session(body: TokenIn, request: Request, response: Response) -> None:
    """Exchange the emailed token for the manage cookie. Writes nothing (D3, tests 8/9): not even
    the consent confirmation click, which is its own explicit POST (D11)."""
    ip = request.client.host if request.client else None
    # Per IP, before any hashing or lookup (D4, test 28): a stranger with a guessed shape must not
    # get free hash attempts past the limit.
    if limits.hit({limits.ip_key("booking_link_open", ip): OPEN_LIMIT}, OPEN_WINDOW):
        raise ApiError(429, "rate_limited")
    tenant_id, secret = _parse(body.token)
    request.state.tenant_id = tenant_id
    with tenant_context(tenant_id) as db:
        if resolve(db, hashlib.sha256(secret.encode()).digest()) is None:
            raise ApiError(404, "link_expired")
    response.set_cookie(
        COOKIE,
        body.token,
        max_age=COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


@router.get("/", name="read", responses={s: {"model": Error} for s in (404, 429)})
def read(request: Request) -> LinkedBooking:
    ip = request.client.host if request.client else None
    if limits.hit({limits.ip_key("booking_link_read", ip): READ_LIMIT}, READ_WINDOW):
        raise ApiError(429, "rate_limited")
    tenant_id, secret = _cookie_token(request)
    request.state.tenant_id = tenant_id
    with tenant_context(tenant_id) as db:
        row = resolve(db, hashlib.sha256(secret.encode()).digest())
        if row is None:
            raise ApiError(404, "link_expired")
        zone = business_settings.read(db).timezone
        decision = _decide(row, _policy(row))
        consents = pending_consents(db, row.booking_id, row.client_id)
    return _view(row, decision, zone, consents)


class AvailabilityOut(BaseModel):
    timezone: str
    slots: list[datetime]


def _worker_hours(db: Session, service_id: UUID, worker_id: UUID) -> dict[UUID, list[schedule.Row]]:
    """This booking's worker's working hours for this service, exactly the CANDIDATES shape
    create() reads, filtered to one member so an unassigned or removed worker yields none."""
    out: dict[UUID, list[schedule.Row]] = defaultdict(list)
    for member_id, _display_name, weekday, starts_at, ends_at in db.execute(
        bookings.CANDIDATES, {"service_id": service_id, "member_id": worker_id}
    ).tuples():
        out[member_id].append((weekday, starts_at, ends_at))
    return out


@router.get(
    "/availability", name="availability", responses={s: {"model": Error} for s in (404, 422, 429)}
)
def read_availability(
    request: Request,
    from_: Annotated[availability.Day, Query(alias="from")],
    to: availability.Day,
) -> AvailabilityOut:
    ip = request.client.host if request.client else None
    if limits.hit({limits.ip_key("booking_link_read", ip): READ_LIMIT}, READ_WINDOW):
        raise ApiError(429, "rate_limited")
    if to < from_ or (to - from_).days >= MAX_DAYS:
        raise ApiError(422, "invalid_range")
    tenant_id, secret = _cookie_token(request)
    request.state.tenant_id = tenant_id
    with tenant_context(tenant_id) as db:
        row = resolve(db, hashlib.sha256(secret.encode()).digest())
        if row is None or row.status != "confirmed":
            # A pending booking has no slot to move into yet (D7): the picker has nothing to show.
            raise ApiError(404, "link_expired")
        settings = business_settings.read(db)
        opening = schedule.envelope(db)
        hours = _worker_hours(db, row.service_id, row.worker_id)
        zone = settings.timezone
        first, last, earliest = availability.window(
            availability.now(),
            zone,
            from_,
            to,
            settings.min_notice_minutes,
            settings.booking_horizon_days,
        )
        duration = (row.ends_at - row.starts_at) // timedelta(minutes=1)
        service = db.execute(
            text("SELECT buffer_minutes FROM services WHERE id = :id"), {"id": row.service_id}
        ).first()
        buffer_minutes = service.buffer_minutes if service else None
        result = availability.offered(
            db,
            members=[row.worker_id],
            hours=hours,
            opening=opening,
            settings=settings,
            first=first,
            last=last,
            earliest=earliest,
            duration=duration,
            buffer=availability.buffer_for(duration, buffer_minutes, settings.buffer_pct),
            exclude=row.booking_id,
        )
    return AvailabilityOut(timezone=zone, slots=sorted(result.slots.get(row.worker_id, set())))


class CancelIn(BaseModel):
    model_config = STRICT
    booking_id: Annotated[UUID, Field(strict=False)]
    refund_pct: int


@router.post(
    "/cancel",
    name="cancel",
    responses={s: {"model": Error} for s in (404, 409, 415, 422, 429, 503)},
)
def cancel(body: CancelIn, request: Request) -> LinkedBooking:
    ip = request.client.host if request.client else None
    if limits.hit({limits.ip_key("booking_link_write", ip): WRITE_LIMIT}, WRITE_WINDOW):
        raise ApiError(429, "rate_limited")
    tenant_id, secret = _cookie_token(request)
    request.state.tenant_id = tenant_id
    origin_ip, user_agent = auth.origin(request)
    try:
        with tenant_context(tenant_id) as db:
            db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})  # first statement, always
            row = resolve(db, hashlib.sha256(secret.encode()).digest())
            if row is None:
                raise ApiError(404, "link_expired")
            if body.booking_id != row.booking_id:
                raise ApiError(409, "link_changed")
            decision = _decide(row, _policy(row))
            if not decision.can_cancel:
                raise ApiError(409, "not_allowed")
            if body.refund_pct != decision.refund_pct:
                raise ApiError(409, "terms_changed")
            changed = db.execute(CLIENT_CANCEL, {"id": row.booking_id}).first()
            if changed is None:  # lost a race under the same lock: belt and suspenders on D2
                raise ApiError(404, "link_expired")
            db.execute(
                INSERT_EVENT,
                {
                    "booking_id": row.booking_id,
                    "event": "cancelled_by_client",
                    "ip": origin_ip,
                    "user_agent": user_agent,
                    "policy_version": None,
                    "consent_purposes": None,
                },
            )
            bookings._email(db, tenant_id, row.booking_id, "booking_cancelled_by_client")
            bookings._email_merchants(
                db, tenant_id, row.booking_id, row.worker_id, "booking_client_cancelled"
            )
            zone = business_settings.read(db).timezone
    except OperationalError:
        raise ApiError(503, "busy") from None
    logger.info("booking cancelled by client", extra={"booking_id": str(row.booking_id)})
    # The final state (spec S4): no longer live, so no more consent to ask and nothing further
    # allowed -- the response carries what just happened, and every later call answers 404.
    final_row = SimpleNamespace(**{**dict(row._mapping), "status": changed.status})
    final = Decision(False, False, decision.refund_pct, 0, decision.copy_key, 0)
    return _view(final_row, final, zone, [])


class RescheduleIn(BaseModel):
    model_config = STRICT
    booking_id: Annotated[UUID, Field(strict=False)]
    starts_at: Instant
    reschedule_count: int


@router.post(
    "/reschedule",
    name="reschedule",
    responses={s: {"model": Error} for s in (404, 409, 415, 422, 429, 503)},
)
def reschedule(body: RescheduleIn, request: Request) -> LinkedBooking:
    ip = request.client.host if request.client else None
    if limits.hit({limits.ip_key("booking_link_write", ip): WRITE_LIMIT}, WRITE_WINDOW):
        raise ApiError(429, "rate_limited")
    tenant_id, secret = _cookie_token(request)
    request.state.tenant_id = tenant_id
    origin_ip, user_agent = auth.origin(request)
    try:
        with tenant_context(tenant_id) as db:
            db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})  # first statement, always
            row = resolve(db, hashlib.sha256(secret.encode()).digest())
            if row is None:
                raise ApiError(404, "link_expired")
            if body.booking_id != row.booking_id:
                raise ApiError(409, "link_changed")
            decision = _decide(row, _policy(row))
            if row.status != "confirmed" or not decision.can_reschedule:
                raise ApiError(409, "not_allowed")
            if body.reschedule_count != row.reschedule_count:
                raise ApiError(409, "changed")
            # D9: re-derive through the same pipeline create() uses. FOR SHARE the service, so a
            # concurrent archive can't race this the way create()'s own step 2 guards against it.
            service = db.execute(
                text("""
                SELECT duration_minutes, buffer_minutes FROM services
                WHERE id = :service_id AND archived_at IS NULL
                FOR SHARE
                """),
                {"service_id": row.service_id},
            ).first()
            if service is None:
                raise ApiError(409, "slot_unavailable")
            hours = _worker_hours(db, row.service_id, row.worker_id)
            if row.worker_id not in hours:  # unassigned, or removed from the service
                raise ApiError(409, "slot_unavailable")
            settings = business_settings.read(db)
            zone = settings.timezone
            duration = (row.ends_at - row.starts_at) // timedelta(minutes=1)
            day = body.starts_at.astimezone(ZoneInfo(zone)).date()
            first, last, earliest = availability.window(
                row.now, zone, day, day, settings.min_notice_minutes, settings.booking_horizon_days
            )
            if first > last:
                raise ApiError(409, "slot_unavailable")
            opening = schedule.envelope(db)
            result = availability.offered(
                db,
                members=[row.worker_id],
                hours=hours,
                opening=opening,
                settings=settings,
                first=first,
                last=last,
                earliest=earliest,
                duration=duration,
                buffer=availability.buffer_for(
                    duration, service.buffer_minutes, settings.buffer_pct
                ),
                exclude=row.booking_id,
            )
            if body.starts_at not in result.slots.get(row.worker_id, set()):
                raise ApiError(409, "slot_unavailable")
            try:
                changed = db.execute(
                    RESCHEDULE,
                    {"id": row.booking_id, "new": body.starts_at, "seen": body.reschedule_count},
                ).first()
            except IntegrityError as error:
                if (
                    isinstance(error.orig, ExclusionViolation)
                    and error.orig.diag.constraint_name == bookings.OVERLAP
                ):
                    raise ApiError(409, "slot_taken") from None
                raise
            if changed is None:  # the echo was stale under our own lock: the CHECK is the backstop
                raise ApiError(409, "changed")
            db.execute(
                INSERT_EVENT,
                {
                    "booking_id": row.booking_id,
                    "event": "rescheduled",
                    "ip": origin_ip,
                    "user_agent": user_agent,
                    "policy_version": None,
                    "consent_purposes": None,
                },
            )
            suffix = f":r{changed.reschedule_count}"
            new_iso = changed.starts_at.isoformat()
            bookings._email(
                db,
                tenant_id,
                row.booking_id,
                "booking_confirmed",
                key_suffix=suffix,
                extra={"starts_at": new_iso},
            )
            bookings._email_merchants(
                db,
                tenant_id,
                row.booking_id,
                row.worker_id,
                "booking_client_rescheduled",
                key_suffix=suffix,
                extra={"starts_at": new_iso, "previous_starts_at": row.starts_at.isoformat()},
            )
            bookings._remind(db, tenant_id, row.booking_id, changed.starts_at, row.now)
            new_earliest = min(row.earliest_starts_at or row.starts_at, changed.starts_at)
            new_decision = cancellation.decide(
                _policy(row),
                changed.starts_at,
                row.now,
                reschedule_count=changed.reschedule_count,
                original_starts_at=row.original_starts_at,
                earliest_starts_at=new_earliest,
            )
            consents = pending_consents(db, row.booking_id, row.client_id)
    except OperationalError:
        raise ApiError(503, "busy") from None
    logger.info("booking rescheduled", extra={"booking_id": str(row.booking_id)})
    new_row = SimpleNamespace(
        **{
            **dict(row._mapping),
            "status": "confirmed",
            "starts_at": changed.starts_at,
            "ends_at": changed.ends_at,
            "reschedule_count": changed.reschedule_count,
            "earliest_starts_at": new_earliest,
        }
    )
    return _view(new_row, new_decision, zone, consents)


class ConsentsIn(BaseModel):
    model_config = STRICT
    booking_id: Annotated[UUID, Field(strict=False)]


@router.post(
    "/consents",
    name="consents",
    responses={s: {"model": Error} for s in (404, 409, 415, 422, 429)},
)
def confirm_consents(body: ConsentsIn, request: Request) -> LinkedBooking:
    """D11: the explicit confirmation click, never the link click itself. Confirms the grants
    ticked at booking (the `created` event's true values), minus any later withdrawal, and records
    them with `clients.record_consents(source='booking_page', ...)`. Runs once per booking: a
    second click finds nothing pending and changes nothing (idempotent, test 26)."""
    ip = request.client.host if request.client else None
    if limits.hit({limits.ip_key("booking_link_write", ip): WRITE_LIMIT}, WRITE_WINDOW):
        raise ApiError(429, "rate_limited")
    tenant_id, secret = _cookie_token(request)
    request.state.tenant_id = tenant_id
    origin_ip, user_agent = auth.origin(request)
    with tenant_context(tenant_id) as db:
        row = resolve(db, hashlib.sha256(secret.encode()).digest())
        if row is None:
            raise ApiError(404, "link_expired")
        if body.booking_id != row.booking_id:
            raise ApiError(409, "link_changed")
        event = db.execute(CONSENT_EVENT, {"id": row.booking_id}).first()
        purposes = pending_consents(db, row.booking_id, row.client_id)
        if not purposes or event is None:
            raise ApiError(409, "nothing_to_confirm")
        clients.record_consents(
            db,
            client_id=row.client_id,
            policy_version=event.policy_version,
            purposes=dict.fromkeys(purposes, True),
            source="booking_page",
            ip=origin_ip,
            user_agent=user_agent,
        )
        db.execute(
            INSERT_EVENT,
            {
                "booking_id": row.booking_id,
                "event": "consent_confirmed",
                "ip": origin_ip,
                "user_agent": user_agent,
                "policy_version": None,
                "consent_purposes": None,
            },
        )
        zone = business_settings.read(db).timezone
        decision = _decide(row, _policy(row))
    return _view(row, decision, zone, [])
