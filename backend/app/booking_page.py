"""GET /api/public/booking-pages/{slug}: what a business's public booking page shows (ZIF-56).
Public: no session, no cookie."""

import re
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text

from app import business_settings, clients, limits
from app.availability import WorkerOut
from app.business_settings import Locale
from app.db import SessionLocal, join_tenant
from app.errors import ApiError, Error
from app.services import Price

router = APIRouter(prefix="/api/public", tags=["booking-page"])
LIMIT, LIMIT_WINDOW = 60, timedelta(minutes=1)
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")  # slug_ok()'s format (migration 0030)

# The worker filter is availability.CANDIDATES's (assigned AND has working hours), so a listed
# worker is exactly one the availability GET can offer. Inner joins: a service nobody can perform
# is a dead end for a client and is not listed.
# ponytail: tens of services, no pagination.
SERVICES = text("""
SELECT s.id, s.name, s.description, s.duration_minutes,
       s.price_amount_minor, s.price_currency,
       m.id AS member_id, m.display_name
FROM services s
JOIN service_workers sw ON sw.tenant_id = s.tenant_id AND sw.service_id = s.id
JOIN memberships m ON m.tenant_id = sw.tenant_id AND m.id = sw.member_id
WHERE s.archived_at IS NULL
  AND EXISTS (SELECT 1 FROM working_hours w WHERE w.tenant_id = m.tenant_id AND w.member_id = m.id)
ORDER BY s.id, m.display_name NULLS LAST, m.id
""")


class CancellationOut(BaseModel):
    text: str | None  # the business's own language only; null when it publishes no terms
    free_cancellation_hours: int
    reschedule_cutoff_hours: int
    max_reschedules: int


class PublicServiceOut(BaseModel):
    id: UUID
    name: dict[Locale, str]
    description: dict[Locale, str]
    duration_minutes: int
    price: Price
    workers: list[WorkerOut]  # display_name (nulls last), then id; never empty


class BookingPageOut(BaseModel):
    id: UUID
    slug: str
    name: str
    timezone: str
    language: Locale
    booking_horizon_days: int
    policy_version: str
    cancellation: CancellationOut
    services: list[PublicServiceOut]


@router.get(
    "/booking-pages/{slug}", name="read", responses={s: {"model": Error} for s in (404, 422, 429)}
)
def read(slug: str, request: Request) -> BookingPageOut:
    ip = request.client.host if request.client else None
    # First, before parsing or any lookup. Per IP only: a per-business key would let anyone block a
    # page. Its own key, so page loads don't spend the availability budget.
    if limits.hit({limits.ip_key("booking_page", ip): LIMIT}, LIMIT_WINDOW):
        raise ApiError(429, "rate_limited")
    # Before lower(): Python's str.lower() is Unicode-aware, so a non-ASCII letter could fold to
    # an ASCII one under a different code point (e.g. the Kelvin sign %E2%84%AA -> "k"), aliasing
    # a stored slug. No slug_ok() slug is ever non-ASCII, so this is simply not found.
    if not slug.isascii():
        raise ApiError(404, "not_found")
    slug = slug.lower()
    # Reserved words need no check: they are never stored, so they are simply not found.
    if len(slug) > 40 or not SLUG.fullmatch(slug):
        raise ApiError(404, "not_found")
    with SessionLocal.begin() as db:
        tenant = db.execute(
            text("SELECT id, slug, name FROM tenants WHERE slug = :slug"), {"slug": slug}
        ).first()
        if tenant is None:
            raise ApiError(404, "not_found")
        join_tenant(db, tenant.id)
        request.state.tenant_id = tenant.id
        settings = business_settings.read(db)
        services: dict[UUID, PublicServiceOut] = {}
        for row in db.execute(SERVICES):
            if row.id not in services:
                services[row.id] = PublicServiceOut(
                    id=row.id,
                    name=row.name,
                    description=row.description,
                    duration_minutes=row.duration_minutes,
                    price=Price(amount_minor=row.price_amount_minor, currency=row.price_currency),
                    workers=[],
                )
            services[row.id].workers.append(
                WorkerOut(id=row.member_id, display_name=row.display_name)
            )
    return BookingPageOut(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        timezone=settings.timezone,
        language=settings.language,
        booking_horizon_days=settings.booking_horizon_days,
        policy_version=clients.current_policy_version(),
        cancellation=CancellationOut(
            text=settings.cancellation_policy_text or None,
            free_cancellation_hours=settings.free_cancellation_hours,
            reschedule_cutoff_hours=settings.reschedule_cutoff_hours,
            max_reschedules=settings.max_reschedules,
        ),
        services=list(services.values()),
    )
