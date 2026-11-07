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
# Its autocommit_block completes, so it must not stop a retry of the next migration.
ONE = """
from alembic import op

revision = "one"
down_revision = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        pass
    op.execute("INSERT INTO runs VALUES ('one', current_setting('lock_timeout'))")
"""
# The stand-in: the first {failures} tries fail with {errcode}, inside an autocommit_block when
# {block} (which commits the INSERT before it). nextval counts tries: a sequence never rolls back.
TWO = """
import contextlib

from alembic import op

revision = "two"
down_revision = "one"


def upgrade() -> None:
    op.execute("INSERT INTO runs VALUES ('two', current_setting('lock_timeout'))")
    with op.get_context().autocommit_block() if {block} else contextlib.nullcontext():
        op.execute(\"\"\"DO $$ BEGIN
          IF nextval('tries') <= {failures} THEN
            RAISE EXCEPTION 'stand-in' USING ERRCODE = '{errcode}', DETAIL = '{detail}';
          END IF;
        END $$\"\"\")
"""
RETRIED = "migration retried after a deadlock"
NOT_RETRIED = "migration not retried after a deadlock"
Scratch = Callable[[int, str, bool], tuple[Config, Engine]]


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Scratch]:
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
        conn.execute(text("CREATE TABLE runs (rev text, lock_timeout text)"))
        conn.execute(text("CREATE SEQUENCE tries"))

    def make(failures: int, errcode: str, block: bool) -> tuple[Config, Engine]:
        (tmp_path / "one.py").write_text(ONE)
        two = TWO.format(failures=failures, errcode=errcode, detail=DETAIL, block=block)
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


def state(engine: Engine) -> tuple[list[tuple[str, str]], int, str | None]:
    """Committed runs (revision, the lock_timeout it saw), tries of TWO, and the version."""
    with engine.connect() as conn:
        return (
            [tuple(row) for row in conn.execute(text("SELECT * FROM runs ORDER BY rev"))],
            conn.scalar(text("SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM tries")),
            conn.scalar(text("SELECT version_num FROM alembic_version")),
        )


def logged(log_lines: Callable[[], list[dict[str, Any]]], msg: str) -> list[tuple[str, str]]:
    return [(line["level"], line["detail"]) for line in log_lines() if line["msg"] == msg]


# ZIF-287 fence. Kills: no retry (a deploy fails on autovacuum's one-off deadlock), a retry that
# starts over (`one` applied twice) or keeps the failed try's work (`two` twice), a completed
# autocommit_block in an earlier migration blocking the retry, a retry without this run's
# lock_timeout (RESET ALL alone leaves 0), and a retry with no log line or no DETAIL.
def test_a_deadlocked_migration_runs_once_more_from_where_it_stopped(
    scratch: Scratch, log_lines: Callable[[], list[dict[str, Any]]]
) -> None:
    cfg, engine = scratch(1, "deadlock_detected", False)
    command.upgrade(cfg, "head")
    assert state(engine) == ([("one", "5s"), ("two", "5s")], 2, "two")
    assert logged(log_lines, RETRIED) == [("WARNING", DETAIL)]
    assert logged(log_lines, NOT_RETRIED) == []


# ZIF-287 fence. Kills: more than one retry, a second failure chained to the first (two
# tracebacks), retrying any other error, retrying towards a relative target (it would count from
# where the first try stopped and overshoot), and retrying a migration whose autocommit_block
# already committed part of it, or doing so without saying why.
@pytest.mark.parametrize(
    ("failures", "errcode", "block", "target", "error", "after", "retried", "not_retried"),
    [
        (2, "deadlock_detected", False, "head", DeadlockDetected, (["one"], 2), 1, 0),
        (1, "lock_not_available", False, "head", LockNotAvailable, (["one"], 1), 0, 0),
        (1, "deadlock_detected", False, "+2", DeadlockDetected, (["one"], 1), 0, 0),
        (1, "deadlock_detected", True, "head", DeadlockDetected, (["one", "two"], 1), 0, 1),
    ],
    ids=["second-deadlock", "other-error", "relative-target", "autocommit-block"],
)
def test_a_migration_fails_as_before_when_one_retry_cannot_help(
    scratch: Scratch,
    log_lines: Callable[[], list[dict[str, Any]]],
    failures: int,
    errcode: str,
    block: bool,
    target: str,
    error: type[Exception],
    after: tuple[list[str], int],
    retried: int,
    not_retried: int,
) -> None:
    cfg, engine = scratch(failures, errcode, block)
    with pytest.raises(OperationalError) as caught:
        command.upgrade(cfg, target)
    assert isinstance(caught.value.orig, error), caught.value.orig
    chain: list[BaseException] = []
    link: BaseException | None = caught.value
    while link is not None:
        chain.append(link)
        link = link.__context__
    assert len([e for e in chain if isinstance(e, OperationalError)]) == 1, chain
    runs, tries, version = state(engine)
    assert ([rev for rev, _ in runs], tries, version) == (*after, "one")
    assert logged(log_lines, RETRIED) == [("WARNING", DETAIL)] * retried
    assert logged(log_lines, NOT_RETRIED) == [("WARNING", DETAIL)] * not_retried


