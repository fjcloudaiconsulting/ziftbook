"""Platform admins find, inspect, suspend and reactivate businesses (ZIF-73).

The two reads sit on admin_auth.admin_router (the second factor passed), the two writes on
admin_write_router (passed within 15 minutes). None reads a client's personal data: no clients,
consents or booking links, and no address or browser from the audit log. A suspended business shows
its upcoming confirmed times and their payments, never a booking id or anything about the client.
A business's created date is its uuidv7 id's.
"""

import logging
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Query, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth, business_settings, services
from app.admin_auth import ADMIN_COOKIE, CurrentAdmin, admin_router, admin_write_router, business
from app.bookings import PAYMENT_COLUMNS, PaymentFact, payment_fact
from app.business_settings import BusinessSettings
from app.db import SessionLocal, join_tenant
from app.errors import ApiError, Error
from app.jobs import enqueue

logger = logging.getLogger(__name__)

PAGE_SIZE = 25
RECENT_EVENTS = 20
UPCOMING_LIMIT = 100

Stripe = Literal["none", "onboarding", "ready"]


class BusinessRow(BaseModel):
    id: UUID
    name: str
    slug: str
    country: str
    created_at: datetime | None  # None for an id that carries no time (not uuidv7)
    members: int  # live members: owners and workers
    stripe: Stripe  # from the stored account, never a call to Stripe
    suspended_at: datetime | None


class BusinessPage(BaseModel):
    businesses: list[BusinessRow]
    total: int
    page_size: int


class MemberRow(BaseModel):
    user_id: UUID
    name: str | None  # the business's display name for them, else their account's
    role: Literal["owner", "worker"]


class EventRow(BaseModel):
    id: UUID
    created_at: datetime
    action: str
    actor_user_id: UUID | None  # only a live member's (listed in members): never a client account's
    target_kind: str | None  # "client", "booking", "setting"...: never the id after the colon


class UpcomingBooking(BaseModel):
    # Exactly these: no booking id and nothing about the client.
    starts_at: datetime
    ends_at: datetime
    service_name: dict[str, str]
    worker_display_name: str | None
    payment: PaymentFact | None


class BusinessDetail(BaseModel):
    id: UUID
    name: str
    slug: str
    country: str
    currency: str
    created_at: datetime | None
    stripe: Stripe
    members: list[MemberRow]
    settings: BusinessSettings
    events: list[EventRow]  # the latest RECENT_EVENTS, newest first
    suspended_at: datetime | None
    suspension_reason: str | None
    # Confirmed bookings not yet over (the soonest UPCOMING_LIMIT), only while suspended: the
    # admin's list of what the owner still has to honour. None while active.
    upcoming: list[UpcomingBooking] | None
    upcoming_total: int


TENANT = """
SELECT id, name, slug, country, currency, uuid_extract_timestamp(id) AS created_at,
       suspended_at, suspension_reason
FROM tenants
"""
# QUEUE's payment join (bookings.py): at most one live payment per booking, so no row is added. In
# progress counts as upcoming: the booking is not over. No client, no booking id.
UPCOMING = text(f"""
SELECT b.starts_at, b.ends_at, b.service_name, b.worker_display_name, {PAYMENT_COLUMNS},
       count(*) OVER () AS total
FROM bookings b
LEFT JOIN booking_payments p
  ON p.tenant_id = b.tenant_id AND p.booking_id = b.id AND p.status <> 'canceled'
WHERE b.status = 'confirmed' AND b.ends_at > now()
ORDER BY b.starts_at, b.id
LIMIT :limit
""")
# A business's own rows: run with app.tenant_id set, so row-level security picks the business.
COUNTS = text("""
SELECT (SELECT count(*) FROM memberships WHERE role <> 'removed') AS members,
       (SELECT charges_enabled FROM payment_accounts) AS charges_enabled,
       EXISTS (SELECT FROM payment_accounts) AS connected
""")
MATCHES = "WHERE CAST(:pattern AS text) IS NULL OR name ILIKE :pattern OR slug ILIKE :pattern"


