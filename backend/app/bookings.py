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
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal, NamedTuple, Self
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request, Response
from opentelemetry.trace import SpanKind
from psycopg.errors import ExclusionViolation, ForeignKeyViolation
from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import Row, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import (
    auth,
    availability,
    business_settings,
    clients,
    jobs,
    limits,
    members,
    passwords,
    schedule,
    tracing,
    turnstile,
)
from app import time_off as time_off_module
from app.business_settings import Locale
from app.clients import ClientName, Phone, PolicyVersion, Purpose
from app.db import SessionLocal, join_tenant, tenant_context
from app.errors import ApiError, Error
from app.services import STRICT, DescriptionText, Price

logger = logging.getLogger(__name__)

IP_LIMIT, EMAIL_LIMIT, LIMIT_WINDOW = 30, 5, timedelta(hours=1)
# ZIF-51's own key in app/schedule.py's ticket-number advisory lock convention (OPENING_LOCK = 105):
# disjoint first arguments, so the two locks can never collide.
LOCK_KEY = 51
OVERLAP = "ex_bookings_worker_overlap"  # migration 0026; answered with 409 slot_taken
MAX_AGENDA_WINDOW = timedelta(days=8)  # a 7-day local week across a clock change is 7d +- 1h


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
# those sources a merchant who mis-clicks "completed" one second into a twelve-hour booking pins
# the slot inside OCCUPYING forever and destroys the no-show outcome. Every correction LEAVES the
# exclusion predicate or stays inside it, and neither conflicts.
#
# `completed -> confirmed` is the RESTORE, and it is the only exit from a mis-clicked `completed`
# that costs neither money nor truth: the other two write a false record (`no_show`) or a 100%
# refund by AC (`cancelled_by_merchant`). "Correctable" means restorable, not merely exitable.
# It carries past_only = False because past_only is a property of the TARGET and `confirmed` is
# also how a future pending is accepted; the restore is past-only anyway, through its source -
# `completed` is only reachable under `completed`'s own past_only.
#
# There is deliberately no way OUT of `no_show`. no_show FREES the slot, so `no_show -> completed`
# or `-> confirmed` is a 23P01 waiting for the merchant who corrects a mistake five minutes after
# making it, once a walk-in legitimately holds the freed slot. Whoever adds an undo must catch
# ExclusionViolation with constraint_name == OVERLAP and answer 409 slot_taken, exactly as create()
# already does. `awaiting_payment` is absent from every source list on purpose: nothing writes it
# until ZIF-7 brings the payment path that makes it reachable.
TRANSITIONS: dict[str, Rule] = {
    "confirmed": Rule(("pending", "completed"), False, "booking_confirmed"),
    "declined": Rule(("pending",), False, "booking_declined"),
    "cancelled_by_merchant": Rule(
        ("pending", "confirmed", "completed"), False, "booking_cancelled_by_merchant"
    ),
    "completed": Rule(("confirmed",), True, "booking_completed"),
    "no_show": Rule(("confirmed", "completed"), True, "booking_no_show_recorded"),
}
Target = Literal["confirmed", "declined", "cancelled_by_merchant", "completed", "no_show"]

# Leaving `completed` is the owner's alone, whatever the target. `completed` is the one status a
# non-owner worker can reach on their OWN booking (may_manage lets the assigned worker transition
# it), and every exit from it is destructive or irreversible: `no_show` is terminal and frees the
# slot, `cancelled_by_merchant` is a 100% refund by AC, and `confirmed` un-does a settled record.
# Without this a worker could walk a past booking of theirs to `cancelled_by_merchant` and leave
# the owner with no route back at all. Doing the job stays open to them: `confirmed -> completed`
# and `confirmed -> no_show` are unaffected. A mis-click is corrected by an owner.
OWNER_ONLY_SOURCES = ("completed",)


class StatusChange(BaseModel):
    model_config = STRICT  # app.services.STRICT: strict=True, extra="forbid"
    status: Target
    message: DescriptionText | None = Field(
        default=None,
        description="Only with status `declined`: sent to the client in the decline email.",
    )

    @model_validator(mode="after")
    def only_a_decline_has_a_message(self) -> Self:
        if self.message is not None and self.status != "declined":
            raise ValueError("a message goes with a decline only")
        return self


class BookingStatusOut(BaseModel):
    id: UUID
    status: Target


class BookingRow(BaseModel):
    """What the pending queue and the agenda both say about a booking. No client email or phone:
    a list never carries them (the detail does)."""

    id: UUID
    starts_at: datetime
    ends_at: datetime
    service_id: UUID
    service_name: dict[str, str]  # all three locales, as snapshotted
    price: Price  # app.services.Price
    worker_id: UUID
    worker_display_name: str | None
    client_id: UUID
    client_name: str
    created_at: datetime  # "booked online 2 hours ago"


class PendingOut(BookingRow):
    expires_at: datetime  # NOT NULL for every pending: ck_bookings_expires_at


class AgendaOut(BookingRow):
    expires_at: datetime | None  # set while a hold is live; null once settled
    status: str
    source: str  # booking_page, merchant (a walk-in) or marketplace


class EventOut(BaseModel):
    event: str
    at: datetime
    actor: Literal["client", "team", "system"]
    actor_name: str | None  # null for a removed member, and for a pre-0033 team event
    details: dict[str, Any] | None


class BookingDetailOut(AgendaOut):
    client_email: str | None
    client_phone: str | None
    client_note: str | None
    decline_message: str | None
    cancellation_policy_text: str | None
    reschedule_count: int
    max_reschedules: int
    # A lapsed hold nobody has swept yet still reads status `pending`; this says it is over, so the
    # panel never offers Accept on it (PATCH would 409).
    expired: bool
    history: list[EventOut]


LOCK = text("SELECT pg_advisory_xact_lock(:key, hashtext(current_setting('app.tenant_id')))")