# Revisions after this one follow CONTRIBUTING's lock-before-grants rule; older ones predate it,
# except the two upgrades fixed in place (0013, 0057 and 0063's downgrades are ZIF-284's).
LOCK_RULE_FLOOR = "0094"
FIXED_IN_PLACE = ["0047", "0048"]
ANY = "*"  # a statement that names no table: it counts for every table in the function
NAMES = r"\w+(?:\s*,\s*\w+)*"
# Table-level only: a column grant (privileges with a column list) leaves pg_class alone. ON ALL
# TABLES IN SCHEMA captures nothing, so it counts for every table.
GRANT = re.compile(
    r"\b(?:GRANT|REVOKE)\s+[A-Z,\s]+?\s+ON\s+(?:ALL\s+TABLES\s+IN\s+SCHEMA\b|(?:TABLE\s+)?"
    r"(?!(?:FUNCTIONS?|SCHEMA|SEQUENCES?|ALL|DATABASE|TYPE|DOMAIN|TABLES|LANGUAGE|PROCEDURE"
    rf"|ROUTINE|LARGE)\b)({NAMES}))",
    re.I,
)
# Statements whose lock conflicts with a vacuum's, CREATE TABLE included (ACCESS EXCLUSIVE on the
# new table until commit).
ALTER = re.compile(
    r"\b(?:(?:ALTER|CREATE)\s+TABLE(?:\s+IF\s+(?:NOT\s+)?EXISTS)?(?:\s+ONLY)?"
    r"|(?:CREATE(?:\s+OR\s+REPLACE)?(?:\s+CONSTRAINT)?|DROP|ALTER)\s+(?:TRIGGER|POLICY|RULE)\b"
    r"[^;]*?\bON"
    r"|CREATE\s+(?:UNIQUE\s+)?INDEX\b(?!\s+CONCURRENTLY)[^;]*?\bON(?:\s+ONLY)?"
    rf"|DROP\s+TABLE(?:\s+IF\s+EXISTS)?|TRUNCATE(?:\s+TABLE)?(?:\s+ONLY)?)\s+({NAMES})",
    re.I,
)
# DROP INDEX names the index, not its table.
DROP_INDEX = re.compile(r"\bDROP\s+INDEX\b(?!\s+CONCURRENTLY)", re.I)
# SHARE UPDATE EXCLUSIVE (vacuum's own mode) or stronger; a LOCK with no mode is ACCESS EXCLUSIVE.
MODES = r"SHARE\s+UPDATE\s+EXCLUSIVE|SHARE\s+ROW\s+EXCLUSIVE|SHARE|EXCLUSIVE|ACCESS\s+EXCLUSIVE"
LOCK = re.compile(
    rf"\bLOCK\s+(?:TABLE\s+)?(?:ONLY\s+)?({NAMES})(?:\s+IN\s+(?:{MODES})\s+MODE|\s*(?:;|$))", re.I
)
# op.* calls that alter a table: (position, keyword) of each table argument.
OP_TABLES = {
    "create_table": [(0, "table_name")],
    "drop_table": [(0, "table_name")],
    "add_column": [(0, "table_name")],
    "drop_column": [(0, "table_name")],
    "alter_column": [(0, "table_name")],
    "create_index": [(1, "table_name")],
    "drop_index": [(1, "table_name")],
    "create_check_constraint": [(1, "table_name")],
    "create_unique_constraint": [(1, "table_name")],
    "create_primary_key": [(1, "table_name")],
    "drop_constraint": [(1, "table_name")],
    "create_foreign_key": [(1, "source_table"), (2, "referent_table")],
}


def unlocked_grants(source: str) -> list[str]:
    """'<function>: <table>' for each upgrade/downgrade that changes a table's grants, then alters
    that table, with neither a LOCK TABLE (SHARE UPDATE EXCLUSIVE or stronger) nor an alter of it
    first.

    Blind spots: it reads the function's own string literals and op.* calls only, so SQL in a
    module-level constant or a helper, and a table name in an f-string placeholder, go unseen. A
    DROP INDEX, or an op.* call whose table is not a literal, counts for every table.
    """
    found = []
    for fn in ast.parse(source).body:
        if not isinstance(fn, ast.FunctionDef) or fn.name not in {"upgrade", "downgrade"}:
            continue
        events: list[tuple[int, int, int, str, str]] = []  # line, column, offset, kind, table
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                patterns = (
                    ("grant", GRANT),
                    ("alter", ALTER),
                    ("alter", DROP_INDEX),
                    ("lock", LOCK),
                )
                for kind, pattern in patterns:
                    for m in pattern.finditer(node.value):
                        listed = m.group(1) if pattern.groups else None
                        tables = re.split(r"\s*,\s*", listed.lower()) if listed else [ANY]
                        events += [
                            (node.lineno, node.col_offset, m.start(), kind, t) for t in tables
                        ]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in OP_TABLES
            ):
                keywords = {k.arg: k.value for k in node.keywords}
                args = [
                    node.args[at] if len(node.args) > at else keywords.get(keyword)
                    for at, keyword in OP_TABLES[node.func.attr]
                ]
                tables = [
                    a.value.lower()
                    for a in args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                ]
                events += [(node.lineno, node.col_offset, 0, "alter", t) for t in tables or [ANY]]
        events.sort()
        for table in {t for *_, t in events} - {ANY}:
            seen = [(kind, t == table) for *_, kind, t in events if t in (table, ANY)]
            kinds = [kind for kind, _ in seen]
            if "grant" not in kinds:
                continue
            first = kinds.index("grant")
            # Only a lock or an alter of this very table, before the grant, keeps vacuum out.
            guarded = any(exact and kind != "grant" for kind, exact in seen[:first])
            if "alter" in kinds[first:] and not guarded:
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


