import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, event, text

from app import db
from app.config import WorkerSettings
from app.db import SessionLocal
from app.main import create_app


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
