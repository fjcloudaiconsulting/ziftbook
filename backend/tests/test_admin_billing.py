"""ZIF-78: platform admins edit plans, the default and trial plans, and a business's overrides,
offers and free plan, each through a definer function, audited with its reason."""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app import auth
from app.admin_auth import ADMIN_COOKIE
from app.db import tenant_context
from app.entitlements import FEATURES, entitlements, offers
from app.stripe_gateway import CouponInfo, PriceInfo
from tests.conftest import People, migrate_tenant_context
from tests.stripe_fake import FakeGateway
from tests.test_admin_businesses import admin, an_admin, app  # noqa: F401
from tests.test_admin_second_factor import verified_client

NAME = {"en": "Pro", "nl": "Pro", "pt": "Pro"}
LATER = datetime.now(UTC) + timedelta(days=30)
EUR_MONTHLY = PriceInfo(
    active=True, recurring=True, currency="eur", unit_amount=1900, interval="month"
)


def cookie_hash(client: TestClient) -> bytes:
    return auth.hash_token(client.cookies[ADMIN_COOKIE])


@pytest.fixture
def stripe(fake_stripe: FakeGateway) -> FakeGateway:
    return fake_stripe


@pytest.fixture
def slug(migrate_engine: Engine) -> Iterator[str]:
    """A slug prefix for this test's plans: archived and made private after, as test_entitlements
    leaves its plans (plans are never deleted)."""
    prefix = f"t5-{uuid.uuid4().hex[:10]}"
    yield prefix
    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE plans SET archived_at = now(), public = false WHERE slug LIKE :p"),
            {"p": f"{prefix}%"},
        )


@pytest.fixture(autouse=True)
def billing_settings(migrate_engine: Engine) -> Iterator[None]:
    """platform_billing as it was before the test (the autouse fixture resets only the default)."""
    with migrate_engine.connect() as conn:
        saved = conn.execute(text("SELECT trial_plan_id, trial_days FROM platform_billing")).one()
    yield
    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE platform_billing SET trial_plan_id = :p, trial_days = :d"),
            {"p": saved.trial_plan_id, "d": saved.trial_days},
        )


def price_id() -> str:
    return f"price_{uuid.uuid4().hex}"


def plan_body(slug: str, **overrides: Any) -> dict[str, Any]:
    return {
        "slug": slug,
        "name": NAME,
        "stripe_price_id": None,
        "values": {"places": 3, "deposits": True},
        "public": False,
        "reason": "a plan for the test",
    } | overrides


def make_plan(admin: TestClient, slug: str, **overrides: Any) -> str:  # noqa: F811
    made = admin.post("/api/admin/plans", json=plan_body(slug, **overrides))
    assert made.status_code == 201, made.content
    plan_id: str = made.json()["id"]
    return plan_id


def priced_plan(admin: TestClient, stripe: FakeGateway, slug: str, **overrides: Any) -> str:  # noqa: F811
    price = price_id()
    stripe.prices[price] = EUR_MONTHLY
    return make_plan(admin, slug, stripe_price_id=price, **overrides)


def effective(tenant_id: uuid.UUID) -> Any:
    with tenant_context(tenant_id) as db:
        return entitlements(db)


def subscribe(migrate_engine: Engine, tenant_id: uuid.UUID, plan_id: str) -> None:
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        conn.execute(
            text("""
            INSERT INTO subscriptions
              (tenant_id, stripe_customer_id, stripe_subscription_id, plan_id, status)
            VALUES (:t, :c, :s, :p, 'active')
            """),
            {
                "t": tenant_id,
                "c": f"cus_{uuid.uuid4().hex}",
                "s": f"sub_{uuid.uuid4().hex}",
                "p": plan_id,
            },
        )


def audit_rows(migrate_engine: Engine, actor: uuid.UUID) -> list[tuple[str, str, str | None]]:
    with migrate_engine.connect() as conn:
        return [
            (row.action, row.kind, row.reason)
            for row in conn.execute(
                text(
                    "SELECT action, kind, reason FROM platform_audit_events"
                    " WHERE actor_user_id = :a AND kind = 'write' ORDER BY id"
                ),
                {"a": actor},
            )
        ]


