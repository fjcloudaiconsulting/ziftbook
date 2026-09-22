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
from typing import Annotated, Any, Literal, NamedTuple
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request, Response
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


class Rule(NamedTuple):
    """One PATCH target: where it may come from, whether the appointment must already have
    started, and the audit action it records. The booking_events value is the target status
    itself, so there is no fourth field (0026:211-217 puts no CHECK on booking_events.event, and
    ck_bookings_status already holds the value set)."""

    sources: tuple[str, ...]
    past_only: bool  # starts_at <= now(): a merchant knows a no-show at the start time
    action: auth.Action


# The booking_events value set and its audit action, held here because migration 0026 deliberately
# put no CHECK on booking_events.event (0026:211-217). ZIF-53/ZIF-55/ZIF-7 add their own rows.
#
# "The merchant can always cancel" = from every status that still holds the slot, which is why
# `completed` is a source for cancelled_by_merchant and for no_show: `completed` does NOT leave
# OCCUPYING (app/availability.py:163), DELETE is revoked from the app role (0026:203), and without
# those two sources a merchant who mis-clicks "completed" one second into a twelve-hour booking
# pins the slot inside OCCUPYING forever and destroys the no-show outcome. Both corrections LEAVE
# the exclusion predicate, and leaving never conflicts.
#
# There is deliberately no way OUT of `no_show`. no_show FREES the slot, so `no_show -> completed`
# or `-> confirmed` is a 23P01 waiting for the merchant who corrects a mistake five minutes after
# making it, once a walk-in legitimately holds the freed slot. Whoever adds an undo must catch
# ExclusionViolation with constraint_name == OVERLAP and answer 409 slot_taken, exactly as create()
# already does. `awaiting_payment` is absent from every source list on purpose: nothing writes it
# until ZIF-7 brings the payment path that makes it reachable.
TRANSITIONS: dict[str, Rule] = {
    "confirmed": Rule(("pending",), False, "booking_confirmed"),
    "declined": Rule(("pending",), False, "booking_declined"),
    "cancelled_by_merchant": Rule(
        ("pending", "confirmed", "completed"), False, "booking_cancelled_by_merchant"
    ),
    "completed": Rule(("confirmed",), True, "booking_completed"),
    "no_show": Rule(("confirmed", "completed"), True, "booking_no_show"),
}
Target = Literal["confirmed", "declined", "cancelled_by_merchant", "completed", "no_show"]


class StatusChange(BaseModel):
    model_config = STRICT  # app.services.STRICT: strict=True, extra="forbid"
    status: Target


class BookingStatusOut(BaseModel):
    id: UUID
    status: Target


class PendingOut(BaseModel):
    id: UUID
    starts_at: datetime
    ends_at: datetime
    expires_at: datetime  # NOT NULL for every pending: ck_bookings_expires_at
    service_id: UUID
    service_name: dict[str, str]  # all three locales, as snapshotted
    price: Price  # app.services.Price
    worker_id: UUID
    worker_display_name: str | None
    client_id: UUID
    client_name: str


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
  auto_confirm_at_booking, worker_display_name, free_cancellation_hours, reschedule_cutoff_hours)
VALUES (
  current_setting('app.tenant_id')::uuid, :client_id, :worker_id, :service_id,
  :starts_at, :starts_at + make_interval(mins => :duration_minutes), :status, :expires_at,
  :source, CAST(:service_name AS jsonb), :price_amount_minor, :price_currency, :duration_minutes,
  :cancellation_policy_text, :auto_confirm_at_booking, :worker_display_name,
  :free_cancellation_hours, :reschedule_cutoff_hours)
RETURNING id, status, starts_at, ends_at
""")

INSERT_EVENT = text("""
INSERT INTO booking_events (tenant_id, booking_id, event, ip, user_agent, policy_version,
                            consent_purposes)
VALUES (current_setting('app.tenant_id')::uuid, :booking_id, :event, CAST(:ip AS inet),
        :user_agent, :policy_version, CAST(:consent_purposes AS jsonb))
""")

# ponytail: limit=50 (100 max) with no cursor, so a business holding more live pendings than that
# sees only the soonest ones and the tail stays invisible until the TTL thins it. Bounded by
# pending_ttl_hours x arrival rate, capped per address by max_pending_per_email. The upgrade is a
# (starts_at, id) cursor exactly like app/clients.py:63 - no schema change, no new index.
#
# No starts_at filter, deliberately (rejected review finding): a pending whose appointment time has
# already passed stays in the queue and, soonest-first, sorts to the top. The merchant must still be
# able to settle it rather than have it vanish until the TTL, and recording a booking whose time has
# passed is a case migration 0026 explicitly supports (its walk-in comment, 0026:159-171). Top of
# the list is the right urgency order for it.
QUEUE = text("""
SELECT b.id, b.starts_at, b.ends_at, b.expires_at, b.service_id, b.service_name,
       jsonb_build_object('amount_minor', b.price_amount_minor,
                          'currency', b.price_currency) AS price,
       b.worker_id, b.worker_display_name, b.client_id, c.name AS client_name
FROM bookings b
JOIN clients c ON c.tenant_id = b.tenant_id AND c.id = b.client_id
WHERE b.status = 'pending' AND b.expires_at > now()
  AND (:everyone OR b.worker_id = (SELECT id FROM memberships WHERE user_id = :me))
