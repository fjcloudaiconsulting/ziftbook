"""Plans, trial and billing tables (ZIF-78, migrations 0103 and 0104)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from fastapi import FastAPI
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from app.db import tenant_context
from tests.conftest import (
    EmptyInstall,
    People,
    fresh_email,
    issue_link,
    member_id,
    migrate_tenant_context,
    new_client,
)
from tests.test_migrations import lock_timeout_config
from tests.test_sign_up import app as _app  # noqa: F401 (fixtures)
from tests.test_sign_up import businesses as _businesses  # noqa: F401
from tests.test_sign_up import complete, created

TABLES = (
    "plans",
    "platform_billing",
    "tenant_billing",
    "subscriptions",
    "business_overrides",
    "plan_offers",
    "plan_grants",
)
OBJECTS = text("""
SELECT (SELECT count(*) FROM pg_class
         WHERE relnamespace = 'public'::regnamespace AND relname = ANY(:tables)),
       (SELECT count(*) FROM information_schema.columns
         WHERE table_name = 'memberships' AND column_name IN ('keeps_access', 'access_paused_at')),
       EXISTS (SELECT FROM pg_trigger WHERE tgname = 'tenant_billing_on_tenant'),
       to_regprocedure('tenant_billing_on_tenant()') IS NOT NULL
""")
TRIAL_ENDS = text("SELECT trial_ends_at FROM tenant_billing WHERE tenant_id = :t")


@pytest.fixture
def trial(migrate_engine: Engine) -> Iterator[Any]:
    """Set platform_billing's trial (plan or None, days) as an operator would; put it back after."""
    with migrate_engine.begin() as conn:
        saved = conn.execute(text("SELECT trial_plan_id, trial_days FROM platform_billing")).one()
        plan = conn.scalar(
            text("""INSERT INTO plans (slug, name) VALUES (:s, '{"en":"T","nl":"T","pt":"T"}')
                    RETURNING id"""),
            {"s": f"trial-{uuid.uuid4().hex[:8]}"},
        )

    def set_trial(with_plan: bool, days: int) -> None:
        with migrate_engine.begin() as conn:
            conn.execute(
                text("UPDATE platform_billing SET trial_plan_id = :p, trial_days = :d"),
                {"p": plan if with_plan else None, "d": days},
            )

    yield set_trial
    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE platform_billing SET trial_plan_id = :p, trial_days = :d"),
            {"p": saved.trial_plan_id, "d": saved.trial_days},
        )
        conn.execute(text("DELETE FROM plans WHERE id = :p"), {"p": plan})


# ZIF-78 fence. Kills: no Free plan or no platform row (every business would resolve to nothing),
# more than one platform row, or a Free plan seeded without its one place. On the empty install: the
# suite points this database's default at an all-features plan (conftest, ZIF-78 ruling R4).
def test_free_plan_and_platform_row_are_seeded(empty_database: EmptyInstall) -> None:
    with empty_database.app.connect() as conn:
        rows = conn.execute(
            text("""SELECT p.slug, p.values, p.public, p.name, p.stripe_price_id
                    FROM platform_billing b JOIN plans p ON p.id = b.default_plan_id""")
        ).all()
    assert len(rows) == 1
    slug, values, public, name, price = rows[0]
    assert (slug, values["places"], values["email_reminders"], public, price) == (
        "free",
        1,
        True,
        True,
        None,
    )
    assert name == {"en": "Free", "nl": "Gratis", "pt": "Grátis"}


# ZIF-78 fence. Kills: a trial not copied at sign-up, or copied from somewhere other than
# platform_billing's trial_days.
def test_a_new_business_gets_its_trial_from_platform_billing(
    trial: Any,
    _app: FastAPI,  # noqa: F811
    app_engine: Engine,
    _businesses: list[dict[str, Any]],  # noqa: F811
) -> None:
    trial(True, 14)
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None

    session = created(complete(new_client(_app), token, country="NL"), _businesses)

    with tenant_context(uuid.UUID(session["tenant_id"])) as db:
        ends = db.scalar(TRIAL_ENDS, {"t": session["tenant_id"]})
    assert abs(ends - (datetime.now(UTC) + timedelta(days=14))) < timedelta(minutes=1)


# ZIF-78 fence. Kills: a trial end set with no trial plan, or with zero trial days; a business
# created without its tenant_billing row.
@pytest.mark.parametrize(("with_plan", "days"), [(False, 14), (True, 0)])
def test_no_trial_plan_means_no_trial_end(
    trial: Any, migrate_engine: Engine, with_plan: bool, days: int
) -> None:
    trial(with_plan, days)
    caller = str(uuid.uuid4())
    with migrate_engine.connect() as conn:  # rolled back on close
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": caller})
        tenant = conn.scalar(text("INSERT INTO tenants (name) VALUES ('No trial') RETURNING id"))
        # The trigger put the caller's tenant setting back.
        assert conn.scalar(text("SELECT current_setting('app.tenant_id')")) == caller
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
        rows = conn.execute(TRIAL_ENDS, {"t": tenant}).all()
    assert [tuple(r) for r in rows] == [(None,)]


