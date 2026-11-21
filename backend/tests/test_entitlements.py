"""ZIF-78: one feature registry, one entitlements read, one 402 that names the feature."""

import asyncio
import json
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app import enforcement
from app.auth import CurrentSession
from app.db import tenant_context
from app.entitlements import FEATURES, entitlements, forget, require, require_room, validate
from app.jobs import run_once
from app.main import create_app
from app.worker import KINDS
from tests.conftest import People, give_plan, jobs_as_the_worker, migrate_tenant_context, signed_in

NOW = datetime.now(UTC)
LATER = NOW + timedelta(days=10)
EARLIER = NOW - timedelta(days=1)
NAME = '{"en": "P", "nl": "P", "pt": "P"}'

Plan = Callable[..., uuid.UUID]


@pytest.fixture
def plan(migrate_engine: Engine) -> Iterator[Plan]:
    """Make a plan with these values. Archived and made private afterwards, so no later test's plan
    list shows it (plans are never deleted: grants and subscriptions refer to them)."""
    made: list[uuid.UUID] = []

    def make(
        values: dict[str, Any],
        *,
        public: bool = False,
        price: bool = False,
        archived: bool = False,
    ) -> uuid.UUID:
        slug = f"t-{uuid.uuid4().hex[:12]}"
        with migrate_engine.begin() as conn:
            plan_id: uuid.UUID = conn.scalar(
                text("""
                INSERT INTO plans (slug, name, values, public, stripe_price_id, archived_at)
                VALUES (:s, CAST(:n AS jsonb), CAST(:v AS jsonb), :public, :price,
                        CASE WHEN :archived THEN now() END)
                RETURNING id
                """),
                {
                    "s": slug,
                    "n": NAME,
                    "v": json.dumps(values),
                    "public": public,
                    "price": f"price_{slug.replace('-', '')}" if price else None,
                    "archived": archived,
                },
            )
        made.append(plan_id)
        return plan_id

    yield make
    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE plans SET archived_at = now(), public = false WHERE id = ANY(:ids)"),
            {"ids": made},
        )


@pytest.fixture
def trial_plan(migrate_engine: Engine, plan: Plan) -> Iterator[uuid.UUID]:
    """platform_billing's trial plan set to a plan of its own; put back after."""
    trial = plan({"places": 3, "deposits": True})
    with migrate_engine.begin() as conn:
        saved = conn.scalar(text("SELECT trial_plan_id FROM platform_billing"))
        conn.execute(text("UPDATE platform_billing SET trial_plan_id = :p"), {"p": trial})
    yield trial
    with migrate_engine.begin() as conn:
        conn.execute(text("UPDATE platform_billing SET trial_plan_id = :p"), {"p": saved})


def write(migrate_engine: Engine, tenant_id: uuid.UUID, sql: str, **params: Any) -> None:
    """A billing row as an operator, Stripe's worker or an admin function would leave it."""
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        conn.execute(text(sql), {"t": tenant_id, **params})


def set_trial_end(migrate_engine: Engine, tenant_id: uuid.UUID, ends: datetime | None) -> None:
    write(migrate_engine, tenant_id, "UPDATE tenant_billing SET trial_ends_at = :e", e=ends)


def subscribe(
    migrate_engine: Engine,
    tenant_id: uuid.UUID,
    plan_id: uuid.UUID,
    status: str = "active",
    period_end: datetime | None = LATER,
) -> None:
    write(
        migrate_engine,
        tenant_id,
        """INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id,
             plan_id, status, current_period_end, cancel_at_period_end)
           VALUES (:t, 'cus_1', :sub, :p, :s, :e, true)""",
        sub=f"sub_{uuid.uuid4().hex}",
        p=plan_id,
        s=status,
        e=period_end,
    )