def enforce_jobs(migrate_engine: Engine, tenant_id: uuid.UUID) -> int:
    with migrate_engine.connect() as conn:
        count: int = conn.scalar(
            text("SELECT count(*) FROM jobs WHERE kind = 'billing.enforce' AND tenant_id = :t"),
            {"t": tenant_id},
        )
    return count


def override_url(tenant_id: uuid.UUID, key: str) -> str:
    return f"/api/admin/businesses/{tenant_id}/overrides/{key}"


def offer_url(tenant_id: uuid.UUID, plan_id: str) -> str:
    return f"/api/admin/businesses/{tenant_id}/offers/{plan_id}"


def grant_url(tenant_id: uuid.UUID) -> str:
    return f"/api/admin/businesses/{tenant_id}/grant"


# ZIF-78 fence. Kills: a definer that trusts its caller (a random hash, a factor 16 minutes old),
# and a write route outside admin_write_router.
def test_only_a_fresh_second_factor_writes(
    app: FastAPI,  # noqa: F811
    people: People,
    app_engine: Engine,
    migrate_engine: Engine,
    an_admin: uuid.UUID,  # noqa: F811
    slug: str,
) -> None:
    client = verified_client(app, migrate_engine, an_admin).client
    h = cookie_hash(client)
    plan = make_plan(client, slug)
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE admin_sessions SET second_factor_at = now() - interval '16 minutes'"
                " WHERE id_hash = :h"
            ),
            {"h": h},
        )

    refused = client.put(
        override_url(people.a, "places"), json={"value": 9, "expires_at": None, "reason": "x"}
    )
    assert (refused.status_code, refused.json()) == (403, {"code": "second_factor_stale"})

    calls = {
        "SELECT outcome FROM admin_save_plan(:h, NULL, :s, CAST(:n AS jsonb), NULL,"
        " '{}'::jsonb, false)": {"s": f"{slug}-x", "n": json.dumps(NAME)},
        "SELECT admin_archive_plan(:h, :p)": {"p": plan},
        "SELECT admin_set_billing(:h, :p, NULL, 0)": {"p": plan},
        "SELECT admin_set_override(:h, :t, 'places', '9'::jsonb, NULL, 'x')": {},
        "SELECT admin_remove_override(:h, :t, 'places')": {},
        "SELECT admin_offer_plan(:h, :t, :p, NULL, 'x')": {"p": plan},
        "SELECT admin_withdraw_offer(:h, :t, :p)": {"p": plan},
        "SELECT admin_grant_plan(:h, :t, :p, NULL, 'x')": {"p": plan},
        "SELECT admin_end_grant(:h, :t)": {},
    }
    for hash_ in (h, auth.hash_token("nobody")):
        for sql, params in calls.items():
            with app_engine.begin() as conn:
                outcome = conn.scalar(text(sql), {"h": hash_, "t": people.a, **params})
            assert outcome == "stale", sql
    assert effective(people.a).values["places"] == 50
    assert effective(people.a).source == "default"