def stripe(connected: bool, charges_enabled: bool | None) -> Stripe:
    if not connected:
        return "none"
    return "ready" if charges_enabled else "onboarding"


def like(q: str) -> str | None:
    """A contains-pattern for q, its LIKE wildcards matched literally; None for a blank search."""
    q = q.strip()
    if not q:
        return None
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@admin_router.get("/businesses", responses={s: {"model": Error} for s in (401, 403, 422)})
def list_businesses(
    current: CurrentAdmin,
    q: Annotated[str, Query(max_length=100)] = "",
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
) -> BusinessPage:
    """Businesses whose name or slug contains q, newest first, PAGE_SIZE a page.

    Not recorded per business: it shows no business's people or settings, only counts. Each row's
    counts are read inside that business (join_tenant), as data migrations walk businesses, so the
    app role needs no new grant and row-level security still decides what it sees.
    """
    pattern = like(q)
    with SessionLocal.begin() as session:
        # One snapshot for the count, the page and its counts: a business created between them
        # would otherwise skew total against the rows.
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        total = session.scalar(
            text(f"SELECT count(*) FROM tenants {MATCHES}"), {"pattern": pattern}
        )
        rows = session.execute(
            text(f"{TENANT} {MATCHES} ORDER BY id DESC LIMIT :limit OFFSET :offset"),
            {"pattern": pattern, "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE},
        ).all()
        businesses = []
        # shortcut: two statements per listed business (set_config, COUNTS), about 50 a page, fine
        # for an admin-only list of PAGE_SIZE rows. Upgrade to one set-based read (a reviewed
        # definer function) if the page grows or this shows in latency.
        for row in rows:
            join_tenant(session, row.id)
            counts = session.execute(COUNTS).one()
            businesses.append(
                BusinessRow(
                    id=row.id,
                    name=row.name,
                    slug=row.slug,
                    country=row.country,
                    created_at=row.created_at,
                    members=counts.members,
                    stripe=stripe(counts.connected, counts.charges_enabled),
                    suspended_at=row.suspended_at,
                )
            )
    return BusinessPage(businesses=businesses, total=total or 0, page_size=PAGE_SIZE)


@admin_router.get(
    "/businesses/{tenant_id}", responses={s: {"model": Error} for s in (401, 403, 404, 422)}
)
def read_business(tenant_id: UUID, current: CurrentAdmin, request: Request) -> BusinessDetail:
    """One business: its members and their roles, its settings and its latest audit events.
    Recorded as business_read before anything is read (admin_auth.business)."""
    with business(current, request, tenant_id, target="business_detail") as session:
        row = session.execute(text(f"{TENANT} WHERE id = :t"), {"t": tenant_id}).one()
        counts = session.execute(COUNTS).one()
        members = session.execute(
            text("""
            SELECT m.user_id, COALESCE(m.display_name, u.name) AS name, m.role
            FROM memberships m JOIN users u ON u.id = m.user_id
            WHERE m.role <> 'removed'
            ORDER BY m.id
            """)
        ).all()
        # Never ip, user_agent or details, nor a target's id (client:<id>, booking:<id>), nor an
        # actor who is not a live member (a client's account): no client is identified here.
        events = session.execute(
            text("""
            SELECT id, created_at, action,
                   CASE WHEN actor_user_id IN (
                     SELECT user_id FROM memberships WHERE role <> 'removed'
                   ) THEN actor_user_id END AS actor_user_id,
                   CASE WHEN position(':' IN target) > 0
                     THEN split_part(target, ':', 1) END AS target_kind
            FROM audit_events
            ORDER BY id DESC  -- uuidv7: newest first
            LIMIT :limit
            """),
            {"limit": RECENT_EVENTS},
        ).all()
        settings = business_settings.read(session)
        upcoming = (
            session.execute(UPCOMING, {"limit": UPCOMING_LIMIT}).all() if row.suspended_at else None
        )
    return BusinessDetail(
        id=row.id,
        name=row.name,
        slug=row.slug,
        country=row.country,
        currency=row.currency,
        created_at=row.created_at,
        stripe=stripe(counts.connected, counts.charges_enabled),
        members=[MemberRow.model_validate(m, from_attributes=True) for m in members],
        settings=settings,
        events=[EventRow.model_validate(e, from_attributes=True) for e in events],
        suspended_at=row.suspended_at,
        suspension_reason=row.suspension_reason,
        upcoming=None
        if upcoming is None
        else [
            UpcomingBooking(
                starts_at=u.starts_at,
                ends_at=u.ends_at,
                service_name=u.service_name,
                worker_display_name=u.worker_display_name,
                payment=payment_fact(u),
            )
            for u in upcoming
        ],
        upcoming_total=upcoming[0].total if upcoming else 0,
    )


class Reason(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    # As services.DescriptionText: stripped, never blank, no control characters (a NUL would be a
    # 500 from the database), and the CHECK's 500.
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
        AfterValidator(services.multiline),
    ]


# What each function answers other than its success, as the error it becomes.
REFUSALS = {
    "stale": (403, "second_factor_stale"),
    "already_suspended": (409, "already_suspended"),
    "not_suspended": (409, "not_suspended"),
}
WRITE_ERRORS: dict[int | str, dict[str, Any]] = {
    s: {"model": Error} for s in (401, 403, 404, 409, 415, 422, 503)
}


def tell_owners(db: Session, tenant_id: UUID, template: str, suspended_at: datetime) -> None:
    """One email.send per live owner, in the change's transaction. The payload holds the
    suspension's timestamp, never its reason: mail.send reads the reason from the business, and
    skips a mail the business has since moved past (suspended again, reactivated)."""
    stamp = suspended_at.isoformat()
    for user_id in db.scalars(text("SELECT user_id FROM memberships WHERE role = 'owner'")):
        enqueue(
            db,
            "email.send",
            f"email.send:{tenant_id}:{template}:{stamp}:{user_id}",
            {"recipient_id": str(user_id), "template": template, "suspended_at": stamp},
            tenant_id=tenant_id,
        )


def change(
    current: CurrentAdmin,
    request: Request,
    tenant_id: UUID,
    body: Reason,
    *,
    action: Literal["business_suspended", "business_reactivated"],
    sql: str,
    template: str,
) -> None:
    """The function, the audit row and the owners' emails in one transaction (admin_auth.business):
    a refusal or a failure rolls all three back."""
    with business(
        current, request, tenant_id, target="business", action=action, reason=body.reason
    ) as db:
        row = db.execute(
            text(sql),
            {
                "h": auth.hash_token(request.cookies[ADMIN_COOKIE]),
                "reason": body.reason,
            },
        ).one()
        if row.outcome in REFUSALS:
            raise ApiError(*REFUSALS[row.outcome])
        tell_owners(db, tenant_id, template, row.suspended_at)
    logger.info("business suspended" if action == "business_suspended" else "business reactivated")


@admin_write_router.post(
    "/businesses/{tenant_id}/suspension", status_code=204, responses=WRITE_ERRORS
)
def suspend(tenant_id: UUID, body: Reason, current: CurrentAdmin, request: Request) -> None:
    """Suspend a business, with the reason its owners are shown. 409 when it already is."""
    change(
        current,
        request,
        tenant_id,
        body,
        action="business_suspended",
        sql="SELECT * FROM suspend_business(:h, :reason)",
        template="business_suspended",
    )


@admin_write_router.post(
    "/businesses/{tenant_id}/reactivation", status_code=204, responses=WRITE_ERRORS
)
def reactivate(tenant_id: UUID, body: Reason, current: CurrentAdmin, request: Request) -> None:
    """Lift a suspension. The reason is the admin's, kept in the audit log only. 409 when the
    business is not suspended."""
    change(
        current,
        request,
        tenant_id,
        body,
        action="business_reactivated",
        sql="SELECT * FROM reactivate_business(:h)",
        template="business_reactivated",
    )
