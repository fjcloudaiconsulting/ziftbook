"""Platform admins edit plans, the billing settings, and a business's overrides, plan offers and
free plan (ZIF-78).

Reads are on admin_auth.admin_router, writes on admin_write_router. A write validates first, with
no transaction open: the values against the registry (entitlements.validate), a Price or a coupon
with Stripe. Then, in one transaction, its definer function (0106, which re-checks the admin's
second factor), its audit event with the reason, and billing.enforce for every business whose
entitlements may have changed: a refusal rolls all of it back. A business's own write and read go
through admin_auth.business, which records first.
"""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Path, Request
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import admin, enforcement, stripe_gateway
from app.admin_auth import (
    CurrentAdmin,
    admin_router,
    admin_write_router,
    business,
    session_hash,
)
from app.admin_businesses import WRITE_ERRORS, Reason, ReasonText
from app.db import SessionLocal, join_tenant
from app.entitlements import FEATURES, Entitlements, Kind, entitlements, forget, validate
from app.errors import ApiError, Error
from app.services import NameText

logger = logging.getLogger(__name__)

READ_ERRORS: dict[int | str, dict[str, Any]] = {s: {"model": Error} for s in (401, 403, 404, 422)}

# The CHECKs on plans (0103), so a bad one is a 422, not a 500.
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,40}$")]
PriceId = Annotated[str, StringConstraints(pattern=r"^price_[A-Za-z0-9]{1,64}$")]
# Stripe's generated ids, and the custom ones its dashboard takes.
CouponId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
FeatureKey = Annotated[str, Path(max_length=64)]


def ahead(value: datetime | None) -> datetime | None:
    if value is not None and value <= datetime.now(UTC):
        raise ValueError("not in the future")
    return value


# An expiry or an end: None for open-ended, else still ahead.
Until = Annotated[AwareDatetime | None, AfterValidator(ahead)]


class PlanName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    en: NameText
    nl: NameText
    pt: NameText


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: Slug
    name: PlanName
    stripe_price_id: PriceId | None  # None: nobody pays for it (Free)
    values: dict[str, Any]  # checked against the registry by checked(), naming the key refused
    public: StrictBool
    reason: ReasonText


class PlanOut(BaseModel):
    id: UUID
    slug: str
    name: dict[str, str]
    stripe_price_id: str | None
    values: dict[str, bool | int]  # only the keys the plan sets: the rest are the defaults
    public: bool
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class FeatureOut(BaseModel):
    key: str
    kind: Kind
    default: bool | int
    description: str


class PlansOut(BaseModel):
    plans: list[PlanOut]  # oldest first, archived ones included
    features: list[FeatureOut]  # the registry, in its order: what the editor offers


class BillingSettings(BaseModel):
    default_plan_id: UUID
    trial_plan_id: UUID | None  # None: no trial
    trial_days: int


class BillingSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_plan_id: UUID
    trial_plan_id: UUID | None
    trial_days: Annotated[int, Field(ge=0, le=90)]
    reason: ReasonText


class OverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any  # checked against the key's kind by checked()
    expires_at: Until
    reason: ReasonText


class OfferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coupon_id: CouponId | None
    reason: ReasonText


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: UUID
    ends_at: Until
    reason: ReasonText


class OverrideOut(BaseModel):
    key: str
    value: bool | int
    expires_at: datetime | None  # past: ignored by entitlements, kept until removed
    reason: str
    admin_user_id: UUID
    created_at: datetime


class OfferOut(BaseModel):
    plan_id: UUID
    coupon_id: str | None
    reason: str
    admin_user_id: UUID
    created_at: datetime


class GrantOut(BaseModel):
    plan_id: UUID
    ends_at: datetime | None
    reason: str
    admin_user_id: UUID
    created_at: datetime


class SubscriptionOut(BaseModel):
    plan_id: UUID | None  # None: a Price no plan carries
    status: str
    current_period_end: datetime | None
    cancel_at_period_end: bool
    stripe_customer_id: str
    stripe_subscription_id: str


