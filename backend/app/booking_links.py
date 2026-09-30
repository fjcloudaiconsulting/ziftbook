"""ZIF-54: a client views, cancels or reschedules one booking from their emailed link, no account.
Every failure to find a live link is the SAME 404 `link_expired` (D4, test 6).
"""

import hashlib
import json
import logging
from datetime import UTC, date, datetime, timedelta
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
from app.business_settings import BusinessSettings
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


def _parse(token: str) -> tuple[UUID, bytes]:
    """The token's tenant and its secret's SHA-256 (the booking_links key), or 404 link_expired
    for any shape that isn't exactly what the worker mints (D4): never a distinct code for a
    distinct failure (test 6)."""
    tenant_part, _, secret = token.partition(".")
    try:
        tenant_id = UUID(tenant_part)
    except ValueError:
        raise ApiError(404, "link_expired") from None
    if not invites.SECRET.fullmatch(secret):
        raise ApiError(404, "link_expired")
    return tenant_id, hashlib.sha256(secret.encode()).digest()


def _enter(
    request: Request,
    key: str,
    limit: int,
    window: timedelta,
    token: str | None = None,
    refuse: ApiError | None = None,
) -> tuple[UUID, bytes]:
    """Per IP, before any hashing or lookup (D4, test 28): a stranger with a guessed shape must not
    get free hash attempts past the limit. Then `refuse`, if any, then the body's token or else the
    cookie's (a missing cookie parses like any other bad shape)."""
    ip = request.client.host if request.client else None
    if limits.hit({limits.ip_key(key, ip): limit}, window):
        raise ApiError(429, "rate_limited")
    if refuse is not None:
        raise refuse
    tenant_id, token_hash = _parse(token if token is not None else request.cookies.get(COOKIE, ""))
    request.state.tenant_id = tenant_id
    return tenant_id, token_hash


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


def resolve(db: Session, token_hash: bytes) -> Row[Any]:
    """The live booking a link's hash names, or 404 link_expired (D2/D4: the only failure code this
    whole feature has)."""
    row = db.execute(RESOLVE, {"hash": token_hash}).first()
    if row is None:
        raise ApiError(404, "link_expired")
    return row


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
    # The last moment a cancel still refunds in full: the refund anchor (the earliest start the
    # booking ever held, D7) minus free_cancellation_hours. None once no refund is left, and for a
    # pending or no-longer-cancellable booking.
    free_until: datetime | None


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


def _decide(row: Any, policy: cancellation.Policy) -> Decision:
    decision = cancellation.decide(
        policy,
        row.starts_at,
        row.now,
        reschedule_count=row.reschedule_count,
        original_starts_at=row.original_starts_at,
        earliest_starts_at=row.earliest_starts_at,
    )
    if row.status == "pending":
        # D7/route rule: nothing has been paid on a pending booking, so there is nothing to
        # refund, and it is never reschedulable (D7: a pending booking is never rescheduled).
        decision = Decision(decision.can_cancel, False, 0, 0, "pending", 0)
    return decision


def _policy(row: Any) -> cancellation.Policy:
    return cancellation.Policy(
        row.free_cancellation_hours,
        row.reschedule_cutoff_hours,
        row.max_reschedules,
    )


def _event(
    db: Session,
    booking_id: UUID,
    event: str,
    ip: str | None,
    user_agent: str | None,
    details: dict[str, str] | None = None,
) -> None:
    db.execute(
        INSERT_EVENT,
        {
            "booking_id": booking_id,
            "event": event,
            "ip": ip,
            "user_agent": user_agent,
            "policy_version": None,
            "consent_purposes": None,
            "actor_user_id": None,  # always the client, who has no account here
            "details": None if details is None else json.dumps(details),
        },
    )