SERVICE = text("""
SELECT now() AS now, name, price_amount_minor, price_currency, duration_minutes, buffer_minutes
FROM services
WHERE id = :service_id AND archived_at IS NULL
FOR SHARE
""")

# ZIF-122. Shared by create() and the worker's sweep(): the event rides the same statement, so no
# path can expire a booking without recording it. now() is the transaction's clock, as :now was.
EXPIRE = text("""
WITH gone AS (
  UPDATE bookings SET status = 'expired'
  WHERE status = ANY(CAST(:expiring AS text[])) AND expires_at <= now()
  RETURNING tenant_id, id)
INSERT INTO booking_events (tenant_id, booking_id, event)
SELECT tenant_id, id, 'expired' FROM gone
RETURNING 1
""")

PENDING_COUNT = text("""
SELECT count(*) FROM bookings
WHERE client_id = :client_id AND status = ANY(CAST(:expiring AS text[])) AND expires_at > :now
""")

INSERT_BOOKING = text("""
INSERT INTO bookings (
  tenant_id, client_id, worker_id, service_id, starts_at, ends_at, status, expires_at, source,
  service_name, price_amount_minor, price_currency, duration_minutes, cancellation_policy_text,
  auto_confirm_at_booking, worker_display_name, free_cancellation_hours, reschedule_cutoff_hours,
  original_starts_at, earliest_starts_at, max_reschedules)
VALUES (
  current_setting('app.tenant_id')::uuid, :client_id, :worker_id, :service_id,
  :starts_at, :starts_at + make_interval(mins => :duration_minutes), :status, :expires_at,
  :source, CAST(:service_name AS jsonb), :price_amount_minor, :price_currency, :duration_minutes,
  :cancellation_policy_text, :auto_confirm_at_booking, :worker_display_name,
  :free_cancellation_hours, :reschedule_cutoff_hours,
  -- ZIF-54 (D8). Both snapshotted = the insert's own starts_at, never re-read afterwards.
  :starts_at, :starts_at, :max_reschedules)
RETURNING id, status, starts_at, ends_at
""")

INSERT_EVENT = text("""
INSERT INTO booking_events (tenant_id, booking_id, event, ip, user_agent, policy_version,
                            consent_purposes, actor_user_id, details)
VALUES (current_setting('app.tenant_id')::uuid, :booking_id, :event, CAST(:ip AS inet),
        :user_agent, :policy_version, CAST(:consent_purposes AS jsonb), :actor_user_id,
        CAST(:details AS jsonb))
""")

# The columns QUEUE and RANGE share, so the two lists can never drift apart.
LIST_COLUMNS = """
b.id, b.starts_at, b.ends_at, b.service_id, b.service_name,
jsonb_build_object('amount_minor', b.price_amount_minor, 'currency', b.price_currency) AS price,
b.worker_id, b.worker_display_name, b.client_id, c.name AS client_name, b.created_at
"""

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
QUEUE = text(f"""
SELECT {LIST_COLUMNS}, b.expires_at
FROM bookings b
JOIN clients c ON c.tenant_id = b.tenant_id AND c.id = b.client_id
WHERE b.status = 'pending' AND b.expires_at > now()
  AND (:everyone OR b.worker_id = (SELECT id FROM memberships WHERE user_id = :me))
ORDER BY b.starts_at, b.id
LIMIT :limit
""")

# ZIF-57. The agenda: bookings overlapping [from, to), half-open on both sides.
#  * `starts_at > :from - 12 hours` is the look-back ck_bookings_at_most_12_hours (0026:91) gives:
#    with `ends_at > :from` strict and the CHECK `<=`, a booking that overlaps :from started no
#    earlier than 12 hours before it. Zero margin, on purpose: it is exactly the CHECK.
#  * The statuses are the occupying four plus no_show (an outcome the day still shows); declined,
#    expired and both cancellations are history, not agenda.
#  * A lapsed hold nobody has swept yet is hidden, as QUEUE and availability.BOOKED hide it.
#  * The visibility clause is QUEUE's verbatim, so the two lists and PATCH agree on who sees what.
# ponytail: no limit or cursor. The 8-day span cap is the bound (one worker's day holds a few dozen
# bookings at most); the upgrade is a (starts_at, id) cursor like app/clients.py:63.
RANGE = text(f"""
SELECT {LIST_COLUMNS}, b.expires_at, b.status, b.source
FROM bookings b
JOIN clients c ON c.tenant_id = b.tenant_id AND c.id = b.client_id
WHERE b.starts_at < :to AND b.ends_at > :from
  AND b.starts_at > CAST(:from AS timestamptz) - interval '12 hours'
  AND b.status = ANY(CAST(:statuses AS text[]))
  AND NOT (b.status = ANY(CAST(:expiring AS text[])) AND b.expires_at <= now())
  AND (:everyone OR b.worker_id = (SELECT id FROM memberships WHERE user_id = :me))
ORDER BY b.starts_at, b.id
""")

# One booking for its panel, any status. The worker's user_id rides along for may_manage. Never
# selects clients.internal_note (the merchant's private note about a person) and never the event
# personal data below.
DETAIL = text(f"""
SELECT {LIST_COLUMNS}, b.expires_at, b.status, b.source, wm.user_id AS worker_user_id,
       c.email AS client_email, c.phone AS client_phone, c.client_note, b.decline_message,
       b.cancellation_policy_text, b.reschedule_count, b.max_reschedules,
       coalesce(b.status = ANY(CAST(:expiring AS text[])) AND b.expires_at <= now(), false)
         AS expired
FROM bookings b
JOIN clients c ON c.tenant_id = b.tenant_id AND c.id = b.client_id
JOIN memberships wm ON wm.tenant_id = b.tenant_id AND wm.id = b.worker_id
WHERE b.id = :id
""")