# ZIF-78 fence. Kills: values stored unchecked (a float, a string, an unknown key) that would
# fail every entitlements read, and a definer that relies on the route alone.
def test_save_plan_validates_values_and_price(
    admin: TestClient,  # noqa: F811
    stripe: FakeGateway,
    people: People,
    app_engine: Engine,
    an_admin: uuid.UUID,  # noqa: F811
    slug: str,
) -> None:
    for values, key in (
        ({"places": 5.0}, "places"),
        ({"places": True}, "places"),
        ({"deposits": 1}, "deposits"),
        ({"platform_fee_direct": 10001}, "platform_fee_direct"),
        ({"bogus": True}, "bogus"),
    ):
        refused = admin.post("/api/admin/plans", json=plan_body(slug, values=values))
        assert (refused.status_code, refused.json()) == (
            422,
            {"code": "invalid_values", "key": key},
        ), values
    assert admin.post("/api/admin/plans", json=plan_body("Not A Slug")).status_code == 422
    assert admin.post("/api/admin/plans", json=plan_body(slug, name={"en": "x"})).status_code == 422
    assert stripe.count("retrieve_price") == 0

    # the definer refuses what the route would have: a stored 5.0 decodes as a float
    h = cookie_hash(admin)
    for bad in ('{"places": 5.0}', '{"places": "5"}', '{"places": null}', '{"places": -1}', "[]"):
        with pytest.raises(DBAPIError) as refused_sql, app_engine.begin() as conn:
            conn.execute(
                text(
                    "SELECT * FROM admin_save_plan(:h, NULL, :s, CAST(:n AS jsonb), NULL,"
                    " CAST(:v AS jsonb), false)"
                ),
                {"h": h, "s": f"{slug}-sql", "n": json.dumps(NAME), "v": bad},
            )
        assert getattr(refused_sql.value.orig, "sqlstate", None) == "22023", bad
    for bad in ("5.0", '"5"', "null", "-1", "1e-1", "[1]"):
        with pytest.raises(DBAPIError) as refused_sql, app_engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
            conn.execute(
                text("SELECT admin_set_override(:h, :t, 'places', CAST(:v AS jsonb), NULL, 'x')"),
                {"h": h, "t": people.a, "v": bad},
            )
        assert getattr(refused_sql.value.orig, "sqlstate", None) == "22023", bad
    assert effective(people.a).values["places"] == 50

    plan = make_plan(admin, slug)
    listed = admin.get("/api/admin/plans")
    assert listed.status_code == 200
    mine = next(p for p in listed.json()["plans"] if p["id"] == plan)
    assert mine | {"created_at": None, "updated_at": None} == {
        "id": plan,
        "slug": slug,
        "name": NAME,
        "stripe_price_id": None,
        "values": {"places": 3, "deposits": True},
        "public": False,
        "archived_at": None,
        "created_at": None,
        "updated_at": None,
    }
    assert listed.json()["features"] == [
        {"key": key, "kind": f.kind, "default": f.default, "description": f.description}
        for key, f in FEATURES.items()
    ]

    changed = admin.put(
        f"/api/admin/plans/{plan}", json=plan_body(slug, values={"places": 4}, public=True)
    )
    assert changed.status_code == 200, changed.content
    assert (changed.json()["values"], changed.json()["public"]) == ({"places": 4}, True)
    # the slug is another plan's: 409, nothing changed
    other = make_plan(admin, f"{slug}-b")
    taken = admin.put(f"/api/admin/plans/{other}", json=plan_body(slug))
    assert (taken.status_code, taken.json()) == (409, {"code": "plan_taken"})
    unknown = admin.put(f"/api/admin/plans/{uuid.uuid4()}", json=plan_body(f"{slug}-c"))
    assert unknown.status_code == 404


