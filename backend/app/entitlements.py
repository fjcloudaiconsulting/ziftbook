"""What a business's plan lets it do (ZIF-78): one registry of features in code, values on plans
(migration 0103), per-business overrides, and `entitlements(db)`, the only check a gate makes.
CONTRIBUTING.md, "Plans and entitlements".

The effective plan, computed on every read with no job flipping plans: an admin's grant not yet
ended, else a subscription Stripe still counts (active, trialing, past_due), else a trial not yet
ended once an operator has set the trial plan, else the default plan (Free). Its values lie over
the registry's defaults, and the business's live overrides over both.
"""

import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app import auth, stripe_gateway
from app.db import tenant_context
from app.errors import ApiError, Error

Kind = Literal["flag", "count", "percent"]
Source = Literal["grant", "subscription", "trial", "default"]

# How long a business over its places keeps every member signing in (the spec's 30 days).
GRACE = timedelta(days=30)


@dataclass(frozen=True)
class Feature:
    kind: Kind
    default: bool | int  # used when neither the plan nor an override sets the key
    description: str  # shown in the admin plan editor


# Percent values are basis points, 0 to 10000.
FEATURES: dict[str, Feature] = {
    "places": Feature(
        "count", 0, "People in the business: members plus pending invites, owners included."
    ),
    "online_payments": Feature("flag", False, "Take card payments at checkout."),
    "deposits": Feature("flag", False, "Ask for a deposit or full prepayment on a service."),
    "email_reminders": Feature("flag", False, "Email clients a reminder 24 hours before."),
    "worker_permissions": Feature(
        "flag", False, "Let workers edit their own hours and answer booking requests."
    ),
    "platform_fee_direct": Feature(
        "percent", 0, "Platform fee on bookings from the booking page or the merchant (bp)."
    ),
    "platform_fee_marketplace": Feature(
        "percent", 0, "Platform fee on bookings from the marketplace (bp)."
    ),
    "calendar_sync": Feature("flag", False, "Sync with external calendars."),
    "messages_per_month": Feature("count", 0, "Messages sent to clients each month."),
    "client_messaging": Feature("flag", False, "Message clients from the console."),
    "no_show_fees": Feature("flag", False, "Charge a fee when a client does not show up."),
    "marketplace_listing": Feature("flag", False, "List the business on the marketplace."),
    "custom_domain": Feature("flag", False, "Serve the booking page on the business's own domain."),
    "ai_assistant": Feature("flag", False, "Use the AI assistant."),
    "hide_branding": Feature("flag", False, 'Hide the "Powered by" link on the booking page.'),
}


def validate(values: dict[str, object]) -> dict[str, bool | int]:
    """values checked against the registry, or ValueError naming the first key that fails. type()
    is, not isinstance: True is an int to Python, and must not pass as a count."""
    checked: dict[str, bool | int] = {}
    for key, value in values.items():
        feature = FEATURES.get(key)
        if feature is None:
            raise ValueError(f"{key}: not a feature")
        if feature.kind == "flag" and type(value) is bool:
            checked[key] = value
        elif feature.kind == "count" and type(value) is int and value >= 0:
            checked[key] = value
        elif feature.kind == "percent" and type(value) is int and 0 <= value <= 10000:
            checked[key] = value
        else:
            raise ValueError(f"{key}: not a valid {feature.kind}")
    return checked


@dataclass(frozen=True)
class Entitlements:
    plan_id: UUID
    plan_slug: str
    plan_name: dict[str, str]
    source: Source
    trial_ends_at: datetime | None
    grant_ends_at: datetime | None
    period_end: datetime | None
    cancel_at_period_end: bool
    over_limit_since: datetime | None
    values: dict[str, bool | int]  # every registry key

    def flag(self, key: str) -> bool:
        assert FEATURES[key].kind == "flag", key
        return self.values[key] is True

    def count(self, key: str) -> int:
        assert FEATURES[key].kind != "flag", key
        return int(self.values[key])