# Named columns, never SELECT *: ip, user_agent, policy_version and consent_purposes are the
# employee's and the guest's personal data (0026:219-223) and stay out of this response. The actor
# is joined to memberships by user_id and to users only through that membership, so a removed
# member reads as a null name rather than vanishing the event.
HISTORY = text("""
SELECT e.event, e.created_at AS at, e.actor_user_id, e.details,
       coalesce(m.display_name, u.name) AS actor_name
FROM booking_events e
LEFT JOIN memberships m ON m.tenant_id = e.tenant_id AND m.user_id = e.actor_user_id
LEFT JOIN users u ON u.id = m.user_id
WHERE e.booking_id = :id
ORDER BY e.id
""")

# The booking and who holds it. No FOR UPDATE (D5): the advisory lock and TRANSITION's own
# qualifier are what make the transition exactly-once, and locking a membership row here would
# take locks in an order CONTRIBUTING.md constrains around keep_an_owner.
BOOKING = text("""
SELECT m.user_id AS worker_user_id, b.status, b.starts_at, b.ends_at, b.worker_id, b.service_id,
       b.worker_display_name, now() AS now
FROM bookings b
JOIN memberships m ON m.tenant_id = b.tenant_id AND m.id = b.worker_id
WHERE b.id = :id
""")

# ZIF-53. Every active owner (memberships never soft-delete today; members.remove deletes) plus the
# assigned worker: the people who can settle the booking, the same set members.may_manage and the
# pending queue use.
MERCHANTS = text("SELECT DISTINCT user_id FROM memberships WHERE role = 'owner' OR id = :worker_id")


def email(
    db: Session,
    tenant_id: UUID,
    booking_id: UUID,
    template: str,
    user_id: UUID | None = None,
    *,
    key_suffix: str = "",
    extra: dict[str, str] | None = None,
) -> None:
    """Enqueue one booking email, in the caller's transaction.

    With user_id, the email is to that merchant (ruling R1) rather than the client, and the
    dedupe key and payload carry the user_id too.

    key_suffix (ZIF-54): goes on the dedupe key, before the user_id suffix if any -- a reschedule's
    `:r{count}` so a later reschedule's confirmation email is a fresh job, not a dedupe no-op
    against an earlier one's. extra: values merged into the payload (starts_at,
    previous_starts_at); mail.send_booking's starts_at gate reads starts_at back out of it.
    """
    key = f"email.booking:{tenant_id}:{booking_id}:{template}{key_suffix}"
    payload: dict[str, str] = {"booking_id": str(booking_id), "template": template}
    if extra:
        payload.update(extra)
    if user_id is not None:
        key += f":{user_id}"
        payload["user_id"] = str(user_id)
    jobs.enqueue(db, "email.booking", key, payload, tenant_id=tenant_id)


def email_merchants(
    db: Session,
    tenant_id: UUID,
    booking_id: UUID,
    worker_id: UUID,
    template: str,
    *,
    key_suffix: str = "",
    extra: dict[str, str] | None = None,
) -> None:
    """Enqueue one merchant booking email per active owner plus the assigned worker (ruling R1)."""
    for (user_id,) in db.execute(MERCHANTS, {"worker_id": worker_id}).all():
        email(
            db, tenant_id, booking_id, template, user_id=user_id, key_suffix=key_suffix, extra=extra
        )


def remind(
    db: Session, tenant_id: UUID, booking_id: UUID, starts_at: datetime, now: datetime
) -> None:
    """Enqueue the 24h-ahead reminder, unless the start is already less than 24h away (R3)."""
    due_at = starts_at - timedelta(hours=24)
    if due_at <= now:
        return
    jobs.enqueue(
        db,
        "email.booking",
        f"email.booking:{tenant_id}:{booking_id}:booking_reminder:{starts_at.isoformat()}",
        {
            "booking_id": str(booking_id),
            "template": "booking_reminder",
            "starts_at": starts_at.isoformat(),
        },
        tenant_id=tenant_id,
        due_at=due_at,
    )


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
#
# REJECTED (reviewer, ZIF-55 R4): `awaiting_payment` in that array is unreachable - nothing writes
# the status until ZIF-7 - and no test covers it. It stays, deliberately: the array is a copy of
# ck_bookings_expires_at's own list (0026:95-100), and the day ZIF-7 makes the status reachable an
# array that had been trimmed to the reachable half would silently drop the liveness guard on the
# payment hold. Commentary for ZIF-7, kept in lockstep with the CHECK it mirrors.
# decline_message is set here, in the same statement: the text lands only if the qualified UPDATE
# succeeds. Safe to SET unconditionally (NULL for every other target) only because `declined` is in
# no Rule's sources, so no transition leaves a declined row and clears its message.
TRANSITION = text("""
UPDATE bookings SET status = :status, decline_message = :message
WHERE id = :id
  AND status = ANY(CAST(:allowed_from AS text[]))
  AND (status <> ALL (ARRAY['awaiting_payment', 'pending']) OR expires_at > now())
  AND (NOT :past_only OR starts_at <= now())
RETURNING id, status
""")

# ZIF-54. The two client-side writers, kept beside TRANSITION so every bookings writer lives in
# this one file. Both are D2's liveness predicate as their own qualifier -- app/booking_links.py's
# resolve() already checked it, but the UPDATE re-checks it under the row's own lock, exactly as
# TRANSITION re-checks its own source list rather than trusting the earlier SELECT.
#
# CLIENT_CANCEL never touches TRANSITIONS/Target (D10): 'cancelled_by_client' is not, and must
# never become, a merchant PATCH target (test 20).
CLIENT_CANCEL = text("""
UPDATE bookings SET status = 'cancelled_by_client'
WHERE id = :id AND starts_at > now()
  AND (status = 'confirmed' OR (status = 'pending' AND expires_at > now()))
RETURNING id, status
""")