# ZIF-78 fence. Kills: a Price saved without asking Stripe, or one Checkout can't sell monthly in
# EUR (inactive, one-off, another currency, deleted).
def test_price_must_be_active_recurring_eur(
    admin: TestClient,  # noqa: F811
    stripe: FakeGateway,
    slug: str,
) -> None:
    bad = {
        price_id(): PriceInfo(False, True, "eur", 1900, "month"),
        price_id(): PriceInfo(True, False, "eur", 1900, None),
        price_id(): PriceInfo(True, True, "usd", 1900, "month"),
    }
    stripe.prices.update(bad)
    for price in (*bad, price_id()):  # the last one Stripe doesn't know
        refused = admin.post("/api/admin/plans", json=plan_body(slug, stripe_price_id=price))
        assert (refused.status_code, refused.json()) == (422, {"code": "invalid_price"}), price
    assert stripe.count("retrieve_price") == 4

    stripe.fail_next("retrieve_price")
    down = admin.post("/api/admin/plans", json=plan_body(slug, stripe_price_id=price_id()))
    assert (down.status_code, down.json()) == (503, {"code": "payments_unavailable"})

    price = price_id()
    stripe.prices[price] = EUR_MONTHLY
    plan = make_plan(admin, slug, stripe_price_id=price)
    assert stripe.calls[-1][0] == "retrieve_price"
    assert plan in {p["id"] for p in admin.get("/api/admin/plans").json()["plans"]}

    # its Price deactivated since, or Stripe down: a values edit keeping it is not held up
    stripe.prices[price] = PriceInfo(False, True, "eur", 1900, "month")
    stripe.fail_next("retrieve_price")
    asked = stripe.count("retrieve_price")
    edited = admin.put(
        f"/api/admin/plans/{plan}",
        json=plan_body(slug, stripe_price_id=price, values={"places": 8}),
    )
    assert edited.status_code == 200, edited.content
    assert stripe.count("retrieve_price") == asked


# ZIF-78 fence. Kills: a plan's set Price swapped or cleared (its subscribers' Price, or one an open
# Checkout completes on, would then match no plan), and a check outside the definer.
def test_a_priced_plan_keeps_its_price(
    admin: TestClient,  # noqa: F811
    stripe: FakeGateway,
    people: People,
    app_engine: Engine,
    migrate_engine: Engine,
    slug: str,
) -> None:
    price = price_id()
    stripe.prices[price] = EUR_MONTHLY
    plan = make_plan(admin, slug, stripe_price_id=price)
    unused = make_plan(admin, f"{slug}-b")
    subscribe(migrate_engine, people.b, plan)
    newer = price_id()
    stripe.prices[newer] = EUR_MONTHLY

    for new_price in (newer, None):
        refused = admin.put(
            f"/api/admin/plans/{plan}", json=plan_body(slug, stripe_price_id=new_price)
        )
        assert (refused.status_code, refused.json()) == (409, {"code": "plan_in_use"}), new_price
    # the definer refuses it whoever calls, and leaves the caller's business set
    with app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        outcome = conn.scalar(
            text(
                "SELECT outcome FROM admin_save_plan(:h, :p, :s, CAST(:n AS jsonb), :price,"
                " '{}'::jsonb, false)"
            ),
            {"h": cookie_hash(admin), "p": plan, "s": slug, "n": json.dumps(NAME), "price": newer},
        )
        assert outcome == "in_use"
        assert conn.scalar(text("SELECT current_setting('app.tenant_id')")) == str(people.a)
    plans = {p["id"]: p for p in admin.get("/api/admin/plans").json()["plans"]}
    assert plans[plan]["stripe_price_id"] == price

    # the same Price with new values is fine: features follow the plan
    same = admin.put(
        f"/api/admin/plans/{plan}",
        json=plan_body(slug, stripe_price_id=price, values={"places": 9}),
    )
    assert same.status_code == 200, same.content
    assert effective(people.b).values["places"] == 9
    # a plan with no Price takes one, and then keeps it too, subscribers or not: a Checkout opened
    # on it may still complete and must land on this plan
    priced = admin.put(
        f"/api/admin/plans/{unused}", json=plan_body(f"{slug}-b", stripe_price_id=newer)
    )
    assert (priced.status_code, priced.json()["stripe_price_id"]) == (200, newer)
    third = price_id()
    stripe.prices[third] = EUR_MONTHLY
    for new_price in (third, None):
        moved = admin.put(
            f"/api/admin/plans/{unused}", json=plan_body(f"{slug}-b", stripe_price_id=new_price)
        )
        assert (moved.status_code, moved.json()) == (409, {"code": "plan_in_use"}), new_price