# One read, in the caller's tenant transaction: row-level security keeps every tenant table to
# this business. LEFT JOINs, so a business missing its tenant_billing row (an operator's insert, a
# restore) still answers its default plan. A subscription whose Price no plan carries (plan_id
# NULL) gives no plan here.
READ = text("""
SELECT p.id, p.slug, p.name, p.values, w.source, tb.trial_ends_at, tb.over_limit_since,
       CASE WHEN w.source = 'grant' THEN g.ends_at END AS grant_ends_at,
       s.current_period_end, coalesce(s.cancel_at_period_end, false) AS cancel_at_period_end,
       (SELECT coalesce(jsonb_object_agg(o.key, o.value), '{}') FROM business_overrides o
        WHERE o.expires_at IS NULL OR o.expires_at > now()) AS overrides
FROM platform_billing b
LEFT JOIN tenant_billing tb ON true
LEFT JOIN plan_grants g ON g.ends_at IS NULL OR g.ends_at > now()
LEFT JOIN subscriptions s
  ON s.status IN ('active', 'trialing', 'past_due') AND s.plan_id IS NOT NULL
CROSS JOIN LATERAL (SELECT CASE
  WHEN g.plan_id IS NOT NULL THEN 'grant'
  WHEN s.plan_id IS NOT NULL THEN 'subscription'
  WHEN tb.trial_ends_at > now() AND b.trial_plan_id IS NOT NULL THEN 'trial'
  ELSE 'default' END AS source) w
JOIN plans p ON p.id = CASE w.source
  WHEN 'grant' THEN g.plan_id WHEN 'subscription' THEN s.plan_id
  WHEN 'trial' THEN b.trial_plan_id ELSE b.default_plan_id END
""")


def _stored(values: dict[str, object]) -> dict[str, bool | int]:
    """Saved values, as the writers validated them. A key the registry no longer knows is ignored
    (a removed feature); a known key that no longer validates fails the read, as settings do."""
    return validate({k: v for k, v in values.items() if k in FEATURES})


def entitlements(db: Session) -> Entitlements:
    """The business's effective plan and values. db is a tenant_context; the answer is kept on it,
    so one request reads it once however many gates ask. A writer that changes billing state and
    then reads again in the same transaction calls forget(db) in between."""
    cached: Entitlements | None = db.info.get("entitlements")
    if cached is not None:
        return cached
    row = db.execute(READ).one()
    found = Entitlements(
        plan_id=row.id,
        plan_slug=row.slug,
        plan_name=row.name,
        source=row.source,
        trial_ends_at=row.trial_ends_at,
        grant_ends_at=row.grant_ends_at,
        period_end=row.current_period_end,
        cancel_at_period_end=row.cancel_at_period_end,
        over_limit_since=row.over_limit_since,
        values={key: f.default for key, f in FEATURES.items()}
        | _stored(row.values)
        | _stored(row.overrides),
    )
    db.info["entitlements"] = found
    return found


def forget(db: Session) -> None:
    """Drop the answer kept on db, so the next entitlements(db) reads it again."""
    db.info.pop("entitlements", None)


def require(db: Session, key: str) -> None:
    """402 plan_limit naming key unless the business's plan has the flag on."""
    if not entitlements(db).flag(key):
        raise ApiError(402, "plan_limit", feature=key, limit=None)


def require_room(db: Session, key: str, used: int) -> None:
    """402 plan_limit naming key and its count when used already reaches it."""
    count = entitlements(db).count(key)
    if used >= count:
        raise ApiError(402, "plan_limit", feature=key, limit=count)


# Members who hold a place: everyone not removed, owners included.
LIVE_MEMBERS = text("SELECT count(*) FROM memberships WHERE role <> 'removed'")
# The places in use, as the invite gate counts them: live members plus pending, unexpired invites
# (an accepted invite is deleted).
PLACES_USED = text("""
SELECT (SELECT count(*) FROM memberships WHERE role <> 'removed')
     + (SELECT count(*) FROM invites WHERE expires_at > now())
""")

router = APIRouter(prefix="/api", tags=["billing"])


