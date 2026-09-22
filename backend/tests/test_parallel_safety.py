"""The suite runs one database per xdist worker (ZIF-111).

Everything here fences a detail that fails *silently* if it regresses: a wait helper that returns
without the interleaving it promised, or a URL rewrite that points every worker at the same
database.
"""

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from tests.conftest import (
    ADMIN_DATABASE_URL,
    SHARED_DATABASE,
    XDIST_WORKER,
    wait_until_blocked,
    worker_url,
)

# migrations/env.py's MIGRATION_LOCK_ID. Duplicated rather than imported: importing env.py runs
# the migrations.
MIGRATION_LOCK_ID = 7_428_211_906

URL_NAMES = ("ZIF_MIGRATE_DATABASE_URL", "ZIF_DATABASE_URL")
SAMPLE = "postgresql+psycopg://ziftbook_app:ziftbook_app@localhost:5432/ziftbook"


@pytest.fixture
def elsewhere() -> Iterator[Engine]:
    """A throwaway database of this instance, with one lockable row. Not this worker's."""
    name = f"zif_fence_{os.getpid()}"
    admin = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {name} OWNER ziftbook_migrate"))
    engine = create_engine(
        make_url(os.environ["ZIF_MIGRATE_DATABASE_URL"])
        .set(database=name)
        .render_as_string(hide_password=False),
        poolclass=NullPool,
    )
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE fence (id int PRIMARY KEY)"))
        conn.execute(text("INSERT INTO fence VALUES (1)"))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


@contextmanager
def blocked_on(holder: Engine, waiter: Engine) -> Iterator[None]:
    """Hold the fence row and queue a second session behind it, for as long as the caller wants."""
    row = text("SELECT FROM fence WHERE id = 1 FOR UPDATE")

    def wait() -> None:
        with waiter.begin() as conn:
            conn.execute(row)

    with holder.begin() as conn:
        conn.execute(row)
        thread = threading.Thread(target=wait)
        thread.start()
        yield None
    thread.join(timeout=10)
    assert not thread.is_alive()


@pytest.fixture
def blocked_elsewhere(elsewhere: Engine) -> Iterator[None]:
    with blocked_on(elsewhere, elsewhere):
        yield None


@pytest.fixture
def fence_row(app_engine: Engine, migrate_engine: Engine) -> Iterator[None]:
    """A lockable row in *this* worker's database, readable by the app role."""
    with migrate_engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS fence (id int PRIMARY KEY)"))
        conn.execute(text("GRANT SELECT, UPDATE ON fence TO ziftbook_app"))
        conn.execute(text("INSERT INTO fence VALUES (1) ON CONFLICT DO NOTHING"))
    yield None
    with migrate_engine.begin() as conn:
        conn.execute(text("DROP TABLE fence"))


# 1 (FENCE): an ungranted lock in another database of the same instance is not this test's
# interleaving. The shipped `count(DISTINCT pid) FROM pg_locks WHERE NOT granted` counts it and
# returns early, so the caller proceeds and asserts against a state it never reached.
def test_wait_until_blocked_ignores_another_databases_waiter(
    migrate_engine: Engine, elsewhere: Engine, blocked_elsewhere: None
) -> None:
    # First that the waiter really is blocked, watched from its own database: a fence that sees
    # nothing proves nothing. Then that this worker's connection does not see it.
    wait_until_blocked(elsewhere, 1)

    with pytest.raises(AssertionError, match="never waited on a lock"):
        wait_until_blocked(migrate_engine, 1)


# 2 (FENCE): a row-lock waiter in this database is seen. `AND l.database = (SELECT oid ...)` makes
# this permanently zero, because a row-lock waiter's transactionid lock carries database = NULL.
def test_wait_until_blocked_sees_a_waiter_in_this_database(
    fence_row: None, app_engine: Engine
) -> None:
    with blocked_on(app_engine, app_engine):
        wait_until_blocked(app_engine, 1)  # returns, or raises AssertionError


# 3 (FENCE): the waiter is the app role and the watcher is the migrate role -- three real call
# sites (test_invites_db:366, test_password_auth_db:293 and :371). pg_stat_activity masks
# wait_event_type, state and query across roles, so a pg_stat_activity-only helper reads 0 here
# and times out; pg_locks.granted is unmasked, which is why the join works.
def test_wait_until_blocked_sees_an_app_role_waiter_from_a_migrate_connection(
    fence_row: None, app_engine: Engine, migrate_engine: Engine
) -> None:
    with blocked_on(app_engine, app_engine):
        wait_until_blocked(migrate_engine, 1)


# 4 (FENCE): with a worker id set, a URL points at that worker's own database, and the suite
# running this test has actually had its own URLs rewritten. Omit either and every worker shares
# one database -- the whole ticket.
def test_each_xdist_worker_targets_its_own_database() -> None:
    assert make_url(worker_url(SAMPLE, "gw9")).database == "ziftbook_gw9"
    assert make_url(worker_url(SAMPLE, "gw9")).password == make_url(SAMPLE).password

    if XDIST_WORKER:
        for name in URL_NAMES:
            assert make_url(os.environ[name]).database == f"ziftbook_{XDIST_WORKER}"


# 5 (FENCE): the inverse defect. A serial run must be left alone. Rewriting unconditionally gives
# `ziftbook_None`; guarding on `"PYTEST_XDIST_WORKER" in os.environ` gives `ziftbook_` for an
# exported empty value. Measured with pytest-xdist 3.8.0: the variable is absent both at `-n 0`
# and with no flag at all, so only an operator's own export reaches the empty case -- but it is
# one export away, and `if not worker` costs nothing.
@pytest.mark.parametrize("worker", [None, ""])
def test_a_serial_run_targets_the_shared_database(worker: str | None) -> None:
    assert worker_url(SAMPLE, worker) == SAMPLE

    if not XDIST_WORKER:
        for name in URL_NAMES:
            assert make_url(os.environ[name]).database == SHARED_DATABASE


# 6 (GUARD): migrations/env.py's session-level pg_advisory_lock serialises concurrent migration
# runs *per database*, not cluster-wide. That is what lets N workers migrate at once; making it
# cluster-wide (an advisory lock on a shared database, say) would serialise them.
def test_the_migration_lock_is_per_database(elsewhere: Engine, migrate_engine: Engine) -> None:
    taken = text("SELECT pg_try_advisory_lock(:id)")
    with migrate_engine.connect() as here, elsewhere.connect() as there:
        assert here.scalar(taken, {"id": MIGRATION_LOCK_ID}) is True
        assert there.scalar(taken, {"id": MIGRATION_LOCK_ID}) is True