# ZIF-78 Review Focus 5. Kills: archiving that moves a business off its plan, or a plan still sold
# (offered) once archived.
def test_archive_keeps_subscribers_on_it(
    admin: TestClient,  # noqa: F811
    stripe: FakeGateway,
    people: People,
    migrate_engine: Engine,
    slug: str,
) -> None:
    plan = priced_plan(admin, stripe, slug, public=True)
    subscribe(migrate_engine, people.a, plan)
    with tenant_context(people.b) as db:
        assert plan in {str(o.id) for o in offers(db)}

    archived = admin.post(f"/api/admin/plans/{plan}/archive", json={"reason": "retired"})

    assert archived.status_code == 200, archived.content
    assert archived.json()["archived_at"] is not None
    found = effective(people.a)
    assert (str(found.plan_id), found.source, found.values["places"]) == (
        plan,
        "subscription",
        3,
    )
    with tenant_context(people.b) as db:
        assert plan not in {str(o.id) for o in offers(db)}
    # archiving again is no change and no error
    assert admin.post(f"/api/admin/plans/{plan}/archive", json={"reason": "x"}).status_code == 200


# ZIF-78 fence. Kills: an archived plan made the default or the trial (every new business would
# land on a retired plan), offered or given; and the default plan archived under everyone.
def test_archived_plan_cannot_be_default_or_offered(
    admin: TestClient,  # noqa: F811
    stripe: FakeGateway,
    people: People,
    migrate_engine: Engine,
    slug: str,
) -> None:
    current = admin.get("/api/admin/billing").json()
    plan = priced_plan(admin, stripe, slug)
    assert admin.post(f"/api/admin/plans/{plan}/archive", json={"reason": "x"}).status_code == 200

    for body in (
        {"default_plan_id": plan, "trial_plan_id": None, "trial_days": 0},
        {"default_plan_id": current["default_plan_id"], "trial_plan_id": plan, "trial_days": 7},
    ):
        refused = admin.put("/api/admin/billing", json=body | {"reason": "x"})
        assert (refused.status_code, refused.json()) == (409, {"code": "plan_unavailable"})
    offered = admin.post(offer_url(people.a, plan), json={"coupon_id": None, "reason": "x"})
    assert (offered.status_code, offered.json()) == (409, {"code": "plan_unavailable"})
    given = admin.put(grant_url(people.a), json={"plan_id": plan, "ends_at": None, "reason": "x"})
    assert (given.status_code, given.json()) == (409, {"code": "plan_unavailable"})
    # a plan with no Price can't be offered: nothing to check out
    free = make_plan(admin, f"{slug}-free")
    unpriced = admin.post(offer_url(people.a, free), json={"coupon_id": None, "reason": "x"})
    assert (unpriced.status_code, unpriced.json()) == (409, {"code": "plan_unavailable"})

    in_use = admin.post(
        f"/api/admin/plans/{current['default_plan_id']}/archive", json={"reason": "x"}
    )
    assert (in_use.status_code, in_use.json()) == (409, {"code": "plan_is_default"})
    assert admin.get("/api/admin/billing").json() == current

    # positive control: an unarchived plan becomes the trial plan
    trial = make_plan(admin, f"{slug}-trial")
    body = {"default_plan_id": current["default_plan_id"], "trial_plan_id": trial, "trial_days": 14}
    assert admin.put("/api/admin/billing", json=body | {"reason": "x"}).status_code == 200
    assert admin.get("/api/admin/billing").json() == body
    in_use = admin.post(f"/api/admin/plans/{trial}/archive", json={"reason": "x"})
    assert (in_use.status_code, in_use.json()) == (409, {"code": "plan_is_default"})