class PlanOption(BaseModel):
    """A plan this business can subscribe to. The price is Stripe's (ZIF-79), before any coupon;
    all three price fields are None while Stripe can't be asked. The coupon fields are None while
    Stripe can't be asked and with no coupon; its discount and terms also once it no longer
    applies (coupon_valid false)."""

    id: UUID
    slug: str
    name: dict[str, str]
    platform_fee_direct: int  # basis points
    platform_fee_marketplace: int  # basis points
    coupon_id: str | None  # the business's offer carries a Stripe coupon
    unit_amount: int | None  # minor units of currency
    currency: str | None  # lower case, as Stripe has it
    interval: str | None  # month, year
    percent_off: float | None  # the coupon's, as Stripe has it (12.5 is 12.5%)
    amount_off: int | None  # the coupon's, in minor units of the price's currency
    coupon_duration: Literal["once", "repeating", "forever"] | None  # invoices it discounts
    coupon_duration_in_months: int | None  # a repeating coupon's
    coupon_valid: bool | None  # false: deleted, expired or used up; None: unknown


class EntitlementsOut(BaseModel):
    plan_id: UUID
    plan_slug: str
    plan_name: dict[str, str]
    source: Source
    trial_ends_at: datetime | None
    grant_ends_at: datetime | None
    period_end: datetime | None
    cancel_at_period_end: bool
    over_limit_since: datetime | None
    values: dict[str, bool | int]
    members_used: int  # live members plus pending invites, against places
    grace_ends_at: datetime | None  # when members beyond the places stop signing in
    offers: list[PlanOption]  # owners only; empty for a worker
    # Owners only: the Stripe subscription's status, live or not (unpaid and paused give no plan
    # but still have a portal; canceled and incomplete_expired may subscribe again). None: none.
    subscription_status: str | None


# Public, unarchived paid plans and the plans offered to this business (plan_offers is under
# row-level security), one row each, an offer's coupon included.
OFFERS = text("""
SELECT p.id, p.slug, p.name, p.values, p.stripe_price_id, o.stripe_coupon_id
FROM plans p LEFT JOIN plan_offers o ON o.plan_id = p.id
WHERE p.archived_at IS NULL AND p.stripe_price_id IS NOT NULL AND (p.public OR o.id IS NOT NULL)
ORDER BY p.created_at, p.id
""")


def offers(db: Session) -> list[Row[Any]]:
    """The plans on offer, read in the tenant transaction; plan_options() prices them after it."""
    return list(db.execute(OFFERS))


# Stripe's Prices by id, with when to ask again: in process, so a console load rarely asks Stripe.
# A Price's amount never changes; a failure is kept a short while, so an outage neither hammers
# Stripe nor hides prices long.
PRICE_TTL, FAILED_TTL = 1800.0, 60.0
_prices: dict[str, tuple[float, stripe_gateway.PriceInfo | None]] = {}
# One refresh at a time; meanwhile other requests answer from what is kept (nothing when cold).
_refreshing = threading.Lock()


def prices(ids: list[str]) -> dict[str, stripe_gateway.PriceInfo]:
    """The Prices of these ids Stripe has, from the cache or Stripe. Never raises: a Price Stripe
    can't give is missing from the answer. Call it with no transaction open."""
    now = time.monotonic()
    stale = [i for i in ids if _prices.get(i, (0.0, None))[0] <= now]
    if stale and _refreshing.acquire(blocking=False):
        try:
            got = stripe_gateway.gateway.list_prices(stale)
        except stripe_gateway.Failed:  # what was known stays, asked again a minute on
            _prices.update({i: (now + FAILED_TTL, _prices.get(i, (0.0, None))[1]) for i in stale})
        else:
            _prices.update({i: (now + PRICE_TTL, got.get(i)) for i in stale})
        finally:
            _refreshing.release()
    kept = {i: _prices.get(i, (0.0, None))[1] for i in ids}
    return {i: info for i, info in kept.items() if info is not None}


# Stripe's coupons by id, kept as the Prices are: a coupon's discount never changes, and Stripe
# reads coupons one at a time. A deleted one is kept as an invalid coupon, as long as a found one.
DELETED = stripe_gateway.CouponInfo(False, None, None, "once", None, None)
_coupons: dict[str, tuple[float, stripe_gateway.CouponInfo | None]] = {}
_refreshing_coupons = threading.Lock()


