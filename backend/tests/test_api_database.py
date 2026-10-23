import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool, QueuePool

from app import auth, db, main
from app.config import WorkerSettings
from app.db import SessionLocal
from app.main import create_app
from tests.conftest import ADMIN_DATABASE_URL, new_client


def test_the_contract_builds_without_a_database_url() -> None:
    # As `make openapi` runs it: a fresh process, so reading the URL at import time fails too.
    env = {k: v for k, v in os.environ.items() if k != "ZIF_DATABASE_URL"}
    result = subprocess.run(
        [sys.executable, "-m", "app.main"],
        env=env,
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_the_worker_still_requires_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZIF_DATABASE_URL")

    with pytest.raises(ValidationError):
        WorkerSettings()


def test_the_api_is_bound_to_its_database_while_it_runs(migrated: None) -> None:
    with TestClient(create_app()):
        with SessionLocal() as session:
            # No statement values (password hashes, emails) in error messages or logs.
            assert session.get_bind().engine.hide_parameters
            # The role from ZIF_DATABASE_URL, not the migrate URL.
            assert session.scalar(text("SELECT current_user")) == "ziftbook_app"
    # Unbound after shutdown, so later code can't use a disposed engine by accident.
    assert SessionLocal.kw.get("bind") is None


# ZB-31 FENCE. Wrong impl: the API's pool hands a connection to its next user with the session
# settings the last one left on it (rollback alone keeps a committed SET). The engine's own
# lock_timeout (ZIF-114) and the role's search path are what the reset returns to.
def test_the_api_hands_out_a_pooled_connection_without_the_last_users_settings(
    migrated: None,
) -> None:
    with TestClient(create_app()):  # the lifespan binds the API's own pooled engine
        with SessionLocal() as session:
            used = session.scalar(text("SELECT pg_backend_pid()"))
            session.execute(text("SET app.audit_review = 'on'"))
            session.commit()
        with SessionLocal() as session:
            reused = session.execute(
                text("""
                SELECT pg_backend_pid(), current_setting('app.audit_review', true),
                       current_setting('lock_timeout'), current_setting('search_path')
                """)
            ).one()
    assert tuple(reused) == (used, "", "5s", "public, pg_temp")


# ZB-31 FENCE. Wrong impl: a reset of settings and temporary tables only, which leaves the last
# user's session-level advisory lock held, its WITH HOLD cursor open and its LISTEN listening.
def test_a_pooled_connection_is_handed_out_without_the_last_users_locks_cursors_or_listens(
    migrated: None,
) -> None:
    engine = create_engine(os.environ["ZIF_DATABASE_URL"], pool_size=1, max_overflow=0)
    event.listen(engine, "reset", db.reset_session)
    try:
        with engine.begin() as conn:
            used = conn.scalar(text("SELECT pg_backend_pid()"))
            conn.execute(text("SELECT pg_advisory_lock(31)"))
            conn.execute(text("DECLARE left_open CURSOR WITH HOLD FOR SELECT 1"))
            conn.execute(text("LISTEN left_listening"))
        # More check-ins than psycopg's prepare threshold (5): it never prepares the reset (several
        # statements cannot be), so the reset never fails and the pool keeps the connection.
        for _ in range(6):
            engine.connect().close()
        with engine.connect() as conn:
            reused = conn.execute(
                text("""
                SELECT pg_backend_pid(),
                       (SELECT count(*) FROM pg_locks
                        WHERE locktype = 'advisory' AND pid = pg_backend_pid()),
                       (SELECT count(*) FROM pg_cursors WHERE name = 'left_open'),
                       (SELECT count(*) FROM pg_listening_channels())
                """)
            ).one()
    finally:
        engine.dispose()
    assert tuple(reused) == (used, 0, 0, 0)


# ZB-31 FENCE. Wrong impl: a reset that leaves temporary tables behind (RESET ALL alone). Only the
# database owner may create one now, so the migrate role shows it, through the same listener.
def test_a_pooled_connection_is_handed_out_without_the_last_users_temporary_tables(
    migrated: None,
) -> None:
    engine = create_engine(os.environ["ZIF_MIGRATE_DATABASE_URL"], pool_size=1, max_overflow=0)
    event.listen(engine, "reset", db.reset_session)
    try:
        with engine.begin() as conn:
            used = conn.scalar(text("SELECT pg_backend_pid()"))
            conn.execute(text("CREATE TEMP TABLE left_behind (y int)"))
        with engine.connect() as conn:
            reused = conn.execute(
                text("SELECT pg_backend_pid(), to_regclass('pg_temp.left_behind')")
            ).one()
    finally:
        engine.dispose()
    assert tuple(reused) == (used, None)


# ZB-16 FENCE. Wrong impl: the API engine on SQLAlchemy's pool defaults, where a request that finds
# no free connection waits 30s for one, or with no idle-in-transaction timeout. At most 15
# connections, as the defaults gave: more is a sizing decision (main.py's comment), not a drift.
def test_the_api_pool_and_its_idle_in_transaction_timeout_are_explicit(migrated: None) -> None:
    with TestClient(create_app()):
        pool = SessionLocal.kw["bind"].pool
        with SessionLocal() as session:
            idle = session.scalar(text("SHOW idle_in_transaction_session_timeout"))
            lock = session.scalar(text("SHOW lock_timeout"))
    assert isinstance(pool, QueuePool)
    assert (pool.size(), pool._max_overflow, pool.timeout()) == (5, 10, 5)
    assert (idle, lock) == ("1min", "5s")  # the lock_timeout (ZIF-114) is still there beside it


def _backend_alive(pid: int) -> bool:
    """As the superuser: another role's sessions are masked from the app role."""
    admin = create_engine(ADMIN_DATABASE_URL, poolclass=NullPool)
    try:
        with admin.connect() as conn:
            alive: bool = conn.scalar(
                text("SELECT EXISTS (SELECT FROM pg_stat_activity WHERE pid = :pid)"), {"pid": pid}
            )
    finally:
        admin.dispose()
    return alive


# ZB-16 FENCE. Wrong impl: the timeout set where the API's connections never read it (the URL, whose
# options connect_args replaces; a SET after connecting, which the pool's reset undoes). A tiny
# timeout instead of a minute's wait: the server, not the app, ends the idle transaction.
def test_the_server_ends_an_api_transaction_left_idle(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "IDLE_IN_TRANSACTION_TIMEOUT", "100ms")
    with TestClient(create_app()), SessionLocal() as session:
        pid = session.scalar(text("SELECT pg_backend_pid()"))  # the transaction is open from here
        deadline = time.monotonic() + 10
        while _backend_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _backend_alive(pid)
        with pytest.raises(DBAPIError) as ended:
            session.execute(text("SELECT 1"))
    assert getattr(ended.value.orig, "sqlstate", None) == "25P03"  # idle_in_transaction_timeout


# ZB-16 FENCE. Wrong impl: a request that waits out the pool timeout answered 500 (SQLAlchemy's
# pool TimeoutError is no OperationalError, so the 55P03 handler never sees it).
def test_a_request_that_gets_no_pooled_connection_is_a_503_busy(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "POOL_SIZE", 1)
    monkeypatch.setattr(main, "MAX_OVERFLOW", 0)
    monkeypatch.setattr(main, "POOL_TIMEOUT", 0.1)
    with new_client(create_app()) as client, SessionLocal() as holder:
        holder.connection()  # the pool's one connection, checked out until the end
        # A session cookie, so the request needs the database to resolve it.
        response = client.get("/api/session", headers={"Cookie": f"{auth.COOKIE}=any"})
    assert (response.status_code, response.json()) == (503, {"code": "busy"})