# ZIF-78 fence. Kills: an override written to the wrong business, or not read by entitlements;
# a value the registry refuses; a removal that is not audited.
def test_override_roundtrip_and_audit(
    admin: TestClient,  # noqa: F811
    people: People,
    migrate_engine: Engine,
    an_admin: uuid.UUID,  # noqa: F811
) -> None:
    for key, value in (("places", 2.5), ("places", "3"), ("deposits", 1), ("bogus", True)):
        refused = admin.put(
            override_url(people.a, key), json={"value": value, "expires_at": None, "reason": "x"}
        )
        assert (refused.status_code, refused.json()) == (
            422,
            {"code": "invalid_values", "key": key},
        )
    past = admin.put(
        override_url(people.a, "places"),
        json={"value": 3, "expires_at": "2020-01-01T00:00:00Z", "reason": "x"},
    )
    assert past.status_code == 422

    done = admin.put(
        override_url(people.a, "places"),
        json={"value": 3, "expires_at": LATER.isoformat(), "reason": "pilot"},
    )

    assert done.status_code == 204, done.content
    assert effective(people.a).values["places"] == 3
    assert effective(people.b).values["places"] == 50
    shown = admin.get(f"/api/admin/businesses/{people.a}/billing").json()
    assert shown["effective"]["values"]["places"] == 3
    assert [(o["key"], o["value"], o["reason"]) for o in shown["overrides"]] == [
        ("places", 3, "pilot")
    ]
    # changing it replaces the one row
    assert (
        admin.put(
            override_url(people.a, "places"),
            json={"value": 4, "expires_at": None, "reason": "more"},
        ).status_code
        == 204
    )
    assert effective(people.a).values["places"] == 4

    removed = admin.request("DELETE", override_url(people.a, "places"), json={"reason": "done"})
    assert removed.status_code == 204, removed.content
    assert effective(people.a).values["places"] == 50
    again = admin.request("DELETE", override_url(people.a, "places"), json={"reason": "done"})
    assert (again.status_code, again.json()) == (404, {"code": "not_found"})
    unknown = admin.put(
        override_url(uuid.uuid4(), "places"), json={"value": 3, "expires_at": None, "reason": "x"}
    )
    assert unknown.status_code == 404
    assert audit_rows(migrate_engine, an_admin) == [
        ("override_set", "write", "pilot"),
        ("override_set", "write", "more"),
        ("override_removed", "write", "done"),
    ]


# ZIF-78 fence. Kills: a coupon saved without asking Stripe (a deleted or used-up one, or an amount
# off in another currency, that Checkout would refuse), and an offer that does not reach the
# business's plan list.
def test_offer_with_coupon_validates_coupon(
    admin: TestClient,  # noqa: F811
    stripe: FakeGateway,
    people: People,
    slug: str,
) -> None:
    plan = priced_plan(admin, stripe, slug)
    stripe.coupons["spent"] = CouponInfo(False, 50.0, None, "once", None, None)
    stripe.coupons["half"] = CouponInfo(True, 50.0, None, "repeating", 3, None)
    stripe.coupons["five-dollars"] = CouponInfo(True, None, 500, "forever", None, "usd")
    stripe.coupons["five-euros"] = CouponInfo(True, None, 500, "forever", None, "eur")
    for coupon in ("spent", "missing", "five-dollars"):
        refused = admin.post(offer_url(people.a, plan), json={"coupon_id": coupon, "reason": "x"})
        assert (refused.status_code, refused.json()) == (422, {"code": "invalid_coupon"})
    with tenant_context(people.a) as db:
        assert plan not in {str(o.id) for o in offers(db)}

    done = admin.post(offer_url(people.a, plan), json={"coupon_id": "half", "reason": "deal"})

    assert done.status_code == 204, done.content
    with tenant_context(people.a) as db:
        assert {str(o.id): o.coupon_id for o in offers(db)}[plan] == "half"
    with tenant_context(people.b) as db:
        assert plan not in {str(o.id) for o in offers(db)}
    shown = admin.get(f"/api/admin/businesses/{people.a}/billing").json()
    assert [(o["plan_id"], o["coupon_id"]) for o in shown["offers"]] == [(plan, "half")]

    assert (
        admin.request("DELETE", offer_url(people.a, plan), json={"reason": "over"}).status_code
        == 204
    )
    with tenant_context(people.a) as db:
        assert plan not in {str(o.id) for o in offers(db)}
    again = admin.request("DELETE", offer_url(people.a, plan), json={"reason": "over"})
    assert again.status_code == 404
    # positive control: an amount off in EUR
    euros = admin.post(offer_url(people.b, plan), json={"coupon_id": "five-euros", "reason": "x"})
    assert euros.status_code == 204, euros.content


