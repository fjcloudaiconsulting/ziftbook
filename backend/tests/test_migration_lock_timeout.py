"""A migration that can't get a table lock fails within env.py's lock_timeout instead of hanging the
rollout, while a run waiting its turn on the advisory lock waits as long as it takes (ZIF-34)."""

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from psycopg.errors import LockNotAvailable
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from tests.conftest import ADMIN_DATABASE_URL, API_DIR, wait_until_blocked

# The real env.py runs this one revision, against a database of its own, so the test never depends
# on what the current head migration happens to touch.
PROBE = """
from alembic import op

revision = "probe"
down_revision = None


def upgrade() -> None:
    op.execute("ALTER TABLE probe ADD COLUMN added integer")
"""
ADDED = text(
    "SELECT count(*) FROM information_schema.columns "
    "WHERE table_name = 'probe' AND column_name = 'added'"
)
# Same value as env.py's MIGRATION_LOCK_ID. Not decorative: if they drift, the waiting run never
# waits and wait_until_blocked fails.
MIGRATION_LOCK_ID = 7_428_211_906


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Config, Engine]]:
    """A Config that runs the real env.py with only the probe revision, and an engine on the
    database it migrates: this worker's own, created here with a `probe` table and dropped after."""
    base = make_url(os.environ["ZIF_MIGRATE_DATABASE_URL"])
    name = f"{base.database}_lock_timeout"
    admin = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        conn.execute(text(f"CREATE DATABASE {name} OWNER ziftbook_migrate"))
    # statement_timeout is only a backstop: a run with no lock_timeout ends here as QueryCanceled
    # after 30s instead of hanging the suite.
    url = base.set(database=name).update_query_dict({"options": "-c statement_timeout=30s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", url.render_as_string(hide_password=False))
    engine = create_engine(url, poolclass=NullPool)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE probe (id integer)"))
    (tmp_path / "probe.py").write_text(PROBE)
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    cfg.set_main_option("version_locations", str(tmp_path))
    cfg.set_main_option("path_separator", "os")
    try:
        yield cfg, engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        admin.dispose()


# Kills: no lock_timeout (the ALTER waits for the backstop and fails as QueryCanceled), SET LOCAL
# (gone at the commit right after it, same), a timeout set only after the migrations started.
def test_a_migration_behind_an_app_transaction_fails_on_the_lock_timeout(
    scratch: tuple[Config, Engine],
) -> None:
    cfg, engine = scratch
    with engine.connect() as app_transaction:
        # ACCESS SHARE on probe until this transaction ends, as any open read in the app holds.
        app_transaction.execute(text("SELECT FROM probe"))
        with pytest.raises(OperationalError) as caught:
            command.upgrade(cfg, "head")
    assert isinstance(caught.value.orig, LockNotAvailable), caught.value.orig
    with engine.connect() as conn:
        assert conn.scalar(ADDED) == 0


# Kills: lock_timeout set before pg_advisory_lock (or passed as a connect option), which turns a
# deploy waiting for another pod's run into a failure after 5s.
def test_a_run_waiting_its_turn_waits_past_the_lock_timeout(
    scratch: tuple[Config, Engine],
) -> None:
    cfg, engine = scratch
    errors: list[BaseException] = []

    def upgrade() -> None:
        try:
            command.upgrade(cfg, "head")
        except BaseException as error:
            errors.append(error)

    with engine.connect() as other_run:
        other_run.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        waiting = threading.Thread(target=upgrade)
        waiting.start()
        try:
            wait_until_blocked(engine, 1)
            time.sleep(6)  # past the 5s lock_timeout: a bounded wait has failed by now
            assert not errors, errors
            assert waiting.is_alive()
        finally:
            other_run.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
            waiting.join(timeout=30)
    assert not errors, errors
    with engine.connect() as conn:
        assert conn.scalar(ADDED) == 1