class BusinessBilling(BaseModel):
    effective: Entitlements  # what the business may use now, and where it comes from
    subscription: SubscriptionOut | None
    overrides: list[OverrideOut]
    offers: list[OfferOut]
    grant: GrantOut | None


PLAN = """
SELECT id, slug, name, stripe_price_id, values, public, archived_at, created_at, updated_at
FROM plans
"""
SETTINGS = text("SELECT default_plan_id, trial_plan_id, trial_days FROM platform_billing")


def checked(values: dict[str, Any]) -> dict[str, bool | int]:
    """values as entitlements.validate takes them, or 422 invalid_values naming the key refused."""
    for key, value in values.items():
        try:
            validate({key: value})
        except ValueError:
            raise ApiError(422, "invalid_values", key=key) from None
    return validate(values)


def check_price(price_id: str) -> None:
    """422 unless the Price is one Checkout sells as a subscription in EUR."""
    try:
        price = stripe_gateway.gateway.retrieve_price(price_id)
    except stripe_gateway.Failed as failure:
        if failure.kind == "invalid":  # Stripe doesn't know it
            raise ApiError(422, "invalid_price") from None
        raise ApiError(503, "payments_unavailable") from None
    if not (price.active and price.recurring and price.currency.lower() == "eur"):
        raise ApiError(422, "invalid_price")


def check_coupon(coupon_id: str) -> None:
    """422 unless Stripe still applies the coupon (not deleted, expired or used up) to an EUR
    Price."""
    try:
        coupon = stripe_gateway.gateway.retrieve_coupon(coupon_id)
    except stripe_gateway.Failed as failure:
        if failure.kind == "invalid":
            raise ApiError(422, "invalid_coupon") from None
        raise ApiError(503, "payments_unavailable") from None
    # An amount off in another currency: Checkout would refuse it on an EUR Price.
    if not coupon.valid or (
        coupon.amount_off is not None and (coupon.currency or "").lower() != "eur"
    ):
        raise ApiError(422, "invalid_coupon")


def refuse(outcome: str, conflict: str) -> None:
    """A definer's answer other than ok, as the error it becomes."""
    if outcome == "stale":
        raise ApiError(403, "second_factor_stale")
    if outcome == "not_found":
        raise ApiError(404, "not_found")
    if outcome == "conflict":
        raise ApiError(409, conflict)
    if outcome == "in_use":  # admin_save_plan: a plan's set Price moved or cleared
        raise ApiError(409, "plan_in_use")
    if outcome != "ok":  # mismatch: called outside the business's tenant_context, a bug
        raise RuntimeError(f"definer answered {outcome}")


def enforce_where(db: Session, on: Callable[[Entitlements], bool]) -> None:
    """billing.enforce for every business whose entitlements, read inside it (join_tenant, as
    list_businesses does), match: row-level security keeps the app role from one read across
    businesses. Leaves db in the last business's tenant: the caller commits next."""
    # shortcut: two statements per business on every plan or settings write, fine while there are
    # hundreds. Upgrade to one set-based read (a reviewed definer function) when it shows in the
    # write's latency. It holds the plan's or platform_billing's lock while each enqueue takes KEY
    # SHARE on a tenants row, so a long currency change on one business can make the write hit
    # lock_timeout (503 busy).
    for tenant_id in db.scalars(text("SELECT id FROM tenants ORDER BY id")).all():
        join_tenant(db, tenant_id)
        forget(db)
        if on(entitlements(db)):
            enforcement.enqueue(db, tenant_id)
    forget(db)


def plan_out(db: Session, plan_id: UUID) -> PlanOut:
    row = db.execute(text(f"{PLAN} WHERE id = :id"), {"id": plan_id}).one()
    return PlanOut.model_validate(row, from_attributes=True)


@admin_router.get("/plans", responses={s: {"model": Error} for s in (401, 403)})
def list_plans(current: CurrentAdmin) -> PlansOut:
    """Every plan, and the feature registry the editor is built from."""
    with SessionLocal.begin() as db:
        rows = db.execute(text(f"{PLAN} ORDER BY created_at, id")).all()
    return PlansOut(
        plans=[PlanOut.model_validate(row, from_attributes=True) for row in rows],
        features=[
            FeatureOut(key=key, kind=f.kind, default=f.default, description=f.description)
            for key, f in FEATURES.items()
        ],
    )