# ZIF-78 fence. Kills: a grant that doesn't beat the default plan, one given to the wrong
# business, and an end that leaves it in force.
def test_grant_and_end_grant_change_entitlements(
    admin: TestClient,  # noqa: F811
    people: People,
    slug: str,
) -> None:
    plan = make_plan(admin, slug, values={"places": 7})

    given = admin.put(
        grant_url(people.a), json={"plan_id": plan, "ends_at": LATER.isoformat(), "reason": "x"}
    )

    assert given.status_code == 204, given.content
    found = effective(people.a)
    assert (str(found.plan_id), found.source, found.values["places"]) == (plan, "grant", 7)
    assert found.grant_ends_at is not None
    assert effective(people.b).source == "default"
    shown = admin.get(f"/api/admin/businesses/{people.a}/billing").json()
    assert (shown["grant"]["plan_id"], shown["effective"]["source"]) == (plan, "grant")
    assert shown["subscription"] is None

    ended = admin.request("DELETE", grant_url(people.a), json={"reason": "over"})
    assert ended.status_code == 204, ended.content
    assert effective(people.a).source == "default"
    assert admin.get(f"/api/admin/businesses/{people.a}/billing").json()["grant"] is None
    again = admin.request("DELETE", grant_url(people.a), json={"reason": "over"})
    assert again.status_code == 404


# ZIF-78 fence. Kills: a write that skips the audit, audits without the reason, or audits a
# refusal it rolled back.
def test_every_write_is_audited_with_reason(
    admin: TestClient,  # noqa: F811
    stripe: FakeGateway,
    people: People,
    migrate_engine: Engine,
    an_admin: uuid.UUID,  # noqa: F811
    slug: str,
) -> None:
    current = admin.get("/api/admin/billing").json()
    price = price_id()
    stripe.prices[price] = EUR_MONTHLY
    plan = make_plan(admin, slug, stripe_price_id=price)
    a = people.a
    writes: list[tuple[str, str, dict[str, Any]]] = [
        ("PUT", f"/api/admin/plans/{plan}", plan_body(slug, stripe_price_id=price)),
        ("PUT", "/api/admin/billing", current),
        ("PUT", override_url(a, "places"), {"value": 2, "expires_at": None}),
        ("DELETE", override_url(a, "places"), {}),
        ("POST", offer_url(a, plan), {"coupon_id": None}),
        ("DELETE", offer_url(a, plan), {}),
        ("PUT", grant_url(a), {"plan_id": plan, "ends_at": None}),
        ("DELETE", grant_url(a), {}),
        ("POST", f"/api/admin/plans/{plan}/archive", {}),
    ]
    for method, url, body in writes:
        for reason in (None, "  "):
            sent = {k: v for k, v in body.items() if k != "reason"}
            if reason is not None:
                sent["reason"] = reason
            refused = admin.request(method, url, json=sent)
            assert refused.status_code == 422, (method, url, reason)
    # nothing but the plan's creation so far
    assert audit_rows(migrate_engine, an_admin) == [("plan_saved", "write", "a plan for the test")]

    for i, (method, url, body) in enumerate(writes):
        done = admin.request(method, url, json=body | {"reason": f"why {i}"})
        assert done.status_code in (200, 204), (method, url, done.content)
    assert audit_rows(migrate_engine, an_admin)[1:] == [
        (action, "write", f"why {i}")
        for i, action in enumerate(
            (
                "plan_saved",
                "billing_settings_changed",
                "override_set",
                "override_removed",
                "plan_offered",
                "plan_offer_withdrawn",
                "plan_granted",
                "plan_grant_ended",
                "plan_archived",
            )
        )
    ]
    with migrate_engine.connect() as conn:
        tenants = conn.execute(
            text(
                "SELECT action, tenant_id, target FROM platform_audit_events"
                " WHERE actor_user_id = :a AND kind = 'write' ORDER BY id"
            ),
            {"a": an_admin},
        ).all()
    assert {(row.action, row.tenant_id) for row in tenants} >= {
        ("override_set", a),
        ("plan_granted", a),
        ("plan_saved", None),
    }
    # a refusal leaves no audit row
    before = len(audit_rows(migrate_engine, an_admin))
    admin.request("DELETE", grant_url(a), json={"reason": "nothing to end"})
    assert len(audit_rows(migrate_engine, an_admin)) == before