ORDER BY b.starts_at, b.id
LIMIT :limit
""")

# The booking and who holds it. No FOR UPDATE (D5): the advisory lock and TRANSITION's own
# qualifier are what make the transition exactly-once, and locking a membership row here would
# take locks in an order CONTRIBUTING.md constrains around keep_an_owner.
BOOKING = text("""
SELECT m.user_id AS worker_user_id
FROM bookings b
JOIN memberships m ON m.tenant_id = b.tenant_id AND m.id = b.worker_id
WHERE b.id = :id
""")

# One statement, both guards in SQL. `expires_at > now()` is the TRANSACTION's clock - the same
# now() availability.BOOKED uses (app/availability.py:175) - so the queue and this can never
# disagree. expires_at is NOT in the SET list: ck_bookings_expires_at is one-way (0026:95-100).
#
# now() is transaction_timestamp(), frozen when the transaction opens, so a request that waits on
# the tenant advisory lock compares against a clock behind wall clock: a pending that expires WHILE
# the request waits is still accepted (measured: a transaction blocked 2.7 s saw now() 2.717 s
# behind clock_timestamp()). Kept on purpose (rejected review finding): statement_timestamp() here
# would let the queue (QUEUE, availability.BOOKED and EXPIRE all use now()) and this transition
# disagree about the same booking - the exact failure the design set out to prevent - and the error
# direction is the safe one: now() <= wall clock, so a live pending is never wrongly refused.
# tests/test_bookings_approval_api.py's F3 fences exactly this convention.
# The liveness clause is `ck_bookings_expires_at`'s own shape (0026:95-100) read as a liveness
# test, not a workaround for it. For a `pending` row the CHECK makes expires_at NOT NULL, so
# ZIF-52's guard is preserved exactly; for a `confirmed` or `completed` row the clause is satisfied
# by the STATUS, whatever expires_at holds. Writing it as a bare `expires_at > now()` would be
# correct for ZIF-52 (which was gated on status = 'pending') and a bug HERE: an auto_confirm
# booking has expires_at IS NULL, `NULL > now()` is NULL, and every merchant cancellation of one
# would answer 409.
TRANSITION = text("""
UPDATE bookings SET status = :status
WHERE id = :id
  AND status = ANY(CAST(:allowed_from AS text[]))
  AND (status <> ALL (ARRAY['awaiting_payment', 'pending']) OR expires_at > now())
  AND (NOT :past_only OR starts_at <= now())
RETURNING id, status
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
            expires_at = (
                None
                if status == "confirmed"
                else row.now + timedelta(hours=settings.pending_ttl_hours)
            )
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
                                # ZIF-55: the thresholds in force NOW, snapshotted beside the
                                # text. Never re-read at cancellation time.
                                "free_cancellation_hours": settings.free_cancellation_hours,
                                "reschedule_cutoff_hours": settings.reschedule_cutoff_hours,
                                "auto_confirm_at_booking": settings.auto_confirm,
                                "worker_display_name": names[candidate],
                            },
                        ).one()
                        # 15. Inside the winning savepoint, immediately after the booking insert.
                        db.execute(
                            INSERT_EVENT,
                            {
                                "booking_id": booking.id,
                                "event": "created",
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


merchant_router = APIRouter(prefix="/api", tags=["booking-approvals"])


@merchant_router.get(
    "/bookings/pending", name="list", responses={s: {"model": Error} for s in (401, 422)}
)
def pending(
    current: auth.CurrentSession,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[PendingOut]:
    """The business's live pending bookings, soonest first. An owner sees the whole queue; a
    worker sees only bookings assigned to them - members.is_owner, the same owner half that
    members.may_manage applies to the transition, so the list and the transition can never
    disagree."""
    rows = current.db.execute(
        QUEUE,
        {"everyone": members.is_owner(current), "me": current.user_id, "limit": limit},
    ).all()
    response.headers["Cache-Control"] = "no-store"
    return [PendingOut.model_validate(row, from_attributes=True) for row in rows]


@merchant_router.patch(
    "/bookings/{booking_id}",
    name="update",
    responses={s: {"model": Error} for s in (401, 403, 404, 409, 415, 422)},
)
def transition(
    booking_id: UUID,
    change: StatusChange,
    current: auth.CurrentSession,
    request: Request,
    response: Response,
) -> BookingStatusOut:
    """A merchant settles a booking: accept, decline, cancel, or record how it went. An owner, or
    the membership in bookings.worker_id, may transition; anyone else on this business gets 403.
    Another business's booking is 404 (row-level security makes it invisible) rather than 403.

    The UPDATE's own qualifier is the only authority on whether a transition is legal - the SELECT
    above it exists for the 404/403 pair and nothing else - which is what makes it exactly-once
    under the advisory lock. Zero rows updated is 409 invalid_transition, for either refusal:
    wrong source status, or an appointment that has not started yet."""
    current.db.execute(LOCK, {"key": LOCK_KEY})  # 1: nothing above this
    row = current.db.execute(BOOKING, {"id": booking_id}).first()  # 2
    if row is None:
        raise ApiError(404, "not_found")
    if not members.may_manage(current, row.worker_user_id):  # 3
        raise ApiError(403, "owner_only")
    rule = TRANSITIONS[change.status]
    changed = current.db.execute(
        TRANSITION,
        {
            "id": booking_id,
            "status": change.status,
            "allowed_from": list(rule.sources),
            "past_only": rule.past_only,
        },
    ).first()  # 4
    if changed is None:
        raise ApiError(409, "invalid_transition")
    origin_ip, user_agent = auth.origin(request)
    current.db.execute(  # 5
        INSERT_EVENT,
        {
            "booking_id": booking_id,
            "event": change.status,
            "ip": origin_ip,
            "user_agent": user_agent,
            "policy_version": None,
            "consent_purposes": None,
        },
    )
    auth.record(  # 6
        current.db,
        request,
        rule.action,
        actor_user_id=current.user_id,
        target=f"booking:{booking_id}",
    )
    response.headers["Cache-Control"] = "no-store"  # 7
    return BookingStatusOut(id=changed.id, status=changed.status)
