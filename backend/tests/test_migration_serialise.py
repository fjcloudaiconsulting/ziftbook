"""Two migration runs against one database take turns on env.py's advisory lock instead of both
running the same revision (ZIF-119)."""

import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.pool import NullPool

from tests.conftest import ADMIN_DATABASE_URL, API_DIR

# Held by the test so the first run stops inside its migration, still holding env.py's lock.
GATE = 119
# The real env.py runs this one revision, against a database of its own: it parks on the gate, then
# records that it ran. lock_timeout 0 because the gate is held on purpose, for longer than env.py's
# own lock_timeout (ZIF-34) would allow.
PROBE = f"""
from alembic import op

revision = "probe"
down_revision = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = 0")
    op.execute("SELECT pg_advisory_xact_lock({GATE})")
    op.execute("INSERT INTO runs DEFAULT VALUES")
"""
# One `alembic upgrade head` with the real env.py and only the probe revision. A process each, as
# two pods would be: Alembic's `context` is process-global, so two runs in one process collide.
UPGRADE = """
import sys
from alembic import command
from alembic.config import Config

cfg = Config(toml_file=sys.argv[1])
cfg.set_main_option("version_locations", sys.argv[2])
cfg.set_main_option("path_separator", "os")
command.upgrade(cfg, "head")
"""
# Every advisory lock a session in this database is waiting for, with who it waits on. A bigint key
# is split into classid (high 32 bits) and objid (low 32 bits).
ADVISORY_WAITS = text("""
SELECT l.pid, (l.classid::bigint << 32) | l.objid::bigint AS key, pg_blocking_pids(l.pid) AS by
FROM pg_locks l
WHERE l.locktype = 'advisory' AND NOT l.granted
  AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
""")


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Engine]]:
    """A directory holding only the probe revision, and an engine on the database it migrates: this
    worker's own, created here with a `runs` table and dropped after."""
    base = make_url(os.environ["ZIF_MIGRATE_DATABASE_URL"])
    # It renders into DROP DATABASE as the superuser: a plain identifier only, and short enough that
    # Postgres's 63-byte truncation can't cut the suffix off and leave the base database's name.
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,49}", base.database or ""):
        raise RuntimeError(f"database name {base.database!r} is not a plain identifier")
    name = f"{base.database}_serialise"
    admin = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        conn.execute(text(f"CREATE DATABASE {name} OWNER ziftbook_migrate"))
    # A backstop only: a run left waiting by a failed assertion ends after 30s, not never.
    url = base.set(database=name).update_query_dict({"options": "-c statement_timeout=30s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", url.render_as_string(hide_password=False))
    engine = create_engine(url, poolclass=NullPool)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE runs (id serial)"))
    (tmp_path / "probe.py").write_text(PROBE)
    try:
        yield tmp_path, engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            # FORCE: this worker's own database, and a run that never finished must not keep it.
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


def start_upgrade(versions: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", UPGRADE, str(API_DIR / "pyproject.toml"), str(versions)],
        cwd=API_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def advisory_waits(engine: Engine, count: int) -> list[tuple[int, int, list[int]]]:
    """The advisory waits in this database once there are `count` of them."""
    rows: list[tuple[int, int, list[int]]] = []
    for _ in range(300):
        with engine.connect() as conn:
            rows = [(pid, key, list(by)) for pid, key, by in conn.execute(ADVISORY_WAITS)]
        if len(rows) >= count:
            return rows
        time.sleep(0.05)
    raise AssertionError(f"never saw {count} advisory lock waits, last saw {rows}")


# Kills: no pg_advisory_lock in env.py, pg_try_advisory_lock (does not wait), and
# pg_advisory_xact_lock (released by the commit before the first migration). In each, the second run
# never waits on an advisory lock: it heads into the probe revision too, blocked on the first run's
# uncommitted alembic_version instead.
def test_a_second_run_waits_for_the_first_to_finish(scratch: tuple[Path, Engine]) -> None:
    versions, engine = scratch
    runs: list[subprocess.Popen[str]] = []
    with engine.connect() as gate:
        gate.execute(text("SELECT pg_advisory_lock(:key)"), {"key": GATE})
        try:
            runs.append(start_upgrade(versions))
            [(first, key, _)] = advisory_waits(engine, 1)
            assert key == GATE  # the first run is inside its migration, holding env.py's lock

            runs.append(start_upgrade(versions))
            waits = advisory_waits(engine, 2)
            [(_, key, by)] = [wait for wait in waits if wait[0] != first]
            # The second run waits on an advisory lock that the first run holds, not on the gate.
            assert key != GATE, waits
            assert by == [first], waits
        finally:
            gate.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": GATE})
            outputs = [run.communicate(timeout=60)[0] for run in runs]
    assert [run.returncode for run in runs] == [0, 0], outputs
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM runs")) == 1
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "probe"