def save_plan(
    current: CurrentAdmin, request: Request, plan_id: UUID | None, body: PlanIn
) -> PlanOut:
    values = checked(body.values)
    stored = None
    if plan_id is not None:
        with SessionLocal.begin() as db:  # closed before Stripe is asked
            stored = db.scalar(
                text("SELECT stripe_price_id FROM plans WHERE id = :id"), {"id": plan_id}
            )
    # Only a new Price is asked about: a values edit must not wait on Stripe, nor on a Price
    # deactivated since (its subscribers keep it).
    if body.stripe_price_id is not None and body.stripe_price_id != stored:
        check_price(body.stripe_price_id)
    with SessionLocal.begin() as db:
        row = db.execute(
            text("""
            SELECT * FROM admin_save_plan(:h, :id, :slug, CAST(:name AS jsonb), :price,
                                          CAST(:values AS jsonb), :public)
            """),
            {
                "h": session_hash(request),
                "id": plan_id,
                "slug": body.slug,
                "name": body.name.model_dump_json(),
                "price": body.stripe_price_id,
                "values": json.dumps(values),
                "public": body.public,
            },
        ).one()
        refuse(row.outcome, "plan_taken")  # its slug or its Price is another plan's
        admin.record(
            db,
            request,
            "plan_saved",
            actor_user_id=current.user_id,
            target=f"plan:{row.id}",
            reason=body.reason,
            details={
                "slug": body.slug,
                "stripe_price_id": body.stripe_price_id,
                "public": body.public,
                "values": values,
            },
        )
        # A plan's values apply to every business on it at once (no grandfathering).
        enforce_where(db, lambda found: found.plan_id == row.id)
        saved = plan_out(db, row.id)
    logger.info("plan saved", extra={"plan_id": str(row.id)})
    return saved


@admin_write_router.post("/plans", status_code=201, responses=WRITE_ERRORS)
def create_plan(body: PlanIn, current: CurrentAdmin, request: Request) -> PlanOut:
    """A new plan. 409 plan_taken when its slug or Price is another plan's."""
    return save_plan(current, request, None, body)


@admin_write_router.put("/plans/{plan_id}", responses=WRITE_ERRORS)
def update_plan(plan_id: UUID, body: PlanIn, current: CurrentAdmin, request: Request) -> PlanOut:
    """Change a plan, archived or not: its values reach every business on it at once. 409
    plan_in_use for a Price moved or cleared once set: make a new plan instead."""
    return save_plan(current, request, plan_id, body)


@admin_write_router.post("/plans/{plan_id}/archive", responses=WRITE_ERRORS)
def archive_plan(plan_id: UUID, body: Reason, current: CurrentAdmin, request: Request) -> PlanOut:
    """Stop selling a plan. Businesses on it keep it; 409 plan_is_default for the default or
    trial plan."""
    with SessionLocal.begin() as db:
        refuse(
            db.scalar(
                text("SELECT admin_archive_plan(:h, :id)"),
                {"h": session_hash(request), "id": plan_id},
            ),
            "plan_is_default",
        )
        admin.record(
            db,
            request,
            "plan_archived",
            actor_user_id=current.user_id,
            target=f"plan:{plan_id}",
            reason=body.reason,
        )
        enforce_where(db, lambda found: found.plan_id == plan_id)
        archived = plan_out(db, plan_id)
    logger.info("plan archived", extra={"plan_id": str(plan_id)})
    return archived


@admin_router.get("/billing", responses={s: {"model": Error} for s in (401, 403)})
def read_billing(current: CurrentAdmin) -> BillingSettings:
    """The default plan, and the trial's plan and days for a new business."""
    with SessionLocal.begin() as db:
        row = db.execute(SETTINGS).one()
    return BillingSettings.model_validate(row, from_attributes=True)


