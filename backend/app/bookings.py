"""POST /api/public/businesses/{tenant_id}/services/{service_id}/bookings (ZIF-51): a client picks
a service and a free slot and books it as a guest.

The database - not this route - is what keeps two clients from holding the same worker at the same
time: migration 0026's ex_bookings_worker_overlap. Every writer of `bookings` must take
`pg_advisory_xact_lock(51, hashtext(tenant))` as its transaction's first statement, or an EXCLUDE
constraint's lack of a speculative-insertion path lets two concurrent identical inserts deadlock
each other (about 9% of losers, measured). See CONTRIBUTING.md "Bookings" and migration 0026's
comment on the constraint.
"""

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, Response
from psycopg.errors import ExclusionViolation, ForeignKeyViolation
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import Row, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app import (
    auth,
    availability,
    business_settings,
    clients,
    limits,
    members,
    passwords,
    schedule,
    turnstile,
)
from app import time_off as time_off_module
from app.business_settings import Locale
from app.clients import ClientName, Phone, PolicyVersion, Purpose
from app.db import SessionLocal, join_tenant, tenant_context
from app.errors import ApiError, Error
from app.services import STRICT, Price

IP_LIMIT, EMAIL_LIMIT, LIMIT_WINDOW = 30, 5, timedelta(hours=1)
# ZIF-5's ~24h for merchant approval. ZIF-54 may promote this to a setting; it is not one today.
PENDING_TTL = timedelta(hours=24)
# ZIF-51's own key in app/schedule.py's ticket-number advisory lock convention (OPENING_LOCK = 105):
# disjoint first arguments, so the two locks can never collide.
LOCK_KEY = 51
OVERLAP = "ex_bookings_worker_overlap"  # migration 0026; answered with 409 slot_taken


class BookingIn(BaseModel):
    # Field by field: nothing a client sends can name a business, a currency, a price, a status,
    # a source or a platform account. There is no user_id field here and never will be.
    model_config = STRICT  # app.services.STRICT: strict=True, extra="forbid"
    # time_off.Instant, NEVER a bare AwareDatetime. Two independent reasons, each fatal:
    #   * {"starts_at": "0001-01-01T00:00:00+00:00"} makes astimezone() raise OverflowError, which
    #     nothing catches, on the first PUBLIC WRITE path in the product - an unauthenticated 500
    #     with a stack trace. Instant's utc() checks 2000 <= year <= 2999 FIRST.
    #   * Under STRICT (strict=True) FastAPI validates in PYTHON mode, which refuses strings, so a
    #     bare AwareDatetime 422s EVERY booking. Instant carries Field(strict=False) and fixes both.
    starts_at: time_off_module.Instant  # the UTC instant the public GET offered, exactly
    # Also strict=False. BookingIn is the repo's first strict model with a UUID field.
    member_id: Annotated[UUID, Field(strict=False)] | None = None  # None: "anyone"
    name: ClientName  # app.clients.ClientName
    email: passwords.Email  # required: ZIF-53 has to reach this person
    phone: Phone | None = None
    locale: Locale | None = None
    policy_version: PolicyVersion  # the CONSENT wording version; validated by texts_for()
    consents: dict[Purpose, bool] = Field(default_factory=dict)
    turnstile_token: Annotated[str, StringConstraints(max_length=2048)] | None = None


class BookingOut(BaseModel):
    id: UUID
    status: str  # "pending" or "confirmed"
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int
    service_name: dict[str, str]  # all three locales, as snapshotted
    price: Price  # app.services.Price
    worker_id: UUID  # memberships.id, as the public GET already publishes
    worker_display_name: str | None
    cancellation_policy_text: str | None


@dataclass(frozen=True)
class Account:
    user_id: UUID
    email: str


def account(request: Request) -> Account | None:
    """The signed-in person and their VERIFIED account email, or None. Its own transaction, like
    app.auth.signed_in: the path's business is not this session's business, and users' policy shows
    a user only through a membership in the CURRENT tenant, so the email must be read under the
    session's own tenant and never under the path's.

    CALL IT AT STEP 0, BEFORE tenant_context IS OPENED, and let this transaction CLOSE first.
    app/auth.py:187-201 closes its own transaction before opening the endpoint's for the same
    reason. Calling it inside the booking transaction holds a SECOND pooled connection for the whole
    life of a transaction that is holding services FOR SHARE; at pool_size 5 with max_overflow 10
    against a 40-thread pool, about 15 concurrent bookings deadlock on CONNECTION CHECKOUT - a hang
    with no SQLSTATE to catch and nothing to map to a status code.
    """
    token = request.cookies.get(auth.COOKIE)
    if not token:
        return None
    with SessionLocal.begin() as session:
        row = session.execute(
            auth.RESOLVE,
            {"id_hash": auth.hash_token(token), "idle": auth.IDLE, "touch_every": auth.TOUCH_EVERY},
        ).first()
        if row is None:
            return None
        join_tenant(session, row.tenant_id)
        email = session.scalar(text("SELECT email FROM users WHERE id = :id"), {"id": row.user_id})
    return None if email is None else Account(row.user_id, email)


