"""A deploy's migration run retries once when autovacuum deadlocks it, and new migrations lock a
table before changing grants on it when they also alter it (ZIF-287)."""

import ast
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg.errors import DeadlockDetected, LockNotAvailable
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from tests.conftest import ADMIN_DATABASE_URL, API_DIR

# What a client gets from Postgres for a real deadlock: pids, lock modes and OIDs.
DETAIL = (
    "Process 1 waits for AccessExclusiveLock on relation 2 of database 3; blocked by process 4. "
    "Process 4 waits for ShareLock on transaction 5; blocked by process 1."
)
ONE = """
from alembic import op

revision = "one"
down_revision = None


def upgrade() -> None:
    op.execute("INSERT INTO runs VALUES ('one')")
"""
# The stand-in: the first {failures} tries of it fail with {errcode}. Each try first records, in
# autocommit, the lock_timeout it started with, then leaves it 0 as a failed 0072 build does.
TWO = """
from alembic import op

revision = "two"
down_revision = "one"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("INSERT INTO tries VALUES (current_setting('lock_timeout'))")
        op.execute("SET lock_timeout = 0")
    op.execute("INSERT INTO runs VALUES ('two')")
    op.execute(\"\"\"DO $$ BEGIN
      IF (SELECT count(*) FROM tries) <= {failures} THEN
        RAISE EXCEPTION 'stand-in' USING ERRCODE = '{errcode}', DETAIL = '{detail}';
      END IF;
    END $$\"\"\")
"""
RETRIED = "migration retried after a deadlock"