# earliest_starts_at = LEAST(earliest_starts_at, :new): it only ever goes down (D7/D8), and the
# CHECK pair (ck_bookings_earliest_starts_at, ck_bookings_earliest_starts_at_current) is the
# database's own backstop on that guarantee. `status = 'confirmed'` (never 'pending': D7, a
# pending booking is never rescheduled) and `reschedule_count = :seen` (the client's echo,
# re-checked under the row's own write -- test 15's fence) are both qualifiers, not pre-checks:
# a stale echo or a status this statement doesn't match updates zero rows, answered as 409.
RESCHEDULE = text("""
UPDATE bookings SET starts_at = :new, ends_at = :new + (ends_at - starts_at),
                    earliest_starts_at = LEAST(earliest_starts_at, :new),
                    reschedule_count = reschedule_count + 1
WHERE id = :id AND status = 'confirmed' AND reschedule_count = :seen
RETURNING id, starts_at, ends_at, reschedule_count
""")


def by_load(
    eligible: list[UUID],
    booked_rows: list[tuple[UUID, datetime, datetime, int | None]],
    day: date,
    zone: str,
) -> list[UUID]:
    """Least loaded that day, then worker_id ascending - the tiebreak is deterministic, so
    concurrent requests for the same slot pile onto the same worker by construction."""
    load = Counter(
        m
        for m, starts_at, _ends_at, _o in booked_rows
        if starts_at.astimezone(ZoneInfo(zone)).date() == day
    )
    return sorted(eligible, key=lambda m: (load[m], m))