@admin_write_router.put("/billing", responses=WRITE_ERRORS)
def change_billing(
    body: BillingSettingsIn, current: CurrentAdmin, request: Request
) -> BillingSettings:
    """Set the default and trial plans and the trial's days. A running trial follows the trial
    plan at once (0103). 409 plan_unavailable for an archived plan."""
    with SessionLocal.begin() as db:
        refuse(
            db.scalar(
                text("SELECT admin_set_billing(:h, :default, :trial, :days)"),
                {
                    "h": session_hash(request),
                    "default": body.default_plan_id,
                    "trial": body.trial_plan_id,
                    "days": body.trial_days,
                },
            ),
            "plan_unavailable",
        )
        admin.record(
            db,
            request,
            "billing_settings_changed",
            actor_user_id=current.user_id,
            target="platform_billing",
            reason=body.reason,
            details={
                "default_plan_id": str(body.default_plan_id),
                "trial_plan_id": None if body.trial_plan_id is None else str(body.trial_plan_id),
                "trial_days": body.trial_days,
            },
        )
        enforce_where(db, lambda found: found.source in ("default", "trial"))
        row = db.execute(SETTINGS).one()
    logger.info("billing settings changed")
    return BillingSettings.model_validate(row, from_attributes=True)


@admin_router.get("/businesses/{tenant_id}/billing", responses=READ_ERRORS)
def read_business_billing(
    tenant_id: UUID, current: CurrentAdmin, request: Request
) -> BusinessBilling:
    """A business's effective plan, subscription, overrides, offers and free plan. Recorded as
    business_read before anything is read (admin_auth.business)."""
    with business(current, request, tenant_id, target="business_billing") as db:
        found = entitlements(db)
        subscription = db.execute(
            text("""
            SELECT plan_id, status, current_period_end, cancel_at_period_end,
                   stripe_customer_id, stripe_subscription_id
            FROM subscriptions
            """)
        ).first()
        overrides = db.execute(
            text("""
            SELECT key, value, expires_at, reason, admin_user_id, created_at
            FROM business_overrides ORDER BY key
            """)
        ).all()
        offers = db.execute(
            text("""
            SELECT plan_id, stripe_coupon_id AS coupon_id, reason, admin_user_id, created_at
            FROM plan_offers ORDER BY created_at, id
            """)
        ).all()
        grant = db.execute(
            text("SELECT plan_id, ends_at, reason, admin_user_id, created_at FROM plan_grants")
        ).first()
    return BusinessBilling(
        effective=found,
        subscription=None
        if subscription is None
        else SubscriptionOut.model_validate(subscription, from_attributes=True),
        overrides=[OverrideOut.model_validate(o, from_attributes=True) for o in overrides],
        offers=[OfferOut.model_validate(o, from_attributes=True) for o in offers],
        grant=None if grant is None else GrantOut.model_validate(grant, from_attributes=True),
    )


def change_business(
    current: CurrentAdmin,
    request: Request,
    tenant_id: UUID,
    *,
    action: admin.Action,
    target: str,
    reason: str,
    details: dict[str, object] | None,
    sql: str,
    params: dict[str, object],
) -> None:
    """The audit row (admin_auth.business records first), the definer and the places check in one
    transaction; 404 for an unknown business."""
    with business(
        current, request, tenant_id, target=target, action=action, reason=reason, details=details
    ) as db:
        outcome = db.scalar(text(sql), {"h": session_hash(request), "t": tenant_id, **params})
        refuse(outcome, "plan_unavailable")  # an archived plan, or an offer of one with no Price
        enforcement.enqueue(db, tenant_id)
    logger.info("business billing changed", extra={"action": action})