@pytest.fixture
def scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[int, str], tuple[Config, Engine]]]:
    """Writes the two probe revisions (TWO failing as asked) and gives a Config that runs the real
    env.py with only them, on a database of this worker's own, created here and dropped after."""
    base = make_url(os.environ["ZIF_MIGRATE_DATABASE_URL"])
    name = f"{base.database}_deadlock"
    # It renders into DROP DATABASE as the superuser: a plain identifier only, and within 63 bytes
    # so Postgres can't truncate the suffix away and leave the name of the database the suite uses.
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise RuntimeError(f"database name {name!r} is not a plain identifier of 63 bytes or less")
    admin = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        conn.execute(text(f"CREATE DATABASE {name} OWNER ziftbook_migrate"))
    url = base.set(database=name).update_query_dict({"options": "-c statement_timeout=30s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", url.render_as_string(hide_password=False))
    engine = create_engine(url, poolclass=NullPool)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE runs (rev text)"))
        conn.execute(text("CREATE TABLE tries (lock_timeout text)"))

    def make(failures: int, errcode: str) -> tuple[Config, Engine]:
        (tmp_path / "one.py").write_text(ONE)
        two = TWO.format(failures=failures, errcode=errcode, detail=DETAIL)
        (tmp_path / "two.py").write_text(two)
        cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
        cfg.set_main_option("version_locations", str(tmp_path))
        cfg.set_main_option("path_separator", "os")
        return cfg, engine

    try:
        yield make
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


def state(engine: Engine) -> tuple[list[str], list[str], str | None]:
    with engine.connect() as conn:
        return (
            list(conn.scalars(text("SELECT rev FROM runs ORDER BY rev"))),
            list(conn.scalars(text("SELECT lock_timeout FROM tries"))),
            conn.scalar(text("SELECT version_num FROM alembic_version")),
        )


# ZIF-287 fence. Kills: no retry (a deploy fails on autovacuum's one-off deadlock), a retry that
# starts over (`one` applied twice) or keeps the failed try's work (`two` twice), a retry with the
# lock_timeout a failed autocommit block left at 0, and a retry with no log line or no DETAIL.
def test_a_deadlocked_migration_runs_once_more_from_where_it_stopped(
    scratch: Callable[[int, str], tuple[Config, Engine]],
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    cfg, engine = scratch(1, "deadlock_detected")
    command.upgrade(cfg, "head")
    assert state(engine) == (["one", "two"], ["5s", "5s"], "two")
    retried = [line for line in log_lines() if line["msg"] == RETRIED]
    assert [(line["level"], line["detail"]) for line in retried] == [("WARNING", DETAIL)]


# ZIF-287 fence. Kills: more than one retry, retrying any other error, and retrying towards a
# relative target, which would count from where the first try stopped and overshoot.
@pytest.mark.parametrize(
    ("failures", "errcode", "target", "tries", "error"),
    [
        (2, "deadlock_detected", "head", 2, DeadlockDetected),
        (1, "lock_not_available", "head", 1, LockNotAvailable),
        (1, "deadlock_detected", "+2", 1, DeadlockDetected),
    ],
    ids=["second-deadlock", "other-error", "relative-target"],
)
def test_a_migration_fails_as_before_when_one_retry_cannot_help(
    scratch: Callable[[int, str], tuple[Config, Engine]],
    log_lines: Callable[[], list[dict[str, Any]]],
    failures: int,
    errcode: str,
    target: str,
    tries: int,
    error: type[Exception],
) -> None:
    cfg, engine = scratch(failures, errcode)
    with pytest.raises(OperationalError) as caught:
        command.upgrade(cfg, target)
    assert isinstance(caught.value.orig, error), caught.value.orig
    assert state(engine) == (["one"], ["5s"] * tries, "one")
    assert len([line for line in log_lines() if line["msg"] == RETRIED]) == tries - 1


# Revisions after this one follow CONTRIBUTING's lock-before-grants rule; older ones predate it,
# except the two upgrades fixed in place (0013, 0057 and 0063's downgrades are ZIF-284's).
LOCK_RULE_FLOOR = "0094"
FIXED_IN_PLACE = ["0047", "0048"]
# Table-level only: a column grant (privileges with a column list) leaves pg_class alone.
GRANT = re.compile(
    r"\b(?:GRANT|REVOKE)\s+[A-Z, ]+?\s+ON\s+(?:TABLE\s+)?"
    r"(?!(?:FUNCTIONS?|SCHEMA|SEQUENCE|ALL|DATABASE|TYPE|DOMAIN|TABLES)\b)(\w+)",
    re.I,
)
# Statements whose lock conflicts with a vacuum's, CREATE TABLE included (ACCESS EXCLUSIVE on the
# new table until commit).
ALTER = re.compile(
    r"\b(?:(?:ALTER|CREATE)\s+TABLE(?:\s+IF\s+(?:NOT\s+)?EXISTS)?(?:\s+ONLY)?"
    r"|(?:CREATE|DROP|ALTER)\s+(?:TRIGGER|POLICY|RULE)\b[^;]*?\bON"
    r"|CREATE\s+(?:UNIQUE\s+)?INDEX\b(?!\s+CONCURRENTLY)[^;]*?\bON(?:\s+ONLY)?"
    r"|DROP\s+TABLE(?:\s+IF\s+EXISTS)?|TRUNCATE(?:\s+TABLE)?)\s+(\w+)",
    re.I,
)
LOCK = re.compile(r"\bLOCK\s+(?:TABLE\s+)?(\w+)\s+IN\s+ACCESS\s+EXCLUSIVE\s+MODE", re.I)
# op.* calls that alter a table, by the position of the table argument.
OP_ALTERS = {
    "create_table": 0,
    "add_column": 0,
    "drop_column": 0,
    "alter_column": 0,
    "create_index": 1,
}


def unlocked_grants(source: str) -> list[str]:
    """'<function>: <table>' for each upgrade/downgrade that changes a table's grants, then alters
    that table, with neither a LOCK TABLE ... IN ACCESS EXCLUSIVE MODE nor an alter of it first.

    shortcut: reads the function's own string literals and op.* calls only, so SQL in a
    module-level constant or a table name in an f-string placeholder goes unseen; widen it if a
    migration ever needs that.
    """
    found = []
    for fn in ast.parse(source).body:
        if not isinstance(fn, ast.FunctionDef) or fn.name not in {"upgrade", "downgrade"}:
            continue
        events: list[tuple[int, int, int, str, str]] = []  # line, column, offset, kind, table
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for kind, pattern in (("grant", GRANT), ("alter", ALTER), ("lock", LOCK)):
                    events += [
                        (node.lineno, node.col_offset, m.start(), kind, m.group(1).lower())
                        for m in pattern.finditer(node.value)
                    ]
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                at = OP_ALTERS.get(node.func.attr)
                arg = node.args[at] if at is not None and len(node.args) > at else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    events.append((node.lineno, node.col_offset, 0, "alter", arg.value.lower()))
        events.sort()
        for table in {t for _, _, _, kind, t in events if kind == "grant"}:
            kinds = [kind for *_, kind, t in events if t == table]
            first = kinds.index("grant")
            # An earlier alter already holds a lock no vacuum can start under, as LOCK TABLE does.
            if "alter" in kinds[first:] and not {"alter", "lock"} & set(kinds[:first]):
                found.append(f"{fn.name}: {table}")
    return sorted(found)


SCRIPTS = ScriptDirectory.from_config(Config(toml_file=str(API_DIR / "pyproject.toml")))
LOCK_RULE_REVISIONS = [
    rev.revision
    for rev in SCRIPTS.iterate_revisions("heads", LOCK_RULE_FLOOR)
    if rev.revision != LOCK_RULE_FLOOR
] + FIXED_IN_PLACE


# ZIF-287 fence. Kills: a new migration (or 0047/0048 again) that changes a table's grants and then
# alters it without locking it first, which autovacuum can deadlock in a deploy.
@pytest.mark.parametrize("revision", LOCK_RULE_REVISIONS)
def test_a_migration_locks_a_table_before_changing_its_grants_and_altering_it(
    revision: str,
) -> None:
    script = SCRIPTS.get_revision(revision)
    assert script is not None
    assert unlocked_grants(Path(script.path).read_text()) == []


# The check itself. Kills: a scanner that misses the shape (passing everything), counts column
# grants, or ignores a lock or an earlier alter.
def test_the_lock_rule_finds_grants_then_alters_without_a_lock() -> None:
    def migration(*statements: str) -> str:
        body = "".join(f"    op.execute({s!r})\n" for s in statements)
        return f"def upgrade() -> None:\n{body}\ndef other() -> None:\n    pass\n"

    grant, alter = "REVOKE UPDATE ON users FROM a", "ALTER TABLE users DROP COLUMN phone"
    assert unlocked_grants(migration(grant, alter)) == ["upgrade: users"]
    assert unlocked_grants(migration("DROP POLICY p ON users", grant)) == []
    assert unlocked_grants(migration(grant, "CREATE TRIGGER t BEFORE UPDATE ON users")) == [
        "upgrade: users"
    ]
    assert (
        unlocked_grants(migration("LOCK TABLE users IN ACCESS EXCLUSIVE MODE", grant, alter)) == []
    )
    assert unlocked_grants(migration(alter, grant)) == []
    assert unlocked_grants(migration("CREATE TABLE users (id int)", grant, alter)) == []
    assert unlocked_grants(migration("GRANT UPDATE (name) ON users TO a", alter)) == []
    assert unlocked_grants(migration(grant, "ALTER TABLE tenants ADD COLUMN x int")) == []
    assert unlocked_grants(migration("REVOKE EXECUTE ON FUNCTION f() FROM PUBLIC", alter)) == []
    with_op = (
        f"def downgrade() -> None:\n    op.execute({grant!r})\n    op.drop_column('users', 'x')\n"
    )
    assert unlocked_grants(with_op) == ["downgrade: users"]