def grant(
    migrate_engine: Engine, tenant_id: uuid.UUID, plan_id: uuid.UUID, ends: datetime | None
) -> None:
    write(
        migrate_engine,
        tenant_id,
        """INSERT INTO plan_grants (tenant_id, plan_id, ends_at, reason, admin_user_id)
           VALUES (:t, :p, :e, 'test', :t)""",
        p=plan_id,
        e=ends,
    )


def read(tenant_id: uuid.UUID) -> Any:
    with tenant_context(tenant_id) as db:
        return entitlements(db)


# ZIF-78 fence. Kills: a key missing from, or added to, the spec's list; a default that is not off
# or zero; a type changed (a count read as a flag lets every invite through).
def test_registry_has_the_spec_keys() -> None:
    kinds = {key: feature.kind for key, feature in FEATURES.items()}
    assert kinds == {
        "places": "count",
        "online_payments": "flag",
        "deposits": "flag",
        "email_reminders": "flag",
        "worker_permissions": "flag",
        "platform_fee_direct": "percent",
        "platform_fee_marketplace": "percent",
        "calendar_sync": "flag",
        "messages_per_month": "count",
        "client_messaging": "flag",
        "no_show_fees": "flag",
        "marketplace_listing": "flag",
        "custom_domain": "flag",
        "ai_assistant": "flag",
        "hide_branding": "flag",
    }
    for feature in FEATURES.values():
        assert feature.default == 0
        assert type(feature.default) is (bool if feature.kind == "flag" else int)
        assert feature.description.strip()


# ZIF-78 fence. Kills: an unknown key stored on a plan or override; True accepted as a count
# (bool is an int in Python); a negative count; a fee over 100%.
@pytest.mark.parametrize(
    ("values", "key"),
    [
        ({"places": True}, "places"),
        ({"places": -1}, "places"),
        ({"places": 1.0}, "places"),
        ({"online_payments": 1}, "online_payments"),
        ({"platform_fee_direct": 10001}, "platform_fee_direct"),
        ({"platform_fee_marketplace": -1}, "platform_fee_marketplace"),
        ({"nope": 1}, "nope"),
    ],
)
def test_validate_refuses_unknown_and_mistyped(values: dict[str, object], key: str) -> None:
    with pytest.raises(ValueError, match=key):
        validate(values)
    assert validate({"places": 5, "deposits": False, "platform_fee_direct": 10000}) == {
        "places": 5,
        "deposits": False,
        "platform_fee_direct": 10000,
    }


# ZIF-78 fence. Kills: a business with nothing else not on the default plan; a trial end alone
# (no trial plan set yet) giving anything but the default; registry defaults not filling the keys
# Free leaves out.
@pytest.mark.parametrize("trial_end", [EARLIER, LATER, None], ids=["ended", "no-plan", "none"])
def test_default_plan_when_nothing_else(
    people: People, migrate_engine: Engine, free_default: None, trial_end: datetime | None
) -> None:
    set_trial_end(migrate_engine, people.a, trial_end)

    with tenant_context(people.a) as db:
        got = entitlements(db)
        assert entitlements(db) is got  # once per transaction
        forget(db)
        assert entitlements(db) is not got

    assert (got.source, got.plan_slug, got.count("places")) == ("default", "free", 1)
    assert got.plan_name == {"en": "Free", "nl": "Gratis", "pt": "Grátis"}
    assert got.flag("email_reminders") is True
    assert got.flag("online_payments") is False
    assert got.count("platform_fee_direct") == 0
    assert got.values.keys() == FEATURES.keys()


# ZIF-78 fence. Kills: a live trial ignored; the trial plan read from anywhere but platform_billing.
def test_trial_beats_default(people: People, migrate_engine: Engine, trial_plan: uuid.UUID) -> None:
    set_trial_end(migrate_engine, people.a, LATER)

    got = read(people.a)

    assert (got.source, got.plan_id, got.count("places")) == ("trial", trial_plan, 3)
    assert got.flag("deposits") is True
    assert got.trial_ends_at == LATER


