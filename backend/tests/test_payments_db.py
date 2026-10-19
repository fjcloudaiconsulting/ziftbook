"""ZIF-158 B1: payment_accounts as the database enforces it."""

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from tests.conftest import People


def scoped(conn: object, tenant_id: uuid.UUID) -> None:
    conn.execute(  # type: ignore[attr-defined]
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


def insert(engine: Engine, tenant_id: uuid.UUID, account: str, fee_mode: str | None = None) -> None:
    with engine.begin() as conn:
        scoped(conn, tenant_id)
        if fee_mode is None:
            conn.execute(
                text("INSERT INTO payment_accounts (tenant_id, stripe_account_id) VALUES (:t, :a)"),
                {"t": tenant_id, "a": account},
            )
        else:
            conn.execute(
                text(
                    "INSERT INTO payment_accounts (tenant_id, stripe_account_id, fee_mode) "
                    "VALUES (:t, :a, :m)"
                ),
                {"t": tenant_id, "a": account, "m": fee_mode},
            )


def refused(engine: Engine, tenant_id: uuid.UUID, sql: str, **params: object) -> tuple[str, str]:
    """(sqlstate, constraint) of the error the statement raises as that tenant."""
    with pytest.raises(DBAPIError) as raised:
        with engine.begin() as conn:
            scoped(conn, tenant_id)
            conn.execute(text(sql), params)
    diag = raised.value.orig.diag  # type: ignore[union-attr]
    return raised.value.orig.sqlstate, diag.constraint_name or ""  # type: ignore[union-attr]


def unique_account() -> str:
    return "acct_" + uuid.uuid4().hex[:16]


# ZIF-158 F7. fence. Kills: a tenants column (no RLS: a bad WHERE repoints payouts); a table-level
# UPDATE grant; the write-once trigger missing (owner-role scripts could rewrite the account).
def test_a_business_cannot_touch_another_business_s_account(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    mine, theirs = unique_account(), unique_account()
    insert(app_engine, people.a, mine)
    insert(app_engine, people.b, theirs)

    # RLS WITH CHECK: a row for the other business.
    assert (
        refused(
            app_engine,
            people.a,
            "INSERT INTO payment_accounts (tenant_id, stripe_account_id) VALUES (:t, :a)",
            t=people.b,
            a=unique_account(),
        )[0]
        == "42501"
    )
    with app_engine.begin() as conn:
        scoped(conn, people.a)
        flipped = conn.execute(
            text("UPDATE payment_accounts SET charges_enabled = true WHERE tenant_id = :b"),
            {"b": people.b},
        )
        assert flipped.rowcount == 0
    # No column privilege on the account id.
    assert (
        refused(
            app_engine,
            people.a,
            "UPDATE payment_accounts SET stripe_account_id = :a",
            a=unique_account(),
        )[0]
        == "42501"
    )
    # The trigger holds against the owner role too.
    assert refused(
        migrate_engine,
        people.a,
        "UPDATE payment_accounts SET stripe_account_id = :a",
        a=unique_account(),
    ) == ("23514", "payment_account_write_once")
    assert refused(app_engine, people.a, "DELETE FROM payment_accounts")[0] == "42501"


# ZIF-158 F8. fence. Kills: a plain SQL SECURITY DEFINER function (NULL under FORCE RLS, every event
# dropped); not restoring the flag (it would open every account to later statements).
def test_the_webhook_lookup_sees_one_account_and_leaves_no_trace(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    account = unique_account()
    insert(app_engine, people.a, account)

    with app_engine.begin() as conn:
        found = conn.scalar(text("SELECT stripe_account_tenant(:a)"), {"a": account})
        unknown = conn.scalar(text("SELECT stripe_account_tenant('acct_nobody')"))
        left = conn.execute(
            text(
                "SELECT current_setting('app.tenant_id', true), "
                "current_setting('app.stripe_webhook', true)"
            )
        ).one()
    assert (found, unknown, tuple(left)) == (people.a, None, ("", ""))

    with migrate_engine.begin() as conn:
        scoped(conn, uuid.UUID(int=0))
        assert conn.scalar(text("SELECT count(*) FROM payment_accounts")) == 0


# ZIF-158 F15. fence. Kills: a config that "switches" an existing account to platform-pays while
# Stripe keeps billing the business.
def test_the_fee_payer_is_fixed_with_the_account(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    insert(app_engine, people.a, unique_account())
    fixed = ("23514", "payment_account_fee_payer_fixed")

    assert (
        refused(migrate_engine, people.a, "UPDATE payment_accounts SET fee_mode = 'platform_pays'")
        == fixed
    )

    with migrate_engine.begin() as conn:
        scoped(conn, people.a)
        conn.execute(
            text(
                "UPDATE payment_accounts SET fee_mode = 'business_pays_plus_fee', "
                "application_fee_bp = 150"
            )
        )
    # Mode 2 carries the fee; mode 1 with a fee is refused.
    assert refused(
        migrate_engine, people.a, "UPDATE payment_accounts SET fee_mode = 'business_pays'"
    ) == ("23514", "ck_payment_accounts_fee_only_in_mode_2")
    # Modes 1 and 2 switch freely.
    with migrate_engine.begin() as conn:
        scoped(conn, people.a)
        conn.execute(
            text("UPDATE payment_accounts SET fee_mode = 'business_pays', application_fee_bp = 0")
        )

    insert(app_engine, people.b, unique_account(), fee_mode="platform_pays")
    assert (
        refused(migrate_engine, people.b, "UPDATE payment_accounts SET fee_mode = 'business_pays'")
        == fixed
    )