LOCK = text("SELECT pg_advisory_xact_lock(:key, hashtext(current_setting('app.tenant_id')))")

SERVICE = text("""
SELECT now() AS now, name, price_amount_minor, price_currency, duration_minutes, buffer_minutes
FROM services
WHERE id = :service_id AND archived_at IS NULL
FOR SHARE
""")

# The same statement read_availability runs (working_hours join service_workers join memberships),
# with an optional member_id filter added so an unassigned or unknown member_id yields no
# candidates in this one query, never a 404 (no membership probing).
CANDIDATES = text("""
SELECT w.member_id, m.display_name, w.weekday, w.starts_at, w.ends_at
FROM working_hours w JOIN service_workers s
  ON s.tenant_id = w.tenant_id AND s.member_id = w.member_id
JOIN memberships m ON m.tenant_id = w.tenant_id AND m.id = w.member_id
WHERE s.service_id = :service_id
  AND (CAST(:member_id AS uuid) IS NULL OR w.member_id = :member_id)
""")

TIME_OFF = text("""
SELECT member_id, starts_at, ends_at FROM time_off
WHERE member_id = ANY(CAST(:members AS uuid[]))
  AND starts_at < :end AND ends_at > :start
  AND starts_at > CAST(:start AS timestamptz) - interval '366 days'
""")

EXPIRE = text("""
UPDATE bookings SET status = 'expired'
WHERE status = ANY(CAST(:expiring AS text[])) AND expires_at <= :now
""")

PENDING_COUNT = text("""
SELECT count(*) FROM bookings
WHERE client_id = :client_id AND status = ANY(CAST(:expiring AS text[])) AND expires_at > :now
""")

INSERT_BOOKING = text("""
INSERT INTO bookings (
  tenant_id, client_id, worker_id, service_id, starts_at, ends_at, status, expires_at, source,
  service_name, price_amount_minor, price_currency, duration_minutes, cancellation_policy_text,
  auto_confirm_at_booking, worker_display_name)
VALUES (
  current_setting('app.tenant_id')::uuid, :client_id, :worker_id, :service_id,
  :starts_at, :starts_at + make_interval(mins => :duration_minutes), :status, :expires_at,
  :source, CAST(:service_name AS jsonb), :price_amount_minor, :price_currency, :duration_minutes,
  :cancellation_policy_text, :auto_confirm_at_booking, :worker_display_name)
RETURNING id, status, starts_at, ends_at
""")

INSERT_EVENT = text("""
INSERT INTO booking_events (tenant_id, booking_id, event, ip, user_agent, policy_version,
                            consent_purposes)
VALUES (current_setting('app.tenant_id')::uuid, :booking_id, 'created', CAST(:ip AS inet),
        :user_agent, :policy_version, CAST(:consent_purposes AS jsonb))
""")

router = APIRouter(prefix="/api/public", tags=["bookings"])


