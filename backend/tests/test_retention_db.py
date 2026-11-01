"""The retention sweep's database side (ZIF-235): the policies that let it delete audit events past
their retention, and the worker-only functions that do it.

Rows are seeded and backdated as the superuser (it bypasses row-level security; created_at is
never the app's to set). The xdist database keeps other tests' rows, so every assertion is on the
seeded rows (their `target` marker or id), never on a total."""

import re
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, create_engine, make_url, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool

from tests.conftest import ADMIN_DATABASE_URL

NIL = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def admin(migrate_engine: Engine) -> Iterator[Engine]:
    """The superuser on this xdist worker's database: seeds and backdates past row-level
    security."""
    engine = create_engine(
        make_url(ADMIN_DATABASE_URL).set(database=migrate_engine.url.database), poolclass=NullPool
    )
    yield engine
    engine.dispose()


@pytest.fixture
def marker(admin: Engine) -> Iterator[str]:
    """A target prefix for this test's audit rows; they go afterwards, whatever the test did."""
    prefix = f"test-retention:{uuid.uuid4()}"
    yield prefix
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM audit_events WHERE target LIKE :p"), {"p": f"{prefix}%"})
        conn.execute(
            text("DELETE FROM platform_audit_events WHERE target LIKE :p"), {"p": f"{prefix}%"}
        )