def _view(row: Any, decision: Decision, zone: str, consents: list[Purpose]) -> LinkedBooking:
    return LinkedBooking(
        booking=BookingOut(
            id=row.booking_id,
            status=row.status,
            business=row.business,
            timezone=zone,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            original_starts_at=row.original_starts_at,
            service_name=row.service_name,
            price=Price(amount_minor=row.price_amount_minor, currency=row.price_currency),
            worker_display_name=row.worker_display_name,
            cancellation_policy_text=row.cancellation_policy_text,
        ),
        engine=EngineOut(
            can_cancel=decision.can_cancel,
            can_reschedule=decision.can_reschedule,
            refund_pct=decision.refund_pct,
            copy_key=decision.copy_key,
            reschedule_count=row.reschedule_count,
            reschedules_left=decision.reschedules_left,
            free_until=(
                (row.earliest_starts_at or row.starts_at)
                - timedelta(hours=row.free_cancellation_hours)
                if decision.can_cancel and decision.refund_pct
                else None
            ),
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
    tenant_id, token_hash = _enter(
        request, "booking_link_open", OPEN_LIMIT, OPEN_WINDOW, token=body.token
    )
    with tenant_context(tenant_id) as db:
        resolve(db, token_hash)
    response.set_cookie(
        COOKIE,
        body.token,
        max_age=COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


@router.get("", name="read", responses={s: {"model": Error} for s in (404, 429)})
def read(request: Request) -> LinkedBooking:
    tenant_id, token_hash = _enter(request, "booking_link_read", READ_LIMIT, READ_WINDOW)
    with tenant_context(tenant_id) as db:
        row = resolve(db, token_hash)
        zone = business_settings.read(db).timezone
        decision = _decide(row, _policy(row))
        consents = pending_consents(db, row.booking_id, row.client_id)
    return _view(row, decision, zone, consents)


class AvailabilityOut(BaseModel):
    timezone: str
    slots: list[datetime]


def _slots(
    db: Session,
    row: Any,
    settings: BusinessSettings,
    now: datetime,
    first: date,
    last: date,
    *,
    lock: bool,
) -> set[datetime] | None:
    """The starts this booking's worker could move it to on local days first..last, through the
    same pipeline create() uses (D9), or None once its service is archived. `lock` holds the
    service FOR SHARE, so a concurrent archive can't race a reschedule."""
    service = db.execute(
        text(
            "SELECT buffer_minutes FROM services WHERE id = :id AND archived_at IS NULL"
            + (" FOR SHARE" if lock else "")
        ),
        {"id": row.service_id},
    ).first()
    if service is None:
        return None
    # Filtered to this worker: an unassigned or removed worker has no hours, so no slots.
    hours, _names = availability.candidates(db, row.service_id, row.worker_id)
    first, last, earliest = availability.window(
        now,
        settings.timezone,
        first,
        last,
        settings.min_notice_minutes,
        settings.booking_horizon_days,
    )
    duration = (row.ends_at - row.starts_at) // timedelta(minutes=1)
    result = availability.offered(
        db,
        members=[row.worker_id],
        hours=hours,
        opening=schedule.envelope(db),
        settings=settings,
        first=first,
        last=last,
        earliest=earliest,
        duration=duration,
        buffer=availability.buffer_for(duration, service.buffer_minutes, settings.buffer_pct),
        exclude=row.booking_id,
    )
    return result.slots.get(row.worker_id, set())


@router.get(
    "/availability", name="availability", responses={s: {"model": Error} for s in (404, 422, 429)}
)
def read_availability(
    request: Request,
    from_: Annotated[availability.Day, Query(alias="from")],
    to: availability.Day,
) -> AvailabilityOut:
    bad_range = to < from_ or (to - from_).days >= availability.MAX_DAYS
    tenant_id, token_hash = _enter(
        request,
        "booking_link_read",
        READ_LIMIT,
        READ_WINDOW,
        refuse=ApiError(422, "invalid_range") if bad_range else None,
    )
    with tenant_context(tenant_id) as db:
        row = resolve(db, token_hash)
        if row.status != "confirmed":
            # A pending booking has no slot to move into yet (D7): the picker has nothing to show.
            raise ApiError(404, "link_expired")
        settings = business_settings.read(db)
        # Nothing the reschedule would refuse (409 not_allowed / slot_unavailable): the page shows
        # "no times" rather than slots that can't be taken.
        slots = None
        if _decide(row, _policy(row)).can_reschedule:
            slots = _slots(db, row, settings, availability.now(), from_, to, lock=False)
    return AvailabilityOut(timezone=settings.timezone, slots=sorted(slots or ()))


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
    tenant_id, token_hash = _enter(request, "booking_link_write", WRITE_LIMIT, WRITE_WINDOW)
    origin_ip, user_agent = auth.origin(request)
    try:
        with tenant_context(tenant_id) as db:
            db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})  # first statement, always
            row = resolve(db, token_hash)
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
            _event(db, row.booking_id, "cancelled_by_client", origin_ip, user_agent)
            bookings.email(db, tenant_id, row.booking_id, "booking_cancelled_by_client")
            bookings.email_merchants(
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
    tenant_id, token_hash = _enter(request, "booking_link_write", WRITE_LIMIT, WRITE_WINDOW)
    origin_ip, user_agent = auth.origin(request)
    try:
        with tenant_context(tenant_id) as db:
            db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})  # first statement, always
            row = resolve(db, token_hash)
            if body.booking_id != row.booking_id:
                raise ApiError(409, "link_changed")
            decision = _decide(row, _policy(row))
            if row.status != "confirmed" or not decision.can_reschedule:
                raise ApiError(409, "not_allowed")
            if body.reschedule_count != row.reschedule_count:
                raise ApiError(409, "changed")
            settings = business_settings.read(db)
            zone = settings.timezone
            day = body.starts_at.astimezone(ZoneInfo(zone)).date()
            slots = _slots(db, row, settings, row.now, day, day, lock=True)
            if slots is None or body.starts_at not in slots:
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
            _event(
                db,
                row.booking_id,
                "rescheduled",
                origin_ip,
                user_agent,
                # UTC ISO strings, whatever zone the session's driver hands back.
                {
                    "from": row.starts_at.astimezone(UTC).isoformat(),
                    "to": changed.starts_at.astimezone(UTC).isoformat(),
                },
            )
            suffix = f":r{changed.reschedule_count}"
            new_iso = changed.starts_at.isoformat()
            bookings.email(
                db,
                tenant_id,
                row.booking_id,
                "booking_confirmed",
                key_suffix=suffix,
                extra={"starts_at": new_iso},
            )
            bookings.email_merchants(
                db,
                tenant_id,
                row.booking_id,
                row.worker_id,
                "booking_client_rescheduled",
                key_suffix=suffix,
                extra={"starts_at": new_iso, "previous_starts_at": row.starts_at.isoformat()},
            )
            bookings.remind(db, tenant_id, row.booking_id, changed.starts_at, row.now)
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
    responses={s: {"model": Error} for s in (404, 409, 415, 422, 429, 503)},
)
def confirm_consents(body: ConsentsIn, request: Request) -> LinkedBooking:
    """D11: the explicit confirmation click, never the link click itself. Confirms the grants
    ticked at booking (the `created` event's true values), minus any later withdrawal, and records
    them with `clients.record_consents(source='booking_page', ...)`. Runs once per booking: a
    second click finds nothing pending and changes nothing (idempotent, test 26)."""
    tenant_id, token_hash = _enter(request, "booking_link_write", WRITE_LIMIT, WRITE_WINDOW)
    origin_ip, user_agent = auth.origin(request)
    try:
        with tenant_context(tenant_id) as db:
            db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})  # first statement, always
            row = resolve(db, token_hash)
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
            _event(db, row.booking_id, "consent_confirmed", origin_ip, user_agent)
            zone = business_settings.read(db).timezone
            decision = _decide(row, _policy(row))
    except OperationalError:
        raise ApiError(503, "busy") from None
    return _view(row, decision, zone, [])