@admin_write_router.put(
    "/businesses/{tenant_id}/overrides/{key}", status_code=204, responses=WRITE_ERRORS
)
def set_override(
    tenant_id: UUID, key: FeatureKey, body: OverrideIn, current: CurrentAdmin, request: Request
) -> None:
    """Set one feature's value for this business, over its plan's, until expires_at (None: until
    removed)."""
    value = checked({key: body.value})[key]
    change_business(
        current,
        request,
        tenant_id,
        action="override_set",
        target=f"override:{key}",
        reason=body.reason,
        details={"value": value, "expires_at": body.expires_at and body.expires_at.isoformat()},
        sql="SELECT admin_set_override(:h, :t, :key, CAST(:value AS jsonb), :expires, :reason)",
        params={
            "key": key,
            "value": json.dumps(value),
            "expires": body.expires_at,
            "reason": body.reason,
        },
    )


@admin_write_router.delete(
    "/businesses/{tenant_id}/overrides/{key}", status_code=204, responses=WRITE_ERRORS
)
def remove_override(
    tenant_id: UUID, key: FeatureKey, body: Reason, current: CurrentAdmin, request: Request
) -> None:
    """Remove one feature's override: the plan's value applies again. 404 when there is none."""
    change_business(
        current,
        request,
        tenant_id,
        action="override_removed",
        target=f"override:{key}",
        reason=body.reason,
        details=None,
        sql="SELECT admin_remove_override(:h, :t, :key)",
        params={"key": key},
    )


@admin_write_router.post(
    "/businesses/{tenant_id}/offers/{plan_id}", status_code=204, responses=WRITE_ERRORS
)
def offer_plan(
    tenant_id: UUID, plan_id: UUID, body: OfferIn, current: CurrentAdmin, request: Request
) -> None:
    """Offer a plan (private or public) to this business, with a Stripe coupon Checkout applies.
    Offering it again replaces the coupon. 409 plan_unavailable for an archived or unpriced plan."""
    if body.coupon_id is not None:
        check_coupon(body.coupon_id)
    change_business(
        current,
        request,
        tenant_id,
        action="plan_offered",
        target=f"plan:{plan_id}",
        reason=body.reason,
        details={"coupon_id": body.coupon_id},
        sql="SELECT admin_offer_plan(:h, :t, :plan, :coupon, :reason)",
        params={"plan": plan_id, "coupon": body.coupon_id, "reason": body.reason},
    )


@admin_write_router.delete(
    "/businesses/{tenant_id}/offers/{plan_id}", status_code=204, responses=WRITE_ERRORS
)
def withdraw_offer(
    tenant_id: UUID, plan_id: UUID, body: Reason, current: CurrentAdmin, request: Request
) -> None:
    """Withdraw an offer. A subscription already on the plan stays. 404 when there is none."""
    change_business(
        current,
        request,
        tenant_id,
        action="plan_offer_withdrawn",
        target=f"plan:{plan_id}",
        reason=body.reason,
        details=None,
        sql="SELECT admin_withdraw_offer(:h, :t, :plan)",
        params={"plan": plan_id},
    )


@admin_write_router.put("/businesses/{tenant_id}/grant", status_code=204, responses=WRITE_ERRORS)
def grant_plan(tenant_id: UUID, body: GrantIn, current: CurrentAdmin, request: Request) -> None:
    """Give this business a plan free until ends_at (None: open-ended), over every other source.
    Replaces a grant it has. 409 plan_unavailable for an archived plan."""
    change_business(
        current,
        request,
        tenant_id,
        action="plan_granted",
        target=f"plan:{body.plan_id}",
        reason=body.reason,
        details={"ends_at": body.ends_at and body.ends_at.isoformat()},
        sql="SELECT admin_grant_plan(:h, :t, :plan, :ends, :reason)",
        params={"plan": body.plan_id, "ends": body.ends_at, "reason": body.reason},
    )


@admin_write_router.delete("/businesses/{tenant_id}/grant", status_code=204, responses=WRITE_ERRORS)
def end_grant(tenant_id: UUID, body: Reason, current: CurrentAdmin, request: Request) -> None:
    """End the free plan now: the business falls through to its subscription, trial or the default
    plan. 404 when there is none."""
    change_business(
        current,
        request,
        tenant_id,
        action="plan_grant_ended",
        target="grant",
        reason=body.reason,
        details=None,
        sql="SELECT admin_end_grant(:h, :t)",
        params={},
    )
