"""The suite runs one database per xdist worker (ZIF-111).

Everything here fences a detail that fails *silently* if it regresses: a wait helper that returns
without the interleaving it promised, or a URL rewrite that points every worker at the same
database.
"""

import hashlib
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from tests.conftest import (
    ADMIN_DATABASE_URL,
    API_DIR,
    ORIGINAL_URLS,
    URL_NAMES,
    XDIST_WORKER,
    wait_until_blocked,
    worker_url,
)

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
    """A lockable row in *this* worker's database, readable by the app role.

    try/finally, not the yield's implicit teardown alone: a serial run points migrate_engine at
    the developer's shared `ziftbook` (ZIF-111 §"a serial run still shares ziftbook with make up"),
    so this must not leave `fence` sitting there if the test body raises.
    """
    with migrate_engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS fence (id int PRIMARY KEY)"))
        conn.execute(text("GRANT SELECT, UPDATE ON fence TO ziftbook_app"))
        conn.execute(text("INSERT INTO fence VALUES (1) ON CONFLICT DO NOTHING"))
    try:
        yield None
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS fence"))


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


# 2 (GUARD): a row-lock waiter in this database is seen. Its kill set is a strict subset of test
# 3's (any regression that reds this one also reds test 3), so on its own it protects nothing; it
# exists so that a regression in `l.database` scoping is diagnosable by a single-role failure
# instead of only showing up as the cross-role test 3 going red.
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
    other = make_url(SAMPLE).set(database="ziftbook_143").render_as_string(hide_password=False)
    assert make_url(worker_url(other, "gw9")).database == "ziftbook_143_gw9"

    for bad in ("zift-book", "x;drop", "z" * 51):
        with pytest.raises(RuntimeError):
            worker_url(make_url(SAMPLE).set(database=bad).render_as_string(), "gw9")

    if XDIST_WORKER:
        base = make_url(ORIGINAL_URLS["ZIF_MIGRATE_DATABASE_URL"]).database
        for name in URL_NAMES:
            assert make_url(os.environ[name]).database == f"{base}_{XDIST_WORKER}"


# 5 (FENCE): the inverse defect. A serial run must be left alone. Rewriting unconditionally gives
# `ziftbook_None`; guarding on `"PYTEST_XDIST_WORKER" in os.environ` gives `ziftbook_` for an
# exported empty value. Measured with pytest-xdist 3.8.0: the variable is absent both at `-n 0`
# and with no flag at all, so only an operator's own export reaches the empty case -- but it is
# one export away, and `if not worker` costs nothing.
# Gated on the raw `os.environ.get("PYTEST_XDIST_WORKER")`, not the imported XDIST_WORKER, and
# asserted against ORIGINAL_URLS (captured at import, before conftest's rewrite loop), not
# os.environ: a mutation to `XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER") or "gw0"` makes
# XDIST_WORKER truthy even in a serial run, so gating on the imported constant (as this test used
# to) skips the whole check right when it needs to fire -- and even reading it, comparing against
# os.environ[name] would have been a self-comparison against the same rewrite the code under test
# performed, not an independent check.
@pytest.mark.parametrize("worker", [None, ""])
def test_a_serial_run_targets_the_shared_database(worker: str | None) -> None:
    assert worker_url(SAMPLE, worker) == SAMPLE

    if not os.environ.get("PYTEST_XDIST_WORKER"):
        for name in URL_NAMES:
            assert make_url(os.environ[name]).database == make_url(ORIGINAL_URLS[name]).database


# migrations/env.py's session-level pg_advisory_lock serialises concurrent migration runs *per
# database*, not cluster-wide (PostgreSQL's own documented behaviour -- advisory lock keys are
# scoped to the session's database, not the instance). That is what lets N xdist workers migrate
# their own database at once instead of queueing behind one lock; making it cluster-wide (an
# advisory lock taken against a shared database, say) would serialise them. There used to be a
# test here asserting exactly that fact against a throwaway database; it exercised PostgreSQL, not
# env.py, and stayed green with env.py's lock deleted entirely, so it was deleted.


# ZIF-120: a worker drops its own database at the end of a green run. Each test runs a nested suite
# of one test on a base name of its own, derived from this worker's database so concurrent runs
# never share it, and checks pg_database afterwards.
GREEN = "def test_green() -> None:\n    pass\n"
RED = "def test_red() -> None:\n    assert False\n"


def exists(name: str) -> bool:
    admin = create_engine(ADMIN_DATABASE_URL, poolclass=NullPool)
    with admin.connect() as conn:
        found = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name})
    return found is not None


def create(name: str) -> None:
    """Dropped first: one a killed earlier run left would otherwise fail every run after it."""
    admin = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        conn.execute(text(f"CREATE DATABASE {name} OWNER ziftbook_migrate"))


def drop(*names: str) -> None:
    # No FORCE: the nested suite has exited, so a refusal here means something still holds it.
    admin = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        for name in names:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))


def nested_base(suffix: str) -> str:
    """A short plain identifier unique to this worker's database: `<database>_<suffix>` itself
    could pass the 50-character base cap and fail the nested run against correct code."""
    database = make_url(os.environ["ZIF_MIGRATE_DATABASE_URL"]).database or ""
    return f"zif_nested_{hashlib.sha256(database.encode()).hexdigest()[:12]}_{suffix}"


def run_suite(tmp_path: Path, base: str, body: str, workers: bool) -> tuple[int, str]:
    """The exit code and output of a suite of one test, loading this conftest, on databases named
    after base.

    Every PYTEST_* variable is stripped: the nested controller must not inherit this worker's id.
    """
    (tmp_path / "test_nested.py").write_text(body)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    for name in URL_NAMES:
        env[name] = make_url(env[name]).set(database=base).render_as_string(hide_password=False)
    args = ["-n", "1"] if workers else ["-p", "no:xdist"]
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "tests.conftest", "-p", "no:cacheprovider"]
        + args
        + [str(tmp_path / "test_nested.py")],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return done.returncode, done.stdout[-2000:] + done.stderr[-2000:]


# 6 (FENCE): a green run leaves no worker database behind, and drops only its own. Kills: no
# cleanup at all (gw0 survives), and a pattern drop of `<base>_gw%` or of worker ids the run did
# not use (the gw1 standing in for another run's database goes too).
def test_a_green_run_drops_its_own_worker_database_only(tmp_path: Path) -> None:
    base = nested_base("green")
    create(f"{base}_gw1")
    try:
        code, output = run_suite(tmp_path, base, GREEN, workers=True)
        assert code == 0, output
        assert not exists(f"{base}_gw0")
        assert exists(f"{base}_gw1")
    finally:
        drop(f"{base}_gw0", f"{base}_gw1")


# 7 (FENCE): a red run keeps its worker database to inspect. Kills dropping it whatever the outcome.
def test_a_red_run_keeps_its_worker_database(tmp_path: Path) -> None:
    base = nested_base("red")
    try:
        code, output = run_suite(tmp_path, base, RED, workers=True)
        assert code == 1, output
        assert exists(f"{base}_gw0")
    finally:
        drop(f"{base}_gw0")


# 8 (FENCE): a green serial run never drops the base database it ran on: in a serial run that is
# the developer's `ziftbook`, the one `make up` serves. Kills a cleanup not gated on a worker id.
def test_a_green_serial_run_keeps_the_base_database(tmp_path: Path) -> None:
    base = nested_base("serial")
    create(base)
    try:
        code, output = run_suite(tmp_path, base, GREEN, workers=False)
        assert code == 0, output
        assert exists(base)
    finally:
        drop(base)