@router.post(
    "/businesses/{tenant_id}/services/{service_id}/bookings",
    name="create",
    status_code=201,
    responses={s: {"model": Error} for s in (403, 404, 409, 415, 422, 429, 503)},
)
def create(  # sync def: turnstile.verify's urlopen blocks, and runs in FastAPI's threadpool
    tenant_id: UUID, service_id: UUID, new: BookingIn, request: Request, response: Response
) -> BookingOut:
    """Book a free slot as a guest. Public: no session required; a cookie, if present, only decides
    whether the client record for the posted address is refreshed from what was typed
    (app.clients.find_or_create), and only when it belongs to the signed-in account."""
    signed_in_as = account(request)  # step 0: its own transaction, opened and closed here
    ip = request.client.host if request.client else None
    # 1. Per IP first, before any outbound call: Turnstile ahead of a limit would give an anonymous
    #    caller a free outbound HTTPS POST per request.
    if limits.hit({limits.ip_key("booking", ip): IP_LIMIT}, LIMIT_WINDOW):
        raise ApiError(429, "rate_limited")
    # 2. Turnstile, between the two limits.
    if not turnstile.verify(new.turnstile_token, ip):
        raise ApiError(403, "turnstile_failed")
    # 3. Per email LAST, and in its own hit() call: app/limits.py increments every key
    #    unconditionally and only then evaluates which one tripped, so a combined call before
    #    Turnstile would let a Turnstile-less script lock an address out cheaply.
    if limits.hit(
        {limits.email_key("booking", f"{tenant_id}:{new.email}"): EMAIL_LIMIT}, LIMIT_WINDOW
    ):
        raise ApiError(429, "rate_limited")
    request.state.tenant_id = tenant_id
    response.headers["Cache-Control"] = "no-store"
    origin_ip, user_agent = auth.origin(request)
    try:
        with tenant_context(tenant_id) as db:
            # 1. The first statement of the transaction, always, nothing above it.
            db.execute(LOCK, {"key": LOCK_KEY})
            # 2. FOR SHARE is mandatory: archiving takes FOR NO KEY UPDATE and the booking's foreign
            #    key check only KEY SHARE, so without this a booking can land on a service archived
            #    concurrently. Postgres now() rides on this row and is the transaction's clock.
            row = db.execute(SERVICE, {"service_id": service_id}).first()
            if row is None:
                raise ApiError(404, "not_found")
            settings = business_settings.read(db)  # 3
            opening = schedule.envelope(db)  # 4: once, never per worker or per day
            hours: dict[UUID, list[schedule.Row]] = defaultdict(list)
            names: dict[UUID, str | None] = {}
            for m, display_name, weekday, starts_at, ends_at in db.execute(
                CANDIDATES, {"service_id": service_id, "member_id": new.member_id}
            ).tuples():
                hours[m].append((weekday, starts_at, ends_at))
                names[m] = display_name
            candidates = sorted(hours)  # 5
            # 6. Validate the WHOLE posted map here, before any write, so a bad policy version is a
            #    422 with nothing written - regardless of whether anything was withdrawn.
            clients.texts_for(new.policy_version, new.consents)
            # Consent WITHDRAWALS only; grants ride the booking_events row instead (ruling 18, §10).
            withdrawals: dict[Purpose, bool] = {p: g for p, g in new.consents.items() if not g}
            # Keyed on AUTHENTICATION, never on the route: a signed-in user typing somebody else's
            # address is anonymous for this purpose.
            mine = signed_in_as is not None and signed_in_as.email == new.email
            # 7. Once, before the savepoint: the row lock this takes is what the pending cap needs,
            #    and ROLLBACK TO SAVEPOINT would release a lock taken inside one.
            found = clients.find_or_create(
                db,
                name=new.name,
                email=new.email,
                phone=new.phone,
                locale=new.locale,
                user_id=signed_in_as.user_id if signed_in_as is not None and mine else None,
                refresh=mine,
            )
            if withdrawals:  # 8: record_consents calls texts_for again - its own trust boundary
                clients.record_consents(
                    db,
                    client_id=found.id,
                    policy_version=new.policy_version,
                    purposes=withdrawals,
                    source="booking_page",
                    ip=origin_ip,
                    user_agent=user_agent,
                )
            # 9. Exact under the lock, rather than best-effort: :now is step 2's Postgres clock.
            db.execute(EXPIRE, {"expiring": list(availability.EXPIRING), "now": row.now})
            zone = settings.timezone
            day = new.starts_at.astimezone(ZoneInfo(zone)).date()
            first, last, earliest = availability.window(
                row.now, zone, day, day, settings.min_notice_minutes, settings.booking_horizon_days
            )
            if first > last:  # in the past, or beyond the horizon
                raise ApiError(409, "slot_unavailable")
            off_by_member: dict[UUID, list[availability.Interval]] = defaultdict(list)
            booked_rows: list[tuple[UUID, datetime, datetime, int | None]] = []
            if candidates:
                start = schedule.to_utc(first - timedelta(days=1), time(), zone)
                end = schedule.to_utc(last + timedelta(days=2), time(), zone)
                for m, starts_at, ends_at in db.execute(  # 10
                    TIME_OFF, {"members": candidates, "start": start, "end": end}
                ).tuples():
                    off_by_member[m].append((starts_at, ends_at))
                booked_rows = availability.booked(db, candidates, start, end)  # 11
            bookings: dict[UUID, list[availability.Booked]] = defaultdict(list)
            for m, starts_at, ends_at, override in booked_rows:
                bookings[m].append((starts_at, ends_at, override))
            buffer = availability.buffer_for(
                row.duration_minutes, row.buffer_minutes, settings.buffer_pct
            )
            # 12. Re-derive the posted start through member_slots itself - never a bespoke
            #     validator: two implementations of "is this slot bookable" drift, and the drift is
            #     the bug. The constraint alone does not catch a buffer tail, opening hours, the
            #     worker's own hours, time off, the slot grid, min_notice, the horizon, an
            #     unassigned worker or an archived service.
            offered = {
                candidate: availability.member_slots(
                    hours[candidate],
                    off_by_member[candidate],
                    bookings[candidate],
                    opening=opening,
                    zone=zone,
                    first=first,
                    last=last,
                    duration=row.duration_minutes,
                    buffer=buffer,
                    pct=settings.buffer_pct,
                    step=settings.slot_step_minutes,
                    earliest=earliest,
                )
                for candidate in candidates
            }
            eligible = [c for c in candidates if new.starts_at in offered[c]]
            if not eligible:
                raise ApiError(409, "slot_unavailable")
            # 13. AFTER re-derivation, never before (B6): counting before it makes 429-vs-409 an
            #     oracle for "this address books here" against a deliberately unbookable slot.
            pending = db.scalar(
                PENDING_COUNT,
                {"client_id": found.id, "expiring": list(availability.EXPIRING), "now": row.now},
            )
            if (pending or 0) >= settings.max_pending_per_email:
                raise ApiError(429, "rate_limited")
            # 14. The candidate loop: not "one retry" (§1.1). Least loaded that day, then worker_id
            #     ascending - the tiebreak is deterministic, so concurrent requests for the same
            #     slot pile onto the same worker by construction. Under the tenant lock a loser
            #     acquires the lock only after the winner commits, re-derives, and 409s above; the
            #     loop survives as the backstop for a writer that forgot the lock (23P01) or a
            #     member removed concurrently (23503, B5).
            status = "confirmed" if settings.auto_confirm else "pending"
            expires_at = None if status == "confirmed" else row.now + PENDING_TTL
            load = Counter(
                m
                for m, starts_at, _ends_at, _o in booked_rows
                if starts_at.astimezone(ZoneInfo(zone)).date() == day
            )
            queue = sorted(eligible, key=lambda m: (load[m], m))
            booking: Row[Any] | None = None
            candidate: UUID | None = None
            while queue:
                candidate = queue.pop(0)  # popped whether it loses to a row, a 23P01 or a 23503
                try:
                    with db.begin_nested():
                        booking = db.execute(
                            INSERT_BOOKING,
                            {
                                "client_id": found.id,
                                "worker_id": candidate,
                                "service_id": service_id,
                                "starts_at": new.starts_at,
                                "duration_minutes": row.duration_minutes,
                                "status": status,
                                "expires_at": expires_at,
                                "source": "booking_page",
                                "service_name": json.dumps(row.name),
                                "price_amount_minor": row.price_amount_minor,
                                "price_currency": row.price_currency,
                                "cancellation_policy_text": settings.cancellation_policy_text
                                or None,
                                "auto_confirm_at_booking": settings.auto_confirm,
                                "worker_display_name": names[candidate],
                            },
                        ).one()
                        # 15. Inside the winning savepoint, immediately after the booking insert.
                        db.execute(
                            INSERT_EVENT,
                            {
                                "booking_id": booking.id,
                                "ip": origin_ip,
                                "user_agent": user_agent,
                                "policy_version": new.policy_version,
                                "consent_purposes": json.dumps(new.consents),
                            },
                        )
                except IntegrityError as error:
                    if (
                        isinstance(error.orig, ExclusionViolation)
                        and error.orig.diag.constraint_name == OVERLAP
                    ):
                        continue  # a writer that bypassed the lock took this worker
                    # B5: the member was REMOVED while this request was under way. members.remove
                    # takes FOR UPDATE on the membership and NO advisory lock, so it races us
                    # freely: our insert blocks on the foreign key's FOR KEY SHARE, the owner
                    # commits, and our re-check finds the membership gone. Treat it as "this
                    # candidate went away" - the same situation as losing the slot - not as a 500.
                    if (
                        isinstance(error.orig, ForeignKeyViolation)
                        and error.orig.diag.constraint_name == members.BOOKINGS_WORKER
                    ):
                        continue
                    raise  # any other integrity error stays a 500
                break
            else:
                raise ApiError(409, "slot_taken")
            # mypy narrowing only: the `else` above raises, so these are bound. Stripped under
            # `python -O`, which is why nothing below may depend on this running.
            assert booking is not None and candidate is not None
            return BookingOut(
                id=booking.id,
                status=booking.status,
                starts_at=booking.starts_at,
                ends_at=booking.ends_at,
                duration_minutes=row.duration_minutes,
                service_name=row.name,
                price=Price(amount_minor=row.price_amount_minor, currency=row.price_currency),
                worker_id=candidate,
                worker_display_name=names[candidate],
                cancellation_policy_text=settings.cancellation_policy_text or None,
            )
    except OperationalError:
        # DeadlockDetected (40P01) and serialization failures arrive here, NOT as IntegrityError,
        # and abort the whole transaction - ROLLBACK TO SAVEPOINT cannot recover one, so this cannot
        # be handled inside the loop. Under the step-1 lock this should be unreachable; it exists
        # because "should be" is not a control. 503, not 409 (which would assert "taken" when we do
        # not know) and not 500. app/passwords.py:84 established the code. No retry behind it: a
        # retry would need the Turnstile token again, which is single-use (C9).
        raise ApiError(503, "busy") from None