# ZIF-78 fence. Kills: a trial outranking a paid subscription.
def test_subscription_beats_trial(
    people: People, migrate_engine: Engine, trial_plan: uuid.UUID, plan: Plan
) -> None:
    paid = plan({"places": 7})
    set_trial_end(migrate_engine, people.a, LATER)
    subscribe(migrate_engine, people.a, paid)

    got = read(people.a)

    assert (got.source, got.plan_id, got.count("places")) == ("subscription", paid, 7)
    assert (got.period_end, got.cancel_at_period_end) == (LATER, True)


# ZIF-78 fence. Kills: a business dropped while Stripe retries its card (past_due), or during a
# Stripe-side trial.
@pytest.mark.parametrize("status", ["active", "trialing", "past_due"])
def test_past_due_still_counts(
    people: People, migrate_engine: Engine, plan: Plan, status: str
) -> None:
    paid = plan({"places": 7})
    subscribe(migrate_engine, people.a, paid, status)

    assert read(people.a).source == "subscription"


# ZIF-78 fence. Kills: a canceled, unpaid or incomplete subscription still giving its plan; a
# subscription on a Price no plan carries (plan_id NULL) resolving to nothing.
@pytest.mark.parametrize("status", ["canceled", "unpaid", "incomplete", "incomplete_expired"])
@pytest.mark.parametrize("on_trial", [True, False], ids=["trial", "default"])
def test_canceled_falls_to_trial_or_default(
    people: People,
    migrate_engine: Engine,
    trial_plan: uuid.UUID,
    plan: Plan,
    status: str,
    on_trial: bool,
) -> None:
    set_trial_end(migrate_engine, people.a, LATER if on_trial else EARLIER)
    subscribe(migrate_engine, people.a, plan({"places": 7}), status)

    assert read(people.a).source == ("trial" if on_trial else "default")


def test_a_subscription_with_no_plan_is_not_a_plan(people: People, migrate_engine: Engine) -> None:
    write(
        migrate_engine,
        people.a,
        """INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id,
             status) VALUES (:t, 'cus_1', :sub, 'active')""",
        sub=f"sub_{uuid.uuid4().hex}",
    )

    assert read(people.a).source == "default"


# ZIF-78 fence. Kills: a grant (ziftbook's free plan for a business) losing to its subscription.
def test_grant_beats_subscription(people: People, migrate_engine: Engine, plan: Plan) -> None:
    given = plan({"places": 9})
    subscribe(migrate_engine, people.a, plan({"places": 7}))
    grant(migrate_engine, people.a, given, LATER)

    got = read(people.a)

    assert (got.source, got.plan_id, got.count("places"), got.grant_ends_at) == (
        "grant",
        given,
        9,
        LATER,
    )


# ZIF-78 fence. Kills: an ended grant still giving its plan.
def test_expired_grant_is_ignored(people: People, migrate_engine: Engine, plan: Plan) -> None:
    paid = plan({"places": 7})
    subscribe(migrate_engine, people.a, paid)
    grant(migrate_engine, people.a, plan({"places": 9}), EARLIER)

    got = read(people.a)

    assert (got.source, got.plan_id, got.grant_ends_at) == ("subscription", paid, None)


# ZIF-78 fence. Kills: an override not applied over the plan; an expired override still applied;
# another business's override applied here; an override of one key dropping the plan's others.
def test_override_beats_plan_and_expired_override_is_ignored(
    people: People, migrate_engine: Engine, plan: Plan
) -> None:
    give_plan(people.a, {"places": 2, "deposits": True, "email_reminders": True})
    for tenant_id, key, value, expires in [
        (people.a, "places", "12", None),
        (people.a, "online_payments", "true", LATER),
        (people.a, "deposits", "false", EARLIER),
        (people.b, "email_reminders", "false", None),
    ]:
        write(
            migrate_engine,
            tenant_id,
            """INSERT INTO business_overrides (tenant_id, key, value, expires_at, reason,
                 admin_user_id) VALUES (:t, :k, CAST(:v AS jsonb), :e, 'test', :t)""",
            k=key,
            v=value,
            e=expires,
        )

    got = read(people.a)

    assert got.count("places") == 12
    assert got.flag("online_payments") is True
    assert got.flag("deposits") is True  # its override expired
    assert got.flag("email_reminders") is True  # b's override is b's


