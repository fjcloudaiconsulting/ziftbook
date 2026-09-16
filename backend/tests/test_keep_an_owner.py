"""The keep_an_owner trigger, exercised directly (not through the API): a business with members
always has an owner. app.members.target's caller-plus-target locks are what make the endpoints
themselves race-free; this file is the trigger's own last line of defence."""

import threading

import pytest
from psycopg.errors import CheckViolation, FeatureNotSupported
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import tenant_context
from tests.conftest import People, add_membership, add_user, set_role, wait_until_blocked


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE memberships SET role = 'worker' WHERE user_id = :u",
        "DELETE FROM memberships WHERE user_id = :u",
    ],
)
def test_changing_the_only_owner_is_refused(people: People, statement: str) -> None:
    with pytest.raises(IntegrityError) as error, tenant_context(people.a) as session:
        session.execute(text(statement), {"u": people.both})

    assert isinstance(error.value.orig, CheckViolation)
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.constraint_name == "last_owner"
    with tenant_context(people.a) as session:
        role = session.scalar(
            text("SELECT role FROM memberships WHERE user_id = :u"), {"u": people.both}
        )
    assert role == "owner"


def test_two_concurrent_demotions_leave_exactly_one_owner(
    people: People, app_engine: Engine
) -> None:
    set_role(people.a, people.only_a, "owner")
    outcomes: list[object] = []

    def demote_second() -> None:
        try:
            with app_engine.begin() as second:
                second.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)}
                )
                second.execute(
                    text("UPDATE memberships SET role = 'worker' WHERE user_id = :u"),
                    {"u": people.only_a},
                )
        except Exception as error:  # the expected outcome lands here, as does any bug
            outcomes.append(error)
        else:
            outcomes.append(None)

    with app_engine.begin() as first:
        first.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        first.execute(
            text("UPDATE memberships SET role = 'worker' WHERE user_id = :u"), {"u": people.both}
        )
        thread = threading.Thread(target=demote_second)
        thread.start()
        wait_until_blocked(app_engine, 1)
        # first commits here, on exiting the with-block, releasing the tenants row lock.
    thread.join(timeout=10)
    assert not thread.is_alive(), "the second demotion never finished"

    assert len(outcomes) == 1
    error = outcomes[0]
    assert isinstance(error, IntegrityError)
    assert isinstance(error.orig, CheckViolation)
    assert error.orig.diag.constraint_name == "last_owner"
    with tenant_context(people.a) as session:
        owners = set(session.scalars(text("SELECT user_id FROM memberships WHERE role = 'owner'")))
    assert owners == {people.only_a}


def test_the_function_and_privileges_the_trigger_needs(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    with app_engine.connect() as conn:
        assert (
            conn.scalar(
                text("SELECT has_any_column_privilege('ziftbook_app', 'tenants', 'UPDATE')")
            )
            is True
        )
        assert conn.scalar(text("SHOW default_transaction_isolation")) == "read committed"

    with migrate_engine.connect() as conn:
        row = conn.execute(
            text("""
            SELECT pg_get_userbyid(p.proowner) AS owner, p.prosecdef, p.provolatile, p.proconfig
            FROM pg_proc p
            WHERE p.pronamespace = 'public'::regnamespace AND p.proname = 'keep_an_owner'
            """)
        ).one()
    assert (row.owner, row.prosecdef, row.provolatile) == ("ziftbook_migrate", False, "v")
    assert "search_path=pg_catalog, public, pg_temp" in (row.proconfig or [])


def test_the_trigger_only_watches_an_owner_losing_the_role(
    people: People, app_engine: Engine
) -> None:
    # A worker leaving an ownerless business: the trigger doesn't fire (WHEN OLD.role = 'owner').
    # Kills a trigger without the WHEN condition: it would fire unconditionally, see b left with
    # one member and no owner, and wrongly refuse.
    with tenant_context(people.b) as session:
        session.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": people.only_b})

    # An owner demoted while another owner remains.
    set_role(people.a, people.only_a, "owner")
    set_role(people.a, people.both, "worker")

    # Owner to owner, as the business's only owner: the early return means this never takes the
    # tenants lock, so -- unlike a real demotion or removal -- it must not block even while another
    # connection holds that lock. A trigger without the early return would reach the same PERFORM
    # and wait; role = role always passes the no-owner check either way (the row's new value is
    # already visible to it), so only blocking, not the outcome, tells the two apart.
    with app_engine.connect() as holder:
        tx = holder.begin()
        holder.execute(
            text("SELECT FROM tenants WHERE id = :t FOR NO KEY UPDATE"), {"t": str(people.a)}
        )
        try:
            with tenant_context(people.a) as session:
                session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                session.execute(
                    text("UPDATE memberships SET role = role WHERE user_id = :u"),
                    {"u": people.only_a},
                )
                role = session.scalar(
                    text("SELECT role FROM memberships WHERE user_id = :u"), {"u": people.only_a}
                )
        finally:
            tx.rollback()
    assert role == "owner"


def test_tearing_down_a_whole_business_in_one_statement_is_not_a_last_owner_violation(
    app_engine: Engine, migrate_engine: Engine, bound: None
) -> None:
    with app_engine.begin() as conn:
        tenant_id = conn.scalar(text("INSERT INTO tenants (name) VALUES ('teardown') RETURNING id"))
    owner_id, worker_id = add_user(app_engine), add_user(app_engine)
    add_membership(tenant_id, owner_id, "owner")
    add_membership(tenant_id, worker_id, "worker")

    with tenant_context(tenant_id) as session:
        session.execute(text("DELETE FROM memberships"))  # both rows, one statement
    with migrate_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM users WHERE id IN (:o, :w)"), {"o": owner_id, "w": worker_id}
        )
        conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})


@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
def test_a_non_read_committed_change_is_refused(
    people: People, app_engine: Engine, level: str
) -> None:
    set_role(people.a, people.only_a, "owner")

    with app_engine.connect() as conn:
        conn.execution_options(isolation_level=level)
        with pytest.raises(DBAPIError) as error, conn.begin():
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
            conn.execute(
                text("UPDATE memberships SET role = 'worker' WHERE user_id = :u"),
                {"u": people.both},
            )
    assert isinstance(error.value.orig, FeatureNotSupported)


# Test 21 (guard): no new test needed. test_tenant_schema.py's isolation check, the definer check
# in test_password_auth_db.py (keep_an_owner is SECURITY INVOKER, so it never appears there), and
# test_openapi.py's stale-contract and Error-schema tests already cover this migration and its
# trigger; they are unaffected by it and keep passing.