# ZIF-78 fence. Kills: a change to what a business may use that never re-checks its places, and a
# plan edit that re-checks every business instead of those on it.
def test_writes_enqueue_enforcement(
    admin: TestClient,  # noqa: F811
    stripe: FakeGateway,
    people: People,
    migrate_engine: Engine,
    slug: str,
) -> None:
    plan = make_plan(admin, slug, values={"places": 1})
    assert enforce_jobs(migrate_engine, people.a) == 0
    priced = priced_plan(admin, stripe, f"{slug}-p")
    admin.post(offer_url(people.b, priced), json={"coupon_id": None, "reason": "x"})
    assert enforce_jobs(migrate_engine, people.b) == 1
    admin.request("DELETE", offer_url(people.b, priced), json={"reason": "x"})
    assert enforce_jobs(migrate_engine, people.b) == 2

    admin.put(
        override_url(people.a, "places"), json={"value": 2, "expires_at": None, "reason": "x"}
    )
    assert enforce_jobs(migrate_engine, people.a) == 1
    admin.request("DELETE", override_url(people.a, "places"), json={"reason": "x"})
    assert enforce_jobs(migrate_engine, people.a) == 2
    admin.put(grant_url(people.a), json={"plan_id": plan, "ends_at": None, "reason": "x"})
    assert enforce_jobs(migrate_engine, people.a) == 3

    # a is on the plan, b is not: a save and an archive check a only
    admin.put(f"/api/admin/plans/{plan}", json=plan_body(slug, values={"places": 2}))
    assert (enforce_jobs(migrate_engine, people.a), enforce_jobs(migrate_engine, people.b)) == (
        4,
        2,
    )
    assert admin.post(f"/api/admin/plans/{plan}/archive", json={"reason": "x"}).status_code == 200
    assert (enforce_jobs(migrate_engine, people.a), enforce_jobs(migrate_engine, people.b)) == (
        5,
        2,
    )
    admin.request("DELETE", grant_url(people.a), json={"reason": "x"})
    assert enforce_jobs(migrate_engine, people.a) == 6
    # a and b are both on the default plan now: changing the billing settings checks both
    current = admin.get("/api/admin/billing").json()
    assert admin.put("/api/admin/billing", json=current | {"reason": "x"}).status_code == 200
    assert (enforce_jobs(migrate_engine, people.a), enforce_jobs(migrate_engine, people.b)) == (
        7,
        3,
    )


# ZIF-78 fence. Kills: a per-business definer that writes whatever business its caller is in (or
# none), instead of refusing a p_tenant other than app.tenant_id.
def test_a_definer_outside_its_business_writes_nothing(
    admin: TestClient,  # noqa: F811
    people: People,
    app_engine: Engine,
) -> None:
    h = cookie_hash(admin)
    sql = "SELECT admin_set_override(:h, :t, 'places', '3'::jsonb, NULL, 'x')"
    for caller in (str(people.b), None):
        with app_engine.begin() as conn:
            if caller is not None:
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": caller})
            assert conn.scalar(text(sql), {"h": h, "t": people.a}) == "mismatch"
    assert effective(people.a).values["places"] == 50
    assert effective(people.b).values["places"] == 50
    # positive control: inside a
    with app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        assert conn.scalar(text(sql), {"h": h, "t": people.a}) == "ok"
    assert effective(people.a).values["places"] == 3