def seed_audit(
    admin: Engine,
    target: str,
    action: str,
    age: str,
    tenant_id: uuid.UUID | None = None,
) -> None:
    """age: a Postgres interval, so a boundary is the cutoff's own ('1 year 1 hour')."""
    with admin.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO audit_events (tenant_id, action, target, ip, user_agent, created_at)
            VALUES (:t, :a, :target, '192.0.2.1', 'Agent', now() - CAST(:age AS interval))
            """),
            {"t": tenant_id, "a": action, "target": target, "age": age},
        )


def audit_targets(admin: Engine, prefix: str) -> set[str]:
    with admin.connect() as conn:
        return set(
            conn.scalars(
                text("SELECT target FROM audit_events WHERE target LIKE :p"), {"p": f"{prefix}%"}
            )
        )


# The policies that open audit_events to ziftbook_migrate while app.retention_sweep is on, as
# Postgres deparses them. Nothing else: never bookings or booking_events, whose old rows would
# then be readable across businesses by any migrate-owned definer a caller set the flag for
# (review B1, ZIF-235); the sweep clears those under tenant_isolation instead.
AUDIT_EXPIRED = (
    "((current_setting('app.retention_sweep'::text, true) = 'on'::text) AND"
    " ((created_at < (now() - '1 year'::interval)) OR ((action = 'sign_in_failed'::text) AND"
    " (created_at < (now() - '30 days'::interval)))))"
)
RETENTION_POLICIES = [
    ("audit_events", "retention_sweep", "DELETE", "{ziftbook_migrate}", AUDIT_EXPIRED, None),
    ("audit_events", "retention_sweep_read", "SELECT", "{ziftbook_migrate}", AUDIT_EXPIRED, None),
]


# 13: FENCE. Wrong impl: a retention policy without the cutoff, TO public, or on another table
# (bookings or booking_events: a cross-business read for every migrate-owned definer).
def test_the_retention_policies_are_pinned(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        rows = conn.execute(
            text("""
            SELECT tablename, policyname, cmd, roles::text, qual, with_check FROM pg_policies
            WHERE schemaname = 'public'
              AND (qual ~ 'app\\.retention_sweep' OR with_check ~ 'app\\.retention_sweep')
            ORDER BY 1, 2
            """)
        ).all()
    assert [tuple(row) for row in rows] == RETENTION_POLICIES


# An hour either side of each cutoff.
IN_WINDOW = [("sign_in_failed", "30 days -1 hour"), ("signed_out", "1 year -1 hour")]
PAST = [("sign_in_failed", "30 days 1 hour"), ("signed_out", "1 year 1 hour")]


def delete_as_migrate(migrate_engine: Engine, prefix: str, *, flag: bool, tenant: str) -> set[str]:
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        if flag:
            conn.execute(text("SELECT set_config('app.retention_sweep', 'on', true)"))
        return set(
            conn.scalars(
                text("DELETE FROM audit_events WHERE target LIKE :p RETURNING target"),
                {"p": f"{prefix}%"},
            )
        )


# 11: FENCE. Wrong impl: a DELETE policy without the cutoff (the owner, flag on, could then delete
# events still inside their window, whatever the function body does), or one not gated on the
# flag (any migrate session, the admin CLI's included, could delete events).
def test_the_owner_deletes_only_events_past_their_window_and_only_with_the_flag(
    admin: Engine, migrate_engine: Engine, marker: str
) -> None:
    tenant = uuid.uuid4()
    for i, (action, age) in enumerate(IN_WINDOW + PAST):
        seed_audit(admin, f"{marker}:{i}", action, age, tenant if i % 2 else None)

    assert delete_as_migrate(migrate_engine, marker, flag=False, tenant=NIL) == set()
    gone = delete_as_migrate(migrate_engine, marker, flag=True, tenant=NIL)

    assert gone == {f"{marker}:2", f"{marker}:3"}
    assert audit_targets(admin, marker) == {f"{marker}:0", f"{marker}:1"}


# 12: GUARD. The flag gives the app role nothing: it has no DELETE or UPDATE on audit_events, and
# the policies are TO ziftbook_migrate.
@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM audit_events WHERE target LIKE :p",
        "UPDATE audit_events SET ip = NULL WHERE target LIKE :p",
    ],
)
def test_the_retention_flag_gives_the_app_nothing(
    app_engine: Engine, admin: Engine, marker: str, statement: str
) -> None:
    seed_audit(admin, f"{marker}:old", "signed_out", "400 days")
    with pytest.raises(ProgrammingError), app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.retention_sweep', 'on', true)"))
        conn.execute(text(statement), {"p": f"{marker}%"})
    assert audit_targets(admin, marker) == {f"{marker}:old"}


# Every function owned by ziftbook_migrate, with whether it is a definer and returns a trigger.
FUNCTIONS = text("""
SELECT p.proname, p.prosecdef, p.prorettype = 'trigger'::regtype AS is_trigger, p.prosrc
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE pg_get_userbyid(p.proowner) = 'ziftbook_migrate' AND n.nspname = 'public'
ORDER BY 1
""")
CLEARS_FLAG = "set_config('app.retention_sweep', '', true)"
# Definers that read audit_events without clearing the flag first, each with why that is safe.
FLAG_ALLOWED: dict[str, str] = {}


def code(source: str) -> str:
    """A function body without its comments: a name in a comment reads nothing."""
    return re.sub(r"--[^\n]*|/\*.*?\*/", "", source, flags=re.DOTALL)


def reads_audit_events(source: str) -> int | None:
    """Where the body first names audit_events, or None."""
    found = re.search(r"\baudit_events\b", code(source))
    return found.start() if found else None


def flag_unhandled(conn: Connection) -> set[str]:
    """Migrate-owned definers that name audit_events before clearing app.retention_sweep."""
    unhandled = set()
    for name, definer, _, source in conn.execute(FUNCTIONS):
        first = reads_audit_events(source)
        if not definer or first is None:
            continue
        cleared = code(source).find(CLEARS_FLAG)
        if cleared == -1 or cleared > first:
            unhandled.add(name)
    return unhandled


def invoker_readers(conn: Connection) -> set[str]:
    """Migrate-owned SECURITY INVOKER functions, triggers aside, that name audit_events."""
    return {
        name
        for name, definer, is_trigger, source in conn.execute(FUNCTIONS)
        if not definer and not is_trigger and reads_audit_events(source) is not None
    }


# 14: FENCE (review #6). Wrong impl: a migrate-owned definer that reads audit_events without
# clearing app.retention_sweep first; a caller who set it would read every event past retention,
# of every business. Mentioning the flag is not enough: it must be cleared before the first read.
def test_every_definer_reading_audit_events_clears_the_retention_flag_first(
    migrate_engine: Engine,
) -> None:
    with migrate_engine.connect() as conn:
        assert flag_unhandled(conn) == set(FLAG_ALLOWED)


LEAKY = {
    "never": "SELECT count(*) FROM audit_events",
    "after the read": (
        "SELECT count(*) FROM audit_events"
        " WHERE set_config('app.retention_sweep', '', true) IS NOT NULL"
    ),
}


@pytest.mark.parametrize("body", sorted(LEAKY))
def test_the_flag_check_catches_a_definer_that_forgets_it(
    migrate_engine: Engine, body: str
) -> None:
    with migrate_engine.connect() as conn, conn.begin() as transaction:
        conn.execute(
            text(f"""
            CREATE FUNCTION leaky_audit_reader() RETURNS bigint LANGUAGE sql SECURITY DEFINER
              SET search_path = pg_catalog, public, pg_temp
              AS $$ -- reads audit_events
              {LEAKY[body]} $$
            """)
        )
        found = flag_unhandled(conn)
        transaction.rollback()
    assert found == set(FLAG_ALLOWED) | {"leaky_audit_reader"}


# M6: FENCE (review #6). Wrong impl: a migrate-owned SECURITY INVOKER helper that reads
# audit_events; a definer calling it runs it as ziftbook_migrate and inherits a caller's flag.
def test_no_invoker_function_of_the_owner_reads_audit_events(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        assert invoker_readers(conn) == set()


def test_the_invoker_check_catches_one_and_ignores_comments(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn, conn.begin() as transaction:
        conn.execute(
            text("""
            CREATE FUNCTION invoker_audit_reader() RETURNS bigint LANGUAGE sql
              AS 'SELECT count(*) FROM audit_events'
            """)
        )
        conn.execute(
            text("""
            CREATE FUNCTION mentions_audit_events() RETURNS bigint LANGUAGE sql
              AS $$ SELECT 1::bigint -- not audit_events $$
            """)
        )
        found = invoker_readers(conn)
        transaction.rollback()
    assert found == {"invoker_audit_reader"}