# ZIF-78 fence (Review Focus 1). Kills: a business with no tenant_billing row (an operator's insert
# before the trigger, a restore) answering nothing or failing instead of the default plan.
def test_missing_tenant_billing_row_answers_default(
    people: People, migrate_engine: Engine, free_default: None
) -> None:
    write(migrate_engine, people.a, "DELETE FROM tenant_billing")

    got = read(people.a)

    assert (got.source, got.plan_slug, got.trial_ends_at, got.over_limit_since) == (
        "default",
        "free",
        None,
        None,
    )


# ZIF-78 fence. Kills: archiving a plan dropping its subscribers to Free (archive means: offered to
# nobody new).
def test_archived_plan_still_gives_its_values(
    people: People, migrate_engine: Engine, plan: Plan
) -> None:
    old = plan({"places": 4, "deposits": True}, archived=True)
    subscribe(migrate_engine, people.a, old)

    got = read(people.a)

    assert (got.plan_id, got.count("places"), got.flag("deposits")) == (old, 4, True)


# ZIF-78 fence. Kills: the answer cached by a browser or proxy; a worker shown the plans (and
# coupons) on offer; a private plan offered to every business; an archived or unpaid plan offered;
# another business's offer shown; places in use not counting a pending invite (or counting an
# expired one); the grace deadline missing.
def test_get_entitlements_shape_and_no_store(
    people: People, migrate_engine: Engine, plan: Plan
) -> None:
    public = plan(
        {"platform_fee_direct": 150, "platform_fee_marketplace": 900}, public=True, price=True
    )
    offered = plan({"places": 5}, price=True)
    for_b = plan({"places": 6}, price=True)
    plan({}, public=True, price=True, archived=True)
    plan({}, public=True)  # nobody pays for it: not a plan to subscribe to
    give_plan(people.a, {"places": 1, "email_reminders": True})
    for tenant_id, plan_id, coupon in [
        (people.a, offered, "SPRING"),
        (people.a, public, None),
        (people.b, for_b, None),
    ]:
        write(
            migrate_engine,
            tenant_id,
            """INSERT INTO plan_offers (tenant_id, plan_id, stripe_coupon_id, reason, admin_user_id)
               VALUES (:t, :p, :c, 'test', :t)""",
            p=plan_id,
            c=coupon,
        )
    write(migrate_engine, people.a, "UPDATE tenant_billing SET over_limit_since = :s", s=EARLIER)
    with tenant_context(people.a) as db:  # one pending invite holds a place; an expired one not
        db.execute(
            text("""INSERT INTO invites (tenant_id, email, expires_at) VALUES
                    (:t, 'pending@example.com', now() + interval '1 day'),
                    (:t, 'expired@example.com', now() - interval '1 second')"""),
            {"t": people.a},
        )
    app = create_app()

    owner = signed_in(app, people.a, people.both).get("/api/entitlements")
    worker = signed_in(app, people.a, people.only_a).get("/api/entitlements")

    assert owner.status_code == 200
    assert owner.headers["cache-control"] == "no-store"
    body = owner.json()
    assert set(body) == {
        "plan_id",
        "plan_slug",
        "plan_name",
        "source",
        "trial_ends_at",
        "grant_ends_at",
        "period_end",
        "cancel_at_period_end",
        "over_limit_since",
        "values",
        "members_used",
        "grace_ends_at",
        "offers",
    }
    assert (body["source"], body["members_used"], body["values"]["places"]) == ("grant", 3, 1)
    assert body["values"]["online_payments"] is False
    assert datetime.fromisoformat(body["grace_ends_at"]) == EARLIER + timedelta(days=30)
    mine = {o["id"]: o for o in body["offers"] if o["id"] in {str(public), str(offered)}}
    assert mine == {
        str(public): {
            "id": str(public),
            "slug": mine[str(public)]["slug"],
            "name": {"en": "P", "nl": "P", "pt": "P"},
            "platform_fee_direct": 150,
            "platform_fee_marketplace": 900,
            "coupon_id": None,
        },
        str(offered): {
            "id": str(offered),
            "slug": mine[str(offered)]["slug"],
            "name": {"en": "P", "nl": "P", "pt": "P"},
            "platform_fee_direct": 0,
            "platform_fee_marketplace": 0,
            "coupon_id": "SPRING",
        },
    }
    assert len([o for o in body["offers"] if o["id"] == str(public)]) == 1
    assert str(for_b) not in {o["id"] for o in body["offers"]}
    assert worker.status_code == 200
    assert worker.headers["cache-control"] == "no-store"
    assert worker.json()["offers"] == []
    assert {k: v for k, v in worker.json().items() if k != "offers"} == {
        k: v for k, v in body.items() if k != "offers"
    }


