"""Platform admins find and inspect businesses (ZIF-73), read-only.

Both routes sit on admin_auth.admin_router: the second factor passed. Neither reads a client's
personal data: no clients, bookings, consents or booking links, and no address or browser from the
audit log. A business's created date is its uuidv7 id's.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Query, Request
from pydantic import BaseModel
from sqlalchemy import text

from app import business_settings
from app.admin_auth import CurrentAdmin, admin_router, business
from app.business_settings import BusinessSettings
from app.db import SessionLocal, join_tenant
from app.errors import Error

PAGE_SIZE = 25
RECENT_EVENTS = 20

Stripe = Literal["none", "onboarding", "ready"]


class BusinessRow(BaseModel):
    id: UUID
    name: str
    slug: str
    country: str
    created_at: datetime | None  # None for an id that carries no time (not uuidv7)
    members: int  # live members: owners and workers
    stripe: Stripe  # from the stored account, never a call to Stripe


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
    actor_user_id: UUID | None
    target: str | None


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


TENANT = """
SELECT id, name, slug, country, currency, uuid_extract_timestamp(id) AS created_at
FROM tenants
"""
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
        total = session.scalar(
            text(f"SELECT count(*) FROM tenants {MATCHES}"), {"pattern": pattern}
        )
        rows = session.execute(
            text(f"{TENANT} {MATCHES} ORDER BY id DESC LIMIT :limit OFFSET :offset"),
            {"pattern": pattern, "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE},
        ).all()
        businesses = []
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
        # Never ip or user_agent: a member's or a client's address and browser.
        events = session.execute(
            text("""
            SELECT id, created_at, action, actor_user_id, target
            FROM audit_events
            ORDER BY id DESC  -- uuidv7: newest first
            LIMIT :limit
            """),
            {"limit": RECENT_EVENTS},
        ).all()
        settings = business_settings.read(session)
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
    )