GRANT_USERS = "REVOKE UPDATE ON users FROM a"
ALTER_USERS = "ALTER TABLE users DROP COLUMN phone"


# The check itself. Kills: a scanner that misses a shape of the hazard (passing everything), counts
# column grants, takes a weaker lock or another table's lock as a guard, or ignores an earlier
# alter.
@pytest.mark.parametrize(
    ("statements", "found"),
    [
        ([GRANT_USERS, ALTER_USERS], ["users"]),
        (["DROP POLICY p ON users", GRANT_USERS], []),
        ([GRANT_USERS, "CREATE TRIGGER t BEFORE UPDATE ON users"], ["users"]),
        ([GRANT_USERS, "CREATE OR REPLACE TRIGGER t BEFORE UPDATE ON users"], ["users"]),
        ([GRANT_USERS, "CREATE CONSTRAINT TRIGGER t AFTER INSERT ON users"], ["users"]),
        ([GRANT_USERS, "CREATE POLICY p ON users USING (true)"], ["users"]),
        ([GRANT_USERS, "CREATE INDEX ix ON users (email)"], ["users"]),
        ([GRANT_USERS, "DROP INDEX ix_users_email"], ["users"]),
        (["LOCK TABLE users IN ACCESS EXCLUSIVE MODE", GRANT_USERS, ALTER_USERS], []),
        (["LOCK TABLE users IN SHARE UPDATE EXCLUSIVE MODE", GRANT_USERS, ALTER_USERS], []),
        (["LOCK TABLE users", GRANT_USERS, ALTER_USERS], []),
        (["LOCK TABLE users IN ROW EXCLUSIVE MODE", GRANT_USERS, ALTER_USERS], ["users"]),
        (
            ["LOCK TABLE tenants IN SHARE UPDATE EXCLUSIVE MODE", GRANT_USERS, ALTER_USERS],
            ["users"],
        ),
        ([ALTER_USERS, GRANT_USERS], []),
        (["DROP INDEX ix", GRANT_USERS, ALTER_USERS], ["users"]),
        (["CREATE TABLE users (id int)", GRANT_USERS, ALTER_USERS], []),
        (["GRANT UPDATE (name) ON users TO a", ALTER_USERS], []),
        ([GRANT_USERS, "ALTER TABLE tenants ADD COLUMN x int"], []),
        (["REVOKE EXECUTE ON FUNCTION f() FROM PUBLIC", ALTER_USERS], []),
        (["REVOKE UPDATE ON tenants, users FROM a", ALTER_USERS], ["users"]),
        (["GRANT SELECT ON ALL TABLES IN SCHEMA public TO a", ALTER_USERS], ["users"]),
        (["GRANT SELECT,\n  INSERT ON users TO a", ALTER_USERS], ["users"]),
        (
            [GRANT_USERS, "op.create_foreign_key('fk', 'users', 'tenants', ['t'], ['id'])"],
            ["users"],
        ),
        (
            [
                "REVOKE UPDATE ON tenants FROM a",
                "op.create_foreign_key('f', 'u', 'tenants', [], [])",
            ],
            ["tenants"],
        ),
        ([GRANT_USERS, "op.drop_column(table_name='users', column_name='x')"], ["users"]),
        ([GRANT_USERS, "op.drop_table('users')"], ["users"]),
        ([GRANT_USERS, "op.drop_index('ix')"], ["users"]),
        ([GRANT_USERS, "op.create_index('ix', 'users', ['email'])"], ["users"]),
    ],
)
def test_the_lock_rule_finds_grants_then_alters_without_a_lock(
    statements: list[str], found: list[str]
) -> None:
    body = "".join(
        f"    {s}\n" if s.startswith("op.") else f"    op.execute({s!r})\n" for s in statements
    )
    source = f"def upgrade() -> None:\n{body}\n\ndef other() -> None:\n    pass\n"
    assert unlocked_grants(source) == [f"upgrade: {table}" for table in found]