# ZIF-78 fence. Kills: a refusal that does not name the feature and the limit (the console's one
# upgrade prompt reads them); a null limit dropped from the body; require_room letting the
# count-th one through.
def test_require_answers_402_with_feature_key(people: People) -> None:
    give_plan(people.a, {"places": 1, "email_reminders": True})
    app = create_app()

    @app.get("/api/test-require", tags=["test"])
    def gated(current: CurrentSession, used: int | None = None) -> dict[str, str]:
        if used is None:
            require(current.db, "online_payments")
            require(current.db, "email_reminders")
        else:
            require_room(current.db, "places", used)
        return {"ok": "yes"}

    client = signed_in(app, people.a, people.both)

    flag = client.get("/api/test-require")
    room = client.get("/api/test-require", params={"used": 1})
    under = client.get("/api/test-require", params={"used": 0})

    assert (flag.status_code, flag.json()) == (
        402,
        {"code": "plan_limit", "feature": "online_payments", "limit": None},
    )
    assert (room.status_code, room.json()) == (
        402,
        {"code": "plan_limit", "feature": "places", "limit": 1},
    )
    assert under.status_code == 200
    # Any other error keeps its body exactly.
    assert client.get("/api/test-require", params={"used": "x"}).json() == {
        "code": "invalid_request"
    }


def run_enforce() -> None:
    async def until_idle() -> None:
        while await run_once({"billing.enforce": KINDS["billing.enforce"]}):
            pass

    with jobs_as_the_worker():
        asyncio.run(until_idle())


def over_limit_since(tenant_id: uuid.UUID) -> datetime | None:
    with tenant_context(tenant_id) as db:
        found: datetime | None = db.scalar(text("SELECT over_limit_since FROM tenant_billing"))
    return found


def enqueue(tenant_id: uuid.UUID) -> None:
    with tenant_context(tenant_id) as db:
        enforcement.enqueue(db, tenant_id)


# ZIF-78 fence. Kills: the grace clock not started when members outnumber places; restarted on a
# second run (the 30 days would never end); not cleared once back within the places; an enqueue
# swallowed by an earlier finished job's dedupe key; a job kind the worker does not run, or cannot
# write as its role.
def test_enforce_stamps_and_clears_over_limit_since(people: People) -> None:
    give_plan(people.a, {"places": 1})  # a has an owner and a worker

    enqueue(people.a)
    run_enforce()
    stamped = over_limit_since(people.a)
    enqueue(people.a)
    run_enforce()
    again = over_limit_since(people.a)
    give_plan(people.a, {"places": 2})
    enqueue(people.a)
    run_enforce()

    assert stamped is not None
    assert again == stamped
    assert over_limit_since(people.a) is None
    assert over_limit_since(people.b) is None  # only the business named