def place(
    db: Session,
    *,
    settings: business_settings.BusinessSettings,
    service: Row[Any],
    service_id: UUID,
    client_id: UUID,
    queue: list[UUID],
    names: dict[UUID, str | None],
    starts_at: datetime,
    status: str,
    expires_at: datetime | None,
    source: str,
    event: dict[str, Any],
) -> tuple[Row[Any], UUID]:
    """The one insert path for a new booking (ZIF-57): the public create() and the merchant's both
    land here, so the snapshot columns and the `created` event can never drift between them.

    `queue` is the candidate order; the first worker that takes the slot wins. `event` carries the
    `created` row's ip, user_agent, policy_version, consent_purposes (ALREADY serialised, or None:
    json.dumps(None) is the jsonb literal 'null', which ck_booking_events_consent_purposes
    refuses), actor_user_id and details. Returns the row and the worker it landed on; 409
    slot_taken when every candidate is gone. The caller holds the tenant lock.
    """
    # The candidate loop: not "one retry" (§1.1). Under the tenant lock a loser acquires the lock
    # only after the winner commits, re-derives, and 409s before it gets here; the loop survives as
    # the backstop for a writer that forgot the lock (23P01) or a member removed concurrently
    # (23503, B5).
    booking: Row[Any] | None = None
    candidate: UUID | None = None
    while queue:
        candidate = queue.pop(0)  # popped whether it loses to a row, a 23P01 or a 23503
        try:
            with db.begin_nested():
                booking = db.execute(
                    INSERT_BOOKING,
                    {
                        "client_id": client_id,
                        "worker_id": candidate,
                        "service_id": service_id,
                        "starts_at": starts_at,
                        "duration_minutes": service.duration_minutes,
                        "status": status,
                        "expires_at": expires_at,
                        "source": source,
                        "service_name": json.dumps(service.name),
                        "price_amount_minor": service.price_amount_minor,
                        "price_currency": service.price_currency,
                        "cancellation_policy_text": settings.cancellation_policy_text or None,
                        # ZIF-55: the thresholds in force NOW, snapshotted beside the
                        # text. Never re-read at cancellation time.
                        #
                        # REJECTED (reviewer, ZIF-55 R2): a settings PUT committing
                        # between business_settings.read() above and this INSERT
                        # snapshots the PRE-change value, and no tenant advisory lock
                        # closes that window. It is not a race to fix. The pre-change
                        # value is precisely the policy the client was shown on the
                        # booking page they are submitting; snapshotting the value that
                        # replaced it a moment ago would sell them terms they never saw.
                        "free_cancellation_hours": settings.free_cancellation_hours,
                        "reschedule_cutoff_hours": settings.reschedule_cutoff_hours,
                        "max_reschedules": settings.max_reschedules,
                        "auto_confirm_at_booking": settings.auto_confirm,
                        "worker_display_name": names[candidate],
                    },
                ).one()
                # Inside the winning savepoint, immediately after the booking insert.
                db.execute(
                    INSERT_EVENT,
                    {
                        "booking_id": booking.id,
                        "event": "created",
                        **event,
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
    return booking, candidate


# ZIF-57. The merchant's move: worker and snapshot name travel with the time, and reschedule_count
# is NOT touched (it is the client's allowance, cancellation.py's max_reschedules; the business
# moving a booking must not spend it). earliest_starts_at takes LEAST because
# ck_bookings_earliest_starts_at_current forces it when moving earlier; moving later leaves the
# refund anchor where it was. `status = 'confirmed'` is a qualifier, as RESCHEDULE's: D7.
MOVE = text("""
UPDATE bookings SET starts_at = :new, ends_at = :new + (ends_at - starts_at),
                    worker_id = :worker, worker_display_name = :name,
                    earliest_starts_at = LEAST(earliest_starts_at, :new)
WHERE id = :id AND status = 'confirmed'
RETURNING id, starts_at, ends_at, worker_id
""")

WORKER_USER = text("SELECT user_id FROM memberships WHERE id = :id")


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
            # ZIF-145. After Turnstile and the limits, so unpublished answers like unknown (F10);
            # before any write. A booking that read published=true before a concurrent unpublish
            # commits still lands: it was placed first, and the owner sees it.
            if not settings.published:
                raise ApiError(404, "not_found")
            opening = schedule.envelope(db)  # 4: once, never per worker or per day
            hours, names = availability.candidates(db, service_id, new.member_id)
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
            # 9. Exact under the lock, rather than best-effort: EXPIRE's now() is step 2's clock.
            db.execute(EXPIRE, {"expiring": list(availability.EXPIRING)})
            zone = settings.timezone
            day = new.starts_at.astimezone(ZoneInfo(zone)).date()
            first, last, earliest = availability.window(
                row.now, zone, day, day, settings.min_notice_minutes, settings.booking_horizon_days
            )
            if first > last:  # in the past, or beyond the horizon
                raise ApiError(409, "slot_unavailable")
            buffer = availability.buffer_for(
                row.duration_minutes, row.buffer_minutes, settings.buffer_pct
            )
            # 10-12. ONE implementation of "is this slot bookable" (availability.offered), never a
            #     bespoke validator: the constraint alone does not catch a buffer tail, opening
            #     hours, the worker's own hours, time off, the slot grid, min_notice, the horizon,
            #     an unassigned worker or an archived service. offered() itself no-ops (no query)
            #     when candidates is empty.
            result = availability.offered(
                db,
                members=candidates,
                hours=hours,
                opening=opening,
                settings=settings,
                first=first,
                last=last,
                earliest=earliest,
                duration=row.duration_minutes,
                buffer=buffer,
            )
            booked_rows = result.booked
            offered = result.slots
            eligible = [c for c in candidates if new.starts_at in offered[c]]  # keys every member
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
            queue = by_load(eligible, booked_rows, day, zone)
            booking, candidate = place(
                db,
                settings=settings,
                service=row,
                service_id=service_id,
                client_id=found.id,
                queue=queue,
                names=names,
                starts_at=new.starts_at,
                status=status,
                expires_at=expires_at,
                source="booking_page",
                event={
                    "ip": origin_ip,
                    "user_agent": user_agent,
                    "policy_version": new.policy_version,
                    "consent_purposes": json.dumps(new.consents),
                    "actor_user_id": None,  # the client: nobody is signed in to act
                    "details": None,
                },
            )
            # ZIF-53. Same session as the status write, outside the savepoint: a rollback of the
            # savepoint (a lost race) drops nothing here, since this only runs after `break`.
            if status == "confirmed":
                # starts_at (ZIF-54 D10): a reschedule before this runs makes it skip itself.
                email(
                    db,
                    tenant_id,
                    booking.id,
                    "booking_confirmed",
                    extra={"starts_at": booking.starts_at.isoformat()},
                )
                remind(db, tenant_id, booking.id, booking.starts_at, row.now)
                email_merchants(db, tenant_id, booking.id, candidate, "booking_new")
            else:
                email(db, tenant_id, booking.id, "booking_received")
                email_merchants(db, tenant_id, booking.id, candidate, "booking_request")
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


@merchant_router.get("/bookings", name="range", responses={s: {"model": Error} for s in (401, 422)})
def agenda(
    current: auth.CurrentSession,
    response: Response,
    from_: Annotated[time_off_module.Instant, Query(alias="from")],
    to: time_off_module.Instant,
) -> list[AgendaOut]:
    """The bookings whose interval overlaps [from, to), for a day or a week of the calendar.
    An owner sees everyone's, a worker their own (the same rule as the pending queue). The window
    may not exceed 8 days: a local week is 7 days plus or minus an hour across a clock change."""
    if to <= from_ or to - from_ > MAX_AGENDA_WINDOW:
        raise ApiError(422, "invalid_window")
    rows = current.db.execute(
        RANGE,
        {
            "from": from_,
            "to": to,
            "statuses": [*availability.OCCUPYING, "no_show"],
            "expiring": list(availability.EXPIRING),
            "everyone": members.is_owner(current),
            "me": current.user_id,
        },
    ).all()
    response.headers["Cache-Control"] = "no-store"
    return [AgendaOut.model_validate(row, from_attributes=True) for row in rows]


def actor_of(
    event: str, actor_user_id: UUID | None, source: str
) -> Literal["client", "team", "system"]:
    """Who an event was by. A recorded actor is the team. Without one (every row before 0033, and
    the client's own actions) it is read from the event: a merchant transition is the team's
    whichever way it was written, `created` is the team's only for a walk-in the merchant recorded
    (a booking_page or marketplace booking is the client's), `expired` is the system's, and the
    rest (cancelled_by_client, rescheduled, consent_confirmed) are the client's."""
    if actor_user_id is not None or event in TRANSITIONS:
        return "team"
    if event == "created":
        return "team" if source == "merchant" else "client"
    return "system" if event == "expired" else "client"


@merchant_router.get(
    "/bookings/{booking_id}",
    name="read",
    responses={s: {"model": Error} for s in (401, 403, 404, 422)},
)
def detail(booking_id: UUID, current: auth.CurrentSession, response: Response) -> BookingDetailOut:
    """One booking with its client's contact details and its history, any status. An owner, or the
    membership in bookings.worker_id, may read it: the rule and the code of PATCH. Another
    business's booking is 404."""
    row = current.db.execute(
        DETAIL, {"id": booking_id, "expiring": list(availability.EXPIRING)}
    ).first()
    if row is None:
        raise ApiError(404, "not_found")
    if not members.may_manage(current, row.worker_user_id):
        raise ApiError(403, "owner_only")
    history = [
        EventOut(
            event=e.event,
            at=e.at,
            actor=actor_of(e.event, e.actor_user_id, row.source),
            actor_name=e.actor_name,
            details=e.details,
        )
        for e in current.db.execute(HISTORY, {"id": booking_id})
    ]
    response.headers["Cache-Control"] = "no-store"
    return BookingDetailOut.model_validate(
        {**row._mapping, "history": history}, from_attributes=True
    )


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
    """A merchant settles a booking: accept, decline, cancel, or record how it went.

    An owner, or the membership in bookings.worker_id, may transition; anyone else on this business
    gets 403, and another business's booking is 404. Undoing a booking already marked `completed`
    is the owner's alone. A transition the booking's current status does not allow, or an outcome
    recorded before the appointment starts, is 409 `invalid_transition`.
    """
    # Internals, deliberately not in the public schema: the UPDATE's own qualifier is the only
    # authority on whether a transition is legal - the SELECT above it exists for the 404/403 pair
    # and nothing else - which is what makes it exactly-once under the advisory lock. Zero rows
    # updated is the 409, for either refusal.
    #
    # REJECTED (reviewer, ZIF-55 R1, and the same finding and the same answer as ZIF-52): the
    # 403/404/409 responses carry no Cache-Control. Statement 7 sets the header after every raise
    # on purpose - it matches the set_display_name precedent, and an error body is an error code
    # and nothing else, so there is nothing in it worth a caching directive.
    current.db.execute(LOCK, {"key": LOCK_KEY})  # 1: nothing above this
    row = current.db.execute(BOOKING, {"id": booking_id}).first()  # 2
    if row is None:
        raise ApiError(404, "not_found")
    if not members.may_manage(current, row.worker_user_id):  # 3
        raise ApiError(403, "owner_only")
    if row.status in OWNER_ONLY_SOURCES and not members.is_owner(current):  # 3b
        raise ApiError(403, "owner_only")
    # 3c, ZIF-143 (replaces ZIF-53 ruling R1): answering a request is the owner's unless the
    # business lets workers. Keyed on the source like 3b, so a worker's cancel of a pending is
    # refused too: the panel offers nothing on a pending to a worker who can't answer.
    if (
        row.status == "pending"
        and not members.is_owner(current)
        and not business_settings.read(current.db).workers_answer_requests
    ):
        raise ApiError(403, "owner_only")
    rule = TRANSITIONS[change.status]
    changed = current.db.execute(
        TRANSITION,
        {
            "id": booking_id,
            "status": change.status,
            "message": change.message,
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
            "actor_user_id": current.user_id,
            "details": None,
        },
    )
    # ZIF-53, before auth.record on purpose: a statement that fails after this must roll the job
    # back with the change (test 20). `row.status`, read at step 2 under the advisory lock, is the
    # PRE-transition source.
    if change.status == "confirmed" and row.status == "pending":
        email(
            current.db,
            current.tenant_id,
            booking_id,
            "booking_confirmed",
            extra={"starts_at": row.starts_at.isoformat()},
        )
        remind(current.db, current.tenant_id, booking_id, row.starts_at, row.now)
    elif change.status == "declined":
        email(current.db, current.tenant_id, booking_id, "booking_declined")
    elif change.status == "cancelled_by_merchant" and row.status in ("pending", "confirmed"):
        email(current.db, current.tenant_id, booking_id, "booking_cancelled")
    auth.record(  # 6
        current.db,
        request,
        rule.action,
        actor_user_id=current.user_id,
        target=f"booking:{booking_id}",
    )
    response.headers["Cache-Control"] = "no-store"  # 7
    return BookingStatusOut(id=changed.id, status=changed.status)


class MerchantBookingIn(BaseModel):
    """A member books on the calendar: a walk-in, a phone call, a regular. The client is named by
    id or created on the spot, never both and never neither."""

    model_config = STRICT
    client_id: Annotated[UUID, Field(strict=False)] | None = None
    new_client: clients.ClientIn | None = None
    service_id: Annotated[UUID, Field(strict=False)]
    member_id: Annotated[UUID, Field(strict=False)] | None = None  # None: "anyone"
    starts_at: time_off_module.Instant
    # A start the grid does not offer (a walk-in now, a favour after hours). Needs a person.
    override: bool = False

    @model_validator(mode="after")
    def one_way_to_name_a_client(self) -> Self:
        if (self.client_id is None) == (self.new_client is None):
            raise ValueError("exactly one of client_id and new_client")
        return self


class RescheduleIn(BaseModel):
    model_config = STRICT
    starts_at: time_off_module.Instant
    member_id: Annotated[UUID, Field(strict=False)] | None = None  # None: keep the worker
    override: bool = False


class RescheduledOut(BaseModel):
    id: UUID
    starts_at: datetime
    ends_at: datetime
    worker_id: UUID


def may_book_for(current: auth.SignedIn, member_id: UUID | None) -> bool:
    """members.may_manage for a member id: an owner books for anyone (an unknown id then answers
    409 slot_unavailable, no probing), a worker for themselves only, and nobody as "anyone" - that
    could land on a colleague."""
    if members.is_owner(current):
        return True
    if member_id is None:
        return False
    return bool(current.db.scalar(WORKER_USER, {"id": member_id}) == current.user_id)


def eligible_for(
    db: Session,
    *,
    settings: business_settings.BusinessSettings,
    now: datetime,
    hours: dict[UUID, list[schedule.Row]],
    starts_at: datetime,
    duration: int,
    buffer_minutes: int | None,
    exclude: UUID | None = None,
) -> tuple[list[UUID], list[tuple[UUID, datetime, datetime, int | None]]]:
    """Who, of these members, can take this exact start: availability.offered, as create() asks it
    (never a second validator). The booked rows ride along for by_load."""
    zone = settings.timezone
    day = starts_at.astimezone(ZoneInfo(zone)).date()
    first, last, earliest = availability.window(
        now, zone, day, day, settings.min_notice_minutes, settings.booking_horizon_days
    )
    if first > last:  # in the past, or beyond the horizon
        return [], []
    members_ = sorted(hours)
    result = availability.offered(
        db,
        members=members_,
        hours=hours,
        opening=schedule.envelope(db),
        settings=settings,
        first=first,
        last=last,
        earliest=earliest,
        duration=duration,
        buffer=availability.buffer_for(duration, buffer_minutes, settings.buffer_pct),
        exclude=exclude,
    )
    return [m for m in members_ if starts_at in result.slots[m]], result.booked


def tell_team(
    db: Session,
    current: auth.SignedIn,
    booking_id: UUID,
    user_ids: list[UUID],
    template: str,
    *,
    key_suffix: str = "",
    extra: dict[str, str],
) -> None:
    """One email per person, never to the member who made the change (they know)."""
    for user_id in dict.fromkeys(user_ids):
        if user_id != current.user_id:
            email(
                db,
                current.tenant_id,
                booking_id,
                template,
                user_id=user_id,
                key_suffix=key_suffix,
                extra=extra,
            )


@merchant_router.post(
    "/bookings",
    name="create",
    status_code=201,
    responses={s: {"model": Error} for s in (401, 403, 404, 409, 415, 422)},
)
def book(
    new: MerchantBookingIn, current: auth.CurrentSession, request: Request, response: Response
) -> BookingOut:
    """A member books a client in. Confirmed straight away, source `merchant`. The public route's
    gates do not apply: no Turnstile or rate limit (the caller is signed in), no publish gate (a
    business books by hand before it opens its page), no pending cap (nothing is pending).

    An owner books anyone; a worker only themselves. With `override` the grid is skipped - past
    starts, off-hours, time off, buffers - and only the exclusion constraint holds (CONTRIBUTING.md,
    "Bookings")."""
    db = current.db
    db.execute(LOCK, {"key": LOCK_KEY})  # 1: nothing above this
    row = db.execute(SERVICE, {"service_id": new.service_id}).first()  # 2
    if row is None:
        raise ApiError(404, "not_found")
    if not may_book_for(current, new.member_id):  # 3
        raise ApiError(403, "owner_only")
    # 4. An off-grid "anyone" would pick by load and could land on someone on holiday.
    if new.override and new.member_id is None:
        raise ApiError(422, "member_required")
    # 5. Checked, not left to the foreign key (a 500); RLS makes another business's id unknown.
    if new.client_id is not None:
        if db.scalar(text("SELECT 1 FROM clients WHERE id = :id"), {"id": new.client_id}) is None:
            raise ApiError(422, "unknown_client")
        client_id = new.client_id
    else:
        assert new.new_client is not None  # the validator: exactly one
        # A plain INSERT, never find_or_create: its upsert would attach to someone else's record.
        # Same transaction, so a 409 further down takes the new client with it.
        created = clients.insert(db, new.new_client)
        client_id = created.id
        auth.record(
            db,
            request,
            "client_created",
            actor_user_id=current.user_id,
            target=f"client:{client_id}",
        )
    # 6. A lapsed, unswept pending still sits in the exclusion predicate: 23P01 on an override.
    db.execute(EXPIRE, {"expiring": list(availability.EXPIRING)})
    # 7. No probing: unassigned, no hours and unknown member all read the same.
    hours, names = availability.candidates(db, new.service_id, new.member_id)
    if not hours:
        raise ApiError(409, "slot_unavailable")
    settings = business_settings.read(db)
    if new.override:  # `hours` holds exactly the named member (step 4)
        queue = sorted(hours)
    else:
        eligible, booked_rows = eligible_for(
            db,
            settings=settings,
            now=row.now,
            hours=hours,
            starts_at=new.starts_at,
            duration=row.duration_minutes,
            buffer_minutes=row.buffer_minutes,
        )
        if not eligible:
            raise ApiError(409, "slot_unavailable")
        day = new.starts_at.astimezone(ZoneInfo(settings.timezone)).date()
        queue = by_load(eligible, booked_rows, day, settings.timezone)
    origin_ip, user_agent = auth.origin(request)
    booking, worker_id = place(
        db,
        settings=settings,
        service=row,
        service_id=new.service_id,
        client_id=client_id,
        queue=queue,
        names=names,
        starts_at=new.starts_at,
        status="confirmed",
        expires_at=None,
        source="merchant",
        event={
            "ip": origin_ip,
            "user_agent": user_agent,
            "policy_version": None,
            "consent_purposes": None,  # never json.dumps(None): jsonb 'null' fails its CHECK
            "actor_user_id": current.user_id,
            "details": None,
        },
    )
    # A booking recorded after the fact (0026's walk-in) tells nobody: it is over.
    if booking.starts_at > row.now:
        # Unconditional: send_booking returns on a client with no address.
        email(
            db,
            current.tenant_id,
            booking.id,
            "booking_confirmed",
            extra={"starts_at": booking.starts_at.isoformat()},
        )
        remind(db, current.tenant_id, booking.id, booking.starts_at, row.now)
        # The booking's worker only, resolved AFTER "anyone" picked one; never email_merchants()
        # (it fans out to every owner).
        tell_team(
            db,
            current,
            booking.id,
            [db.scalar(WORKER_USER, {"id": worker_id})],
            "booking_new",
            extra={"starts_at": booking.starts_at.isoformat()},
        )
    auth.record(
        db,
        request,
        "booking_created",
        actor_user_id=current.user_id,
        target=f"booking:{booking.id}",
    )
    response.headers["Cache-Control"] = "no-store"
    logger.info("booking created", extra={"booking_id": str(booking.id)})
    return BookingOut(
        id=booking.id,
        status=booking.status,
        starts_at=booking.starts_at,
        ends_at=booking.ends_at,
        duration_minutes=row.duration_minutes,
        service_name=row.name,
        price=Price(amount_minor=row.price_amount_minor, currency=row.price_currency),
        worker_id=worker_id,
        worker_display_name=names[worker_id],
        cancellation_policy_text=settings.cancellation_policy_text or None,
    )


@merchant_router.post(
    "/bookings/{booking_id}/reschedule",
    name="reschedule",
    responses={s: {"model": Error} for s in (401, 403, 404, 409, 415, 422)},
)
def reschedule(
    booking_id: UUID,
    change: RescheduleIn,
    current: auth.CurrentSession,
    request: Request,
    response: Response,
) -> RescheduledOut:
    """A member moves a confirmed booking to another time and/or worker. A pending booking is not
    moved (D7): accept it first. The client's own reschedule allowance is not spent."""
    db = current.db
    db.execute(LOCK, {"key": LOCK_KEY})  # 1: nothing above this
    row = db.execute(BOOKING, {"id": booking_id}).first()
    if row is None:
        raise ApiError(404, "not_found")
    if not members.may_manage(current, row.worker_user_id):
        raise ApiError(403, "owner_only")
    worker_id = change.member_id or row.worker_id
    # A worker cannot hand a booking to a colleague: the new worker must pass the same rule.
    if worker_id != row.worker_id and not may_book_for(current, worker_id):
        raise ApiError(403, "owner_only")
    if row.status != "confirmed":  # D7; and a cancelled one is gone
        raise ApiError(409, "invalid_transition")
    if change.starts_at == row.starts_at and worker_id == row.worker_id:
        raise ApiError(422, "unchanged")
    db.execute(EXPIRE, {"expiring": list(availability.EXPIRING)})
    # Override or not: the member must do this service and have hours, or the snapshot name below
    # would name nobody and the exclusion constraint would be the only check left.
    hours, names = availability.candidates(db, row.service_id, worker_id)
    if not hours:
        raise ApiError(409, "slot_unavailable")
    if not change.override:
        service = db.execute(SERVICE, {"service_id": row.service_id}).first()
        if service is None:  # archived since it was booked
            raise ApiError(409, "slot_unavailable")
        eligible, _ = eligible_for(
            db,
            settings=business_settings.read(db),
            now=row.now,
            hours=hours,
            starts_at=change.starts_at,
            # The booking's own length, and its own slot free (exclude): as booking_links does.
            duration=(row.ends_at - row.starts_at) // timedelta(minutes=1),
            buffer_minutes=service.buffer_minutes,
            exclude=booking_id,
        )
        if not eligible:
            raise ApiError(409, "slot_unavailable")
    try:
        moved = db.execute(
            MOVE,
            {
                "id": booking_id,
                "new": change.starts_at,
                "worker": worker_id,
                "name": names[worker_id],
            },
        ).first()
    except IntegrityError as error:
        if (
            isinstance(error.orig, ExclusionViolation)
            and error.orig.diag.constraint_name == OVERLAP
        ):
            raise ApiError(409, "slot_taken") from None
        # The worker was REMOVED meanwhile (B5): this candidate went away, not a 500.
        if (
            isinstance(error.orig, ForeignKeyViolation)
            and error.orig.diag.constraint_name == members.BOOKINGS_WORKER
        ):
            raise ApiError(409, "slot_unavailable") from None
        raise
    if moved is None:  # lost a race under our own lock: the UPDATE's qualifier is the authority
        raise ApiError(409, "invalid_transition")
    origin_ip, user_agent = auth.origin(request)
    db.execute(
        INSERT_EVENT,
        {
            "booking_id": booking_id,
            "event": "rescheduled",
            "ip": origin_ip,
            "user_agent": user_agent,
            "policy_version": None,
            "consent_purposes": None,
            "actor_user_id": current.user_id,
            # UTC ISO strings, whatever zone the session's driver hands back (as booking_links).
            "details": json.dumps(
                {
                    "from": row.starts_at.astimezone(UTC).isoformat(),
                    "to": moved.starts_at.astimezone(UTC).isoformat(),
                }
            ),
        },
    )
    # A move into the past tells nobody (see book()).
    if moved.starts_at > row.now:
        new_iso = moved.starts_at.isoformat()
        # reschedule_count does not move, so `:r{count}` would dedupe a move back to an earlier
        # time; the transaction's clock is unique per move (they serialise on the tenant lock).
        stamp = f":m{row.now.timestamp()}"
        email(
            db,
            current.tenant_id,
            booking_id,
            "booking_confirmed",
            key_suffix=stamp,
            extra={"starts_at": new_iso},
        )
        remind(
            db, current.tenant_id, booking_id, moved.starts_at, row.now
        )  # the old one self-skips
        changed_member = worker_id != row.worker_id
        # The old worker hears it moved (away, when the member changed); the new one has a new
        # booking, not a move "now with" themselves. The actor is skipped either way.
        tell_team(
            db,
            current,
            booking_id,
            [row.worker_user_id],
            "booking_moved_team",
            key_suffix=stamp,
            extra={
                "starts_at": new_iso,
                "previous_starts_at": row.starts_at.isoformat(),
                **({"member_changed": "1"} if changed_member else {}),
            },
        )
        if changed_member:
            tell_team(
                db,
                current,
                booking_id,
                [db.scalar(WORKER_USER, {"id": worker_id})],
                "booking_new",
                key_suffix=stamp,
                extra={"starts_at": new_iso},
            )
    auth.record(
        db,
        request,
        "booking_rescheduled",
        actor_user_id=current.user_id,
        target=f"booking:{booking_id}",
    )
    response.headers["Cache-Control"] = "no-store"
    logger.info("booking rescheduled", extra={"booking_id": str(booking_id)})
    return RescheduledOut(
        id=moved.id, starts_at=moved.starts_at, ends_at=moved.ends_at, worker_id=moved.worker_id
    )


def sweep() -> int:
    """Expire every tenant's due pendings; the worker's housekeeping, never correctness (create()
    and availability.booked() already treat them as gone). Returns how many it settled."""
    # The ids in their own short session, closed before the loop: the worker's pool is exactly 21.
    with SessionLocal() as session:
        tenant_ids = session.scalars(text("SELECT id FROM tenants")).all()
    expired = 0
    with tracing.span("bookings.sweep", SpanKind.INTERNAL, {"tenants": len(tenant_ids)}) as span:
        # ponytail: one transaction per tenant per sweep; upgrade to a SECURITY DEFINER "tenants
        # with due expiry" once the tenant count matters. A failure raises: the worker retries the
        # whole sweep next poll, so a tenant that always fails blocks the ones after it (and pings).
        for tenant_id in tenant_ids:
            with tenant_context(tenant_id) as db:
                db.execute(LOCK, {"key": LOCK_KEY})  # 1: first, like every booking writer
                # RETURNING, not rowcount: Session.execute gives a Result, which has none (mypy).
                count = len(db.execute(EXPIRE, {"expiring": list(availability.EXPIRING)}).all())
                if count:
                    logger.info("bookings expired", extra={"count": count})
                expired += count
        span.set_attribute("expired", expired)
    return expired