def coupons(ids: list[str]) -> dict[str, stripe_gateway.CouponInfo]:
    """The coupons of these ids Stripe knows, from the cache or Stripe; one no longer applying (or
    deleted) has valid false. Never raises: a coupon Stripe can't give right now is missing from
    the answer. Call it with no transaction open."""
    now = time.monotonic()
    stale = [i for i in dict.fromkeys(ids) if _coupons.get(i, (0.0, None))[0] <= now]
    if stale and _refreshing_coupons.acquire(blocking=False):
        try:
            for coupon_id in stale:
                try:
                    got = stripe_gateway.gateway.retrieve_coupon(coupon_id)
                except stripe_gateway.Failed as failure:
                    if failure.kind == "invalid":  # deleted
                        _coupons[coupon_id] = (now + PRICE_TTL, DELETED)
                    else:  # what was known stays, asked again a minute on
                        known = _coupons.get(coupon_id, (0.0, None))[1]
                        _coupons[coupon_id] = (now + FAILED_TTL, known)
                else:
                    _coupons[coupon_id] = (now + PRICE_TTL, got)
        finally:
            _refreshing_coupons.release()
    kept = {i: _coupons.get(i, (0.0, None))[1] for i in ids}
    return {i: info for i, info in kept.items() if info is not None}


def plan_options(rows: list[Row[Any]]) -> list[PlanOption]:
    known = prices([row.stripe_price_id for row in rows])
    known_coupons = coupons([row.stripe_coupon_id for row in rows if row.stripe_coupon_id])
    options = []
    for row in rows:
        values = {key: f.default for key, f in FEATURES.items()} | _stored(row.values)
        price = known.get(row.stripe_price_id)
        found = known_coupons.get(row.stripe_coupon_id) if row.stripe_coupon_id else None
        coupon = found if found and found.valid else None
        # An amount off counts only in the price's currency: Checkout refuses it on another.
        same_currency = (
            coupon is not None
            and price is not None
            and (coupon.currency or "").lower() == price.currency.lower()
        )
        options.append(
            PlanOption(
                id=row.id,
                slug=row.slug,
                name=row.name,
                platform_fee_direct=int(values["platform_fee_direct"]),
                platform_fee_marketplace=int(values["platform_fee_marketplace"]),
                coupon_id=row.stripe_coupon_id,
                unit_amount=price.unit_amount if price else None,
                currency=price.currency if price else None,
                interval=price.interval if price else None,
                percent_off=coupon.percent_off if coupon else None,
                amount_off=coupon.amount_off if coupon and same_currency else None,
                coupon_duration=coupon.duration if coupon else None,
                coupon_duration_in_months=coupon.duration_in_months if coupon else None,
                coupon_valid=found.valid if found else None,
            )
        )
    return options


SUBSCRIPTION_STATUS = text("SELECT status FROM subscriptions")


@router.get(
    "/entitlements", name="read_entitlements", responses={s: {"model": Error} for s in (401, 403)}
)
def read_entitlements(
    row: Annotated[Row[Any], Depends(auth.resolve)], response: Response
) -> EntitlementsOut:
    """The business's plan, where it comes from, every feature's value, and the places in use."""
    response.headers["Cache-Control"] = "no-store"
    # The session read here, not in a CurrentSession: the prices may call Stripe, and no
    # transaction (so no pool connection) is held across a Stripe call.
    with tenant_context(row.tenant_id) as db:
        auth.named(auth.SignedIn(row.user_id, row.tenant_id, row.role, db))
        found = entitlements(db)
        used = db.scalar(PLACES_USED)
        rows = offers(db) if row.role == "owner" else []
        status = db.scalar(SUBSCRIPTION_STATUS) if row.role == "owner" else None
    since = found.over_limit_since
    return EntitlementsOut(
        **asdict(found),
        members_used=used,
        grace_ends_at=since + GRACE if since is not None else None,
        offers=plan_options(rows),
        subscription_status=status,
    )