# ZIF-78 fence. Kills: any write on the billing tables for the app role, which would let a business
# give itself a plan, a grant, an override or a subscription, or lift its own pause. Admin writes
# arrive as definer functions; Stripe's as the worker.
@pytest.mark.parametrize(
    "statement",
    [
        """INSERT INTO plans (slug, name) VALUES ('mine', '{"en":"M","nl":"M","pt":"M"}')""",
        "UPDATE plans SET public = true",
        "DELETE FROM plans",
        "INSERT INTO platform_billing (id, default_plan_id) SELECT false, id FROM plans",
        "UPDATE platform_billing SET trial_days = 90",
        """INSERT INTO business_overrides (tenant_id, key, value, reason, admin_user_id)
           VALUES (:t, 'places', '9', 'x', :t)""",
        "UPDATE business_overrides SET value = '9'",
        """INSERT INTO plan_offers (tenant_id, plan_id, reason, admin_user_id)
           SELECT :t, id, 'x', :t FROM plans""",
        "UPDATE plan_offers SET stripe_coupon_id = 'x'",
        """INSERT INTO plan_grants (tenant_id, plan_id, reason, admin_user_id)
           SELECT :t, id, 'x', :t FROM plans""",
        "UPDATE plan_grants SET ends_at = NULL",
        """INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id, status)
           VALUES (:t, 'cus_1', 'sub_1', 'active')""",
        "UPDATE subscriptions SET status = 'active'",
        "UPDATE tenant_billing SET over_limit_since = NULL",
        "UPDATE tenant_billing SET trial_ends_at = NULL",
        "UPDATE memberships SET access_paused_at = NULL",
        "DELETE FROM subscriptions",
        "DELETE FROM tenant_billing",
        "DELETE FROM business_overrides",
        "DELETE FROM plan_offers",
        "DELETE FROM plan_grants",
    ],
)
def test_app_role_cannot_write_billing_tables(people: People, statement: str) -> None:
    with pytest.raises(ProgrammingError) as error, tenant_context(people.a) as session:
        session.execute(text(statement), {"t": people.a})
    assert isinstance(error.value.orig, InsufficientPrivilege)


# ZIF-78 fence. Kills: the app role unable to read a billing table, or to record who keeps access.
def test_app_role_reads_billing_and_picks_who_keeps_access(people: People) -> None:
    with tenant_context(people.a) as session:
        for table in TABLES:
            session.execute(text(f"SELECT * FROM {table}")).all()
        session.execute(
            text("UPDATE memberships SET keeps_access = true WHERE id = :m"),
            {"m": member_id(people.a, people.both)},
        )


# ZIF-78 fence. Kills: a worker that cannot record a Stripe subscription, stamp a business over its
# limit, or pause a member's access.
def test_worker_writes_subscriptions_and_pauses(
    people: People, worker_engine: Engine, migrate_engine: Engine
) -> None:
    try:
        with migrate_tenant_context(worker_engine, people.a) as conn:
            conn.execute(
                text("""INSERT INTO subscriptions
                          (tenant_id, stripe_customer_id, stripe_subscription_id, status)
                        VALUES (:t, 'cus_A1', 'sub_A1', 'incomplete')"""),
                {"t": people.a},
            )
            conn.execute(text("UPDATE subscriptions SET status = 'active', updated_at = now()"))
            conn.execute(text("UPDATE tenant_billing SET over_limit_since = now()"))
            paused = conn.execute(
                text("UPDATE memberships SET access_paused_at = now() WHERE id = :m"),
                {"m": member_id(people.a, people.only_a)},
            )
            assert paused.rowcount == 1
        with migrate_tenant_context(worker_engine, people.a) as conn:
            assert conn.scalar(text("SELECT status FROM subscriptions")) == "active"
            assert conn.scalar(text("SELECT over_limit_since IS NOT NULL FROM tenant_billing"))
    finally:
        with migrate_tenant_context(migrate_engine, people.a) as conn:
            conn.execute(text("DELETE FROM subscriptions"))
            conn.execute(text("UPDATE tenant_billing SET over_limit_since = NULL"))


# ZIF-78 fence. Kills: a 0104 downgrade that leaves a memberships column or takes 0103's objects;
# a 0103 downgrade that leaves a table, the trigger or its function behind; an upgrade that skips
# existing businesses' 30-day trial end.
def test_migration_round_trips(
    people: People, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    try:
        for revision, left in (("0103", (7, 0, True, True)), ("0102", (0, 0, False, False))):
            command.downgrade(cfg, revision)
            with migrate_engine.connect() as conn:
                assert tuple(conn.execute(OBJECTS, {"tables": list(TABLES)}).one()) == left
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert tuple(conn.execute(OBJECTS, {"tables": list(TABLES)}).one()) == (7, 2, True, True)
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        ends = conn.scalar(TRIAL_ENDS, {"t": people.a})
    assert abs(ends - (datetime.now(UTC) + timedelta(days=30))) < timedelta(minutes=1)
