"""The retention sweep's database side (ZIF-235): the policies that let it delete audit events past
their retention, and the worker-only functions that do it.

Rows are seeded and backdated as the superuser (it bypasses row-level security; created_at is
never the app's to set). The xdist database keeps other tests' rows, so every assertion is on the
seeded rows (their `target` audit_marker or id), never on a total."""

import re
import secrets
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, get_args

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import ProgrammingError

from app import admin as platform
from app import auth, availability, bookings, limits
from app.db import tenant_context
from tests.conftest import People, add_user, fresh_email, member_id
from tests.test_bookings_db import book, seed_service
from tests.test_clients_db import seed_client

NIL = "00000000-0000-0000-0000-000000000000"


def seed_audit(
    superuser: Engine,
    target: str,
    action: str,
    age: str,
    tenant_id: uuid.UUID | None = None,
) -> None:
    """age: a Postgres interval, so a boundary is the cutoff's own ('1 year 1 hour')."""
    with superuser.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO audit_events (tenant_id, action, target, ip, user_agent, created_at)
            VALUES (:t, :a, :target, '192.0.2.1', 'Agent', now() - CAST(:age AS interval))
            """),
            {"t": tenant_id, "a": action, "target": target, "age": age},
        )


def audit_targets(superuser: Engine, prefix: str) -> set[str]:
    with superuser.connect() as conn:
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
    superuser: Engine, migrate_engine: Engine, audit_marker: str
) -> None:
    tenant = uuid.uuid4()
    for i, (action, age) in enumerate(IN_WINDOW + PAST):
        seed_audit(superuser, f"{audit_marker}:{i}", action, age, tenant if i % 2 else None)

    assert delete_as_migrate(migrate_engine, audit_marker, flag=False, tenant=NIL) == set()
    gone = delete_as_migrate(migrate_engine, audit_marker, flag=True, tenant=NIL)

    assert gone == {f"{audit_marker}:2", f"{audit_marker}:3"}
    assert audit_targets(superuser, audit_marker) == {f"{audit_marker}:0", f"{audit_marker}:1"}


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
    app_engine: Engine, superuser: Engine, audit_marker: str, statement: str
) -> None:
    seed_audit(superuser, f"{audit_marker}:old", "signed_out", "400 days")
    with pytest.raises(ProgrammingError), app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.retention_sweep', 'on', true)"))
        conn.execute(text(statement), {"p": f"{audit_marker}%"})
    assert audit_targets(superuser, audit_marker) == {f"{audit_marker}:old"}


# Every function owned by ziftbook_migrate, with whether it is a definer, and its body. The body
# from pg_get_functiondef, never prosrc: a SQL-standard body (BEGIN ATOMIC) leaves prosrc empty and
# lives in prosqlbody, so a fence on prosrc never sees it (build review, security #1).
FUNCTIONS = text("""
SELECT p.proname, p.prosecdef, pg_get_functiondef(p.oid)
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE pg_get_userbyid(p.proowner) = 'ziftbook_migrate' AND n.nspname = 'public'
  AND p.prokind IN ('f', 'p')
ORDER BY 1
""")
# As written in a plpgsql body, or as Postgres deparses a BEGIN ATOMIC one ('...'::text).
CLEARS_FLAG = re.compile(
    r"set_config\(\s*'app\.retention_sweep'(::text)?\s*,\s*''(::text)?\s*,\s*true\s*\)",
    re.IGNORECASE,
)
# Definers that read audit_events without clearing the flag first, each with why that is safe.
FLAG_ALLOWED: dict[str, str] = {
    # The sweep itself: it sets the flag to 'on' to reach the events past their retention, and
    # restores the caller's value before returning; the policies hold the cutoff.
    "sweep_audit_events": "sets and restores the flag",
}


def body(definition: str) -> str:
    """The body of a pg_get_functiondef: past the header, which names the function and its
    arguments, never a table."""
    if "$function$" in definition:
        return definition.split("$function$")[1]
    found = re.search(r"\bBEGIN\s+ATOMIC\b", definition, re.IGNORECASE)
    return definition[found.start() :] if found else definition


def code(source: str) -> str:
    """A function body without its comments: a name in a comment reads nothing."""
    return re.sub(r"--[^\n]*|/\*.*?\*/", "", body(source), flags=re.DOTALL)


def reads_audit_events(source: str) -> int | None:
    """Where the body first names audit_events, or None."""
    found = re.search(r"\baudit_events\b", code(source))
    return found.start() if found else None


def flag_unhandled(conn: Connection) -> set[str]:
    """Migrate-owned definers that name audit_events before clearing app.retention_sweep."""
    unhandled = set()
    for name, definer, source in conn.execute(FUNCTIONS):
        first = reads_audit_events(source)
        if not definer or first is None:
            continue
        cleared = CLEARS_FLAG.search(code(source))
        if cleared is None or cleared.start() > first:
            unhandled.add(name)
    return unhandled


def invoker_readers(conn: Connection) -> set[str]:
    """Migrate-owned SECURITY INVOKER functions, trigger functions included, that name
    audit_events: a trigger runs as the role whose statement fired it, so one fired inside a
    migrate definer runs as ziftbook_migrate too (build review, security #5)."""
    return {
        name
        for name, definer, source in conn.execute(FUNCTIONS)
        if not definer and reads_audit_events(source) is not None
    }


# 14: FENCE (review #6). Wrong impl: a migrate-owned definer that reads audit_events without
# clearing app.retention_sweep first; a caller who set it would read every event past retention,
# of every business. Mentioning the flag is not enough: it must be cleared before the first read.
def test_every_definer_reading_audit_events_clears_the_retention_flag_first(
    migrate_engine: Engine,
) -> None:
    with migrate_engine.connect() as conn:
        assert flag_unhandled(conn) == set(FLAG_ALLOWED)


DEFINER = """
CREATE FUNCTION {name}() RETURNS bigint LANGUAGE sql SECURITY DEFINER
  SET search_path = pg_catalog, public, pg_temp
"""
LEAKY = {
    "never": "AS $$ -- reads audit_events\n SELECT count(*) FROM audit_events $$",
    "after the read": (
        "AS $$ SELECT count(*) FROM audit_events"
        " WHERE set_config('app.retention_sweep', '', true) IS NOT NULL $$"
    ),
    # A SQL-standard body: prosrc is empty, the body is in prosqlbody.
    "begin atomic": "BEGIN ATOMIC SELECT count(*) FROM audit_events; END",
}
SAFE = {
    "clears first": (
        "AS $$ SELECT count(*) FROM audit_events"
        " WHERE (SELECT set_config('app.retention_sweep', '', true)) IS NOT NULL $$"
    ),
}


@pytest.mark.parametrize("body", sorted(LEAKY))
def test_the_flag_check_catches_a_definer_that_forgets_it(
    migrate_engine: Engine, body: str
) -> None:
    with migrate_engine.connect() as conn, conn.begin() as transaction:
        conn.execute(text(DEFINER.format(name="leaky_audit_reader") + LEAKY[body]))
        found = flag_unhandled(conn)
        transaction.rollback()
    assert found == set(FLAG_ALLOWED) | {"leaky_audit_reader"}


# The positive control's other half: a body that clears the flag before naming the table passes,
# in a plpgsql body as in a deparsed BEGIN ATOMIC one.
@pytest.mark.parametrize(
    "definition",
    [
        "LANGUAGE plpgsql AS $$ BEGIN PERFORM set_config('app.retention_sweep', '', true);"
        " RETURN (SELECT count(*) FROM audit_events); END $$",
        "LANGUAGE sql BEGIN ATOMIC SELECT set_config('app.retention_sweep', '', true);"
        " SELECT count(*) FROM audit_events; END",
    ],
    ids=["plpgsql", "begin atomic"],
)
def test_the_flag_check_passes_a_definer_that_clears_it_first(
    migrate_engine: Engine, definition: str
) -> None:
    with migrate_engine.connect() as conn, conn.begin() as transaction:
        conn.execute(
            text(
                "CREATE FUNCTION careful_audit_reader() RETURNS bigint SECURITY DEFINER"
                " SET search_path = pg_catalog, public, pg_temp " + definition
            )
        )
        found = flag_unhandled(conn)
        transaction.rollback()
    assert found == set(FLAG_ALLOWED)


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
        conn.execute(
            text("""
            CREATE FUNCTION atomic_audit_reader() RETURNS bigint LANGUAGE sql
              BEGIN ATOMIC SELECT count(*) FROM audit_events; END
            """)
        )
        conn.execute(
            text("""
            CREATE FUNCTION trigger_audit_reader() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN PERFORM count(*) FROM audit_events; RETURN NEW; END $$
            """)
        )
        found = invoker_readers(conn)
        transaction.rollback()
    assert found == {"invoker_audit_reader", "atomic_audit_reader", "trigger_audit_reader"}


# ---- 0074 and 0096: the worker's retention functions ----

SWEEPERS = {
    "sweep_audit_events()": "SELECT * FROM sweep_audit_events()",
    "sweep_admin_sessions()": "SELECT * FROM sweep_admin_sessions()",
    "sweep_account_sessions()": "SELECT * FROM sweep_account_sessions()",
    "sweep_booking_origins(uuid)": "SELECT * FROM sweep_booking_origins(NULL)",
    "sweep_consent_origins(uuid)": "SELECT * FROM sweep_consent_origins(NULL)",
}


# 1: FENCE. Wrong impl: EXECUTE granted to ziftbook_app or left to PUBLIC; the API role could then
# delete audit evidence (past its window, but on its own schedule) or walk every business.
@pytest.mark.parametrize("signature", sorted(SWEEPERS))
def test_the_app_role_cannot_call_a_retention_function(
    app_engine: Engine, migrate_engine: Engine, signature: str
) -> None:
    with migrate_engine.connect() as conn:
        held = conn.execute(
            text("""
            SELECT has_function_privilege('ziftbook_app', CAST(:f AS regprocedure), 'EXECUTE'),
                   has_function_privilege('public', CAST(:f AS regprocedure), 'EXECUTE'),
                   has_function_privilege('ziftbook_worker', CAST(:f AS regprocedure), 'EXECUTE')
            """),
            {"f": signature},
        ).one()
    assert tuple(held) == (False, False, True)
    with pytest.raises(ProgrammingError) as error, app_engine.begin() as conn:
        conn.execute(text(SWEEPERS[signature]))
    assert getattr(error.value.orig, "sqlstate", None) == "42501"


SHAPE = text("""
SELECT p.proname, oidvectortypes(p.proargtypes), p.prosecdef,
       pg_get_userbyid(p.proowner), p.proconfig
FROM pg_proc p WHERE p.oid = CAST(:f AS regprocedure)
""")


# 2: FENCE. Wrong impl: SECURITY INVOKER (the worker has no DELETE on audit_events); a search_path
# a caller's schema could shadow; an argument that lets the caller choose a period.
@pytest.mark.parametrize(
    ("signature", "arguments"),
    [
        ("sweep_audit_events()", ""),
        ("sweep_admin_sessions()", ""),
        ("sweep_account_sessions()", ""),
        ("sweep_booking_origins(uuid)", "uuid"),
        ("sweep_consent_origins(uuid)", "uuid"),
    ],
)
def test_a_retention_function_is_a_pinned_definer_whose_caller_chooses_no_period(
    migrate_engine: Engine, signature: str, arguments: str
) -> None:
    with migrate_engine.connect() as conn:
        row = conn.execute(SHAPE, {"f": signature}).one()
    assert tuple(row)[1:] == (
        arguments,
        True,
        "ziftbook_migrate",
        ["search_path=pg_catalog, public, pg_temp"],
    )


AUDIT_COUNTS = ("failed_sign_ins", "older_events", "platform_failures", "platform_events")


def sweep_audit(worker_engine: Engine) -> dict[str, Any]:
    with worker_engine.begin() as conn:
        return dict(conn.execute(text("SELECT * FROM sweep_audit_events()")).one()._mapping)


def sweep_audit_until_done(worker_engine: Engine) -> None:
    """Batches of 1000: call until every count says the backlog is gone."""
    for _ in range(100):
        swept = sweep_audit(worker_engine)
        assert swept["busy"] is False
        if all(swept[name] < 1000 for name in AUDIT_COUNTS):
            return
    raise AssertionError("the audit backlog never drained")


FAILED = [action for action in get_args(auth.Action) if action.endswith("_failed")]


# 3: FENCE. Wrong impl: a failure action kept a year (a `*_failed` action added to auth.Action
# later is covered here, R13), or a cutoff other than 30 days. 15: GUARD: a second sweep changes
# nothing.
@pytest.mark.parametrize("action", FAILED)
def test_a_failed_sign_in_goes_after_30_days_and_stays_before(
    superuser: Engine, worker_engine: Engine, audit_marker: str, action: str
) -> None:
    seed_audit(superuser, f"{audit_marker}:kept", action, "30 days -1 hour")
    seed_audit(superuser, f"{audit_marker}:gone", action, "30 days 1 hour")

    sweep_audit_until_done(worker_engine)
    assert audit_targets(superuser, audit_marker) == {f"{audit_marker}:kept"}
    sweep_audit_until_done(worker_engine)
    assert audit_targets(superuser, audit_marker) == {f"{audit_marker}:kept"}


# 4: FENCE. Wrong impl: a cutoff other than a year, or a sweep limited to one business (or to
# events of a business, leaving the no-business ones).
def test_other_audit_events_go_after_a_year_whatever_their_business(
    superuser: Engine, worker_engine: Engine, audit_marker: str
) -> None:
    for tenant in (uuid.uuid4(), uuid.uuid4(), None):
        seed_audit(
            superuser, f"{audit_marker}:{tenant}:kept", "signed_out", "1 year -1 hour", tenant
        )
        seed_audit(
            superuser, f"{audit_marker}:{tenant}:gone", "signed_out", "1 year 1 hour", tenant
        )

    sweep_audit_until_done(worker_engine)

    assert {t.rsplit(":", 1)[1] for t in audit_targets(superuser, audit_marker)} == {"kept"}
    assert len(audit_targets(superuser, audit_marker)) == 3


SETTINGS = text("""
SELECT current_setting('app.tenant_id', true), current_setting('app.audit_review', true),
       current_setting('app.retention_sweep', true)
""")


def set_settings(conn: Connection, tenant: uuid.UUID) -> None:
    conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
    conn.execute(text("SELECT set_config('app.audit_review', 'on', true)"))
    conn.execute(text("SELECT set_config('app.retention_sweep', 'caller', true)"))


# 5: FENCE. Wrong impl: the caller's settings not restored (a later statement in its transaction
# would run with the flag on, or another business), or a sweep that follows the caller's
# app.tenant_id or app.audit_review instead of its own.
def test_the_sweep_reaches_every_business_and_restores_the_callers_settings(
    superuser: Engine, worker_engine: Engine, audit_marker: str
) -> None:
    mine, other = uuid.uuid4(), uuid.uuid4()
    seed_audit(superuser, f"{audit_marker}:mine", "signed_out", "1 year 1 hour", mine)
    seed_audit(superuser, f"{audit_marker}:other", "signed_out", "1 year 1 hour", other)
    seed_audit(superuser, f"{audit_marker}:none", "sign_in_failed", "30 days 1 hour")
    seed_audit(superuser, f"{audit_marker}:recent", "signed_out", "1 day", other)

    with worker_engine.begin() as conn:
        set_settings(conn, mine)
        conn.execute(text("SELECT * FROM sweep_audit_events()")).one()
        after = tuple(conn.execute(SETTINGS).one())

    assert after == (str(mine), "on", "caller")
    assert audit_targets(superuser, audit_marker) == {f"{audit_marker}:recent"}


def seed_platform(superuser: Engine, target: str, action: platform.Action, age: str) -> None:
    with superuser.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO platform_audit_events (action, kind, target, reason, ip, user_agent,
                                               created_at)
            VALUES (:a, :k, :target, 'test', '192.0.2.2', 'Agent', now() - CAST(:age AS interval))
            """),
            {"a": action, "k": platform.KIND[action], "target": target, "age": age},
        )


def platform_targets(superuser: Engine, prefix: str) -> set[str]:
    with superuser.connect() as conn:
        return set(
            conn.scalars(
                text("SELECT target FROM platform_audit_events WHERE target LIKE :p"),
                {"p": f"{prefix}%"},
            )
        )


PLATFORM_FAILED = [a for a in get_args(platform.Action) if a.endswith("_failed")]


# 6: FENCE (owner ruling D3). Wrong impl: platform events kept forever (no RLS there: the body is
# the only guard), a platform failure kept a year, or business_read cut at 30 days.
@pytest.mark.parametrize("action", [*PLATFORM_FAILED, "business_read", "admin_signed_in"])
def test_platform_events_follow_the_audit_rule(
    superuser: Engine, worker_engine: Engine, audit_marker: str, action: platform.Action
) -> None:
    period = "30 days" if action in PLATFORM_FAILED else "1 year"
    seed_platform(superuser, f"{audit_marker}:kept", action, f"{period} -1 hour")
    seed_platform(superuser, f"{audit_marker}:gone", action, f"{period} 1 hour")

    sweep_audit_until_done(worker_engine)

    assert platform_targets(superuser, audit_marker) == {f"{audit_marker}:kept"}


# 9: FENCE. Wrong impl: no LIMIT (one unbounded DELETE holding its locks), or a batch taken in no
# order (the oldest must go first). On seeded ids: the newest of 1001 is what is left.
def test_the_sweep_works_in_batches_of_1000_oldest_first(
    superuser: Engine, worker_engine: Engine, audit_marker: str
) -> None:
    with superuser.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO audit_events (action, target, created_at)
            SELECT 'sign_in_failed', :p || ':' || n, now() - interval '40 days' - n * interval '1s'
            FROM generate_series(0, 1000) AS n
            """),
            {"p": audit_marker},
        )

    first = sweep_audit(worker_engine)
    assert audit_targets(superuser, audit_marker) == {f"{audit_marker}:0"}
    assert first["failed_sign_ins"] == 1000
    sweep_audit(worker_engine)
    assert audit_targets(superuser, audit_marker) == set()


# 10: FENCE. Wrong impl: a blocking advisory lock (a second replica would queue behind the first),
# or settings changed on the way out of a busy call.
def test_a_second_sweeper_gets_busy_at_once_and_changes_nothing(
    superuser: Engine, worker_engine: Engine, migrate_engine: Engine, audit_marker: str
) -> None:
    seed_audit(superuser, f"{audit_marker}:old", "signed_out", "1 year 1 hour")
    tenant = uuid.uuid4()
    with migrate_engine.connect() as holder, holder.begin():
        holder.execute(text("SELECT pg_advisory_xact_lock(235, 0)"))
        with worker_engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '5s'"))
            set_settings(conn, tenant)
            swept = dict(conn.execute(text("SELECT * FROM sweep_audit_events()")).one()._mapping)
            after = tuple(conn.execute(SETTINGS).one())

    assert swept == {"busy": True, **dict.fromkeys(AUDIT_COUNTS, 0)}
    assert after == (str(tenant), "on", "caller")
    assert audit_targets(superuser, audit_marker) == {f"{audit_marker}:old"}


# 16: GUARD (reframed per review #10: a sum of counts cannot fail for a missing lock). Two sweeps
# started together both finish without an error or a deadlock, and the seeded rows are gone.
def test_two_sweeps_at_once_both_finish(
    superuser: Engine, worker_engine: Engine, audit_marker: str
) -> None:
    with superuser.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO audit_events (action, target, created_at)
            SELECT 'signed_out', :p || ':' || n, now() - interval '400 days'
            FROM generate_series(1, 50) AS n
            """),
            {"p": audit_marker},
        )
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            barrier.wait(10)
            for _ in range(5):  # busy while the other holds its batch: try again
                sweep_audit(worker_engine)
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    sweep_audit_until_done(worker_engine)  # one of the two may have been busy
    assert audit_targets(superuser, audit_marker) == set()


# ---- admin sessions ----


@pytest.fixture
def platform_admin(superuser: Engine, migrate_engine: Engine) -> Iterator[uuid.UUID]:
    user_id = add_user(migrate_engine)
    with superuser.begin() as conn:
        conn.execute(text("INSERT INTO platform_admins (user_id) VALUES (:u)"), {"u": user_id})
    yield user_id
    with superuser.begin() as conn:
        conn.execute(text("DELETE FROM platform_admins WHERE user_id = :u"), {"u": user_id})
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


def seed_admin_session(superuser: Engine, user_id: uuid.UUID, expires: str, seen: str) -> bytes:
    id_hash = secrets.token_bytes(32)
    with superuser.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO admin_sessions (id_hash, user_id, expires_at, last_seen_at, ip, user_agent)
            VALUES (:h, :u, now() + CAST(:e AS interval), now() - CAST(:s AS interval),
                    '192.0.2.3', 'Agent')
            """),
            {"h": id_hash, "u": user_id, "e": expires, "s": seen},
        )
    return id_hash


# 8: FENCE (review #3 margin). Wrong impl: a live session deleted; a cutoff with no margin (a
# reader whose transaction began before the sweep would still see the row as live: idle 30
# minutes, expired at now()); dead sessions kept with their address.
def test_admin_sessions_go_an_hour_after_they_die_and_a_live_one_still_resolves(
    superuser: Engine, app_engine: Engine, worker_engine: Engine, platform_admin: uuid.UUID
) -> None:
    sessions = {
        "live": seed_admin_session(superuser, platform_admin, "1 hour", "1 minute"),
        "expired_in_margin": seed_admin_session(
            superuser, platform_admin, "-59 minutes", "1 minute"
        ),
        "expired": seed_admin_session(superuser, platform_admin, "-61 minutes", "1 minute"),
        "idle_in_margin": seed_admin_session(superuser, platform_admin, "1 hour", "89 minutes"),
        "idle": seed_admin_session(superuser, platform_admin, "1 hour", "91 minutes"),
    }

    with worker_engine.begin() as conn:
        deleted = conn.scalar(text("SELECT deleted FROM sweep_admin_sessions()"))

    with superuser.connect() as conn:
        left = set(
            conn.scalars(
                text("SELECT id_hash FROM admin_sessions WHERE user_id = :u"),
                {"u": platform_admin},
            )
        )
    assert left == {sessions[k] for k in ("live", "expired_in_margin", "idle_in_margin")}
    assert deleted is not None and deleted >= 2
    with app_engine.begin() as conn:
        resolved = conn.execute(
            text("SELECT user_id FROM resolve_admin_session(:h)"), {"h": sessions["live"]}
        ).all()
    assert [row.user_id for row in resolved] == [platform_admin]


def seed_account_session(superuser: Engine, user_id: uuid.UUID, expires: str, seen: str) -> bytes:
    id_hash = secrets.token_bytes(32)
    with superuser.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO account_sessions (id_hash, user_id, expires_at, last_seen_at, ip,
                                          user_agent)
            VALUES (:h, :u, now() + CAST(:e AS interval), now() - CAST(:s AS interval),
                    '192.0.2.4', 'Agent')
            """),
            {"h": id_hash, "u": user_id, "e": expires, "s": seen},
        )
    return id_hash


def account_sessions_of(superuser: Engine, user_id: uuid.UUID) -> set[bytes]:
    with superuser.connect() as conn:
        return set(
            conn.scalars(
                text("SELECT id_hash FROM account_sessions WHERE user_id = :u"), {"u": user_id}
            )
        )


# ZIF-146 x ZIF-235 FENCE (T17's account_sessions rule). Wrong impl: a live account session
# deleted; a cutoff with no margin (a reader whose transaction began before the sweep would still
# see the row as live: idle 7 days, expired at now()); dead sessions kept with their address.
def test_account_sessions_go_an_hour_after_they_die_and_a_live_one_still_resolves(
    superuser: Engine, app_engine: Engine, worker_engine: Engine, migrate_engine: Engine
) -> None:
    user_id = add_user(migrate_engine)
    sessions = {
        "live": seed_account_session(superuser, user_id, "1 hour", "1 minute"),
        "expired_in_margin": seed_account_session(superuser, user_id, "-59 minutes", "1 minute"),
        "expired": seed_account_session(superuser, user_id, "-61 minutes", "1 minute"),
        "idle_in_margin": seed_account_session(superuser, user_id, "1 hour", "7 days 59 minutes"),
        "idle": seed_account_session(superuser, user_id, "1 hour", "7 days 61 minutes"),
    }

    with worker_engine.begin() as conn:
        deleted = conn.scalar(text("SELECT deleted FROM sweep_account_sessions()"))

    left = account_sessions_of(superuser, user_id)
    assert left == {sessions[k] for k in ("live", "expired_in_margin", "idle_in_margin")}
    assert deleted is not None and deleted >= 2
    with app_engine.begin() as conn:
        resolved = conn.execute(
            text("SELECT user_id FROM resolve_account_session(:h)"), {"h": sessions["live"]}
        ).all()
    assert [row.user_id for row in resolved] == [user_id]


# ZIF-146 x ZIF-235 FENCE (M7, the held account session). Wrong impl: no SKIP LOCKED, so the sweep
# waits on (or deadlocks with) another writer holding a dead session's row: start_account_session's
# own purge, a sign-out, sign-out everywhere, a password reset, an erasure or the admin CLI's grant.
# (A booking's FOR KEY SHARE holds only a live one, which the sweep never selects; any conflicting
# lock stands in for them here.) The held row is skipped, never waited on, and goes on a later run.
def test_the_account_session_sweep_skips_a_session_another_transaction_holds(
    superuser: Engine, worker_engine: Engine, migrate_engine: Engine
) -> None:
    user_id = add_user(migrate_engine)
    held = seed_account_session(superuser, user_id, "-2 hours", "1 minute")
    seed_account_session(superuser, user_id, "-2 hours", "1 minute")  # dead and free: deleted
    with superuser.connect() as holder, holder.begin():
        holder.execute(
            text("SELECT FROM account_sessions WHERE id_hash = :h FOR KEY SHARE"), {"h": held}
        )
        with worker_engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '2s'"))  # a wait fails instead of hanging
            conn.execute(text("SELECT deleted FROM sweep_account_sessions()"))
        assert account_sessions_of(superuser, user_id) == {held}
    with worker_engine.begin() as conn:
        conn.execute(text("SELECT deleted FROM sweep_account_sessions()"))
    assert account_sessions_of(superuser, user_id) == set()


# ---- booking origins ----

ORIGIN = "18 months"


def at(clock: Engine, age: str) -> datetime:
    """now() minus a Postgres interval, as the database computes it ('18 months 1 hour')."""
    with clock.connect() as conn:
        value: datetime = conn.scalar(text("SELECT now() - CAST(:a AS interval)"), {"a": age})
    return value


def seed_booking(
    clock: Engine, tenant_id: uuid.UUID, user_id: uuid.UUID, ends: str, *, source: str = "merchant"
) -> uuid.UUID:
    """A confirmed booking that ended at now() minus `ends`."""
    ends_at = at(clock, ends)
    return book(
        tenant_id,
        client_id=seed_client(tenant_id, email=fresh_email()),
        worker_id=member_id(tenant_id, user_id),
        service_id=seed_service(tenant_id),
        starts_at=ends_at - timedelta(minutes=30),
        ends_at=ends_at,
        source=source,
    )


def seed_event(
    superuser: Engine, tenant_id: uuid.UUID, booking_id: uuid.UUID, age: str, event: str = "created"
) -> uuid.UUID:
    with tenant_context(tenant_id) as db:
        event_id: uuid.UUID = db.scalar(
            text("""
            INSERT INTO booking_events (tenant_id, booking_id, event, ip, user_agent,
                                        policy_version, consent_purposes, details)
            VALUES (current_setting('app.tenant_id')::uuid, :b, :e, '192.0.2.10', 'Agent',
                    'v1', '{"marketing_email": true}', '{"note": "kept"}')
            RETURNING id
            """),
            {"b": booking_id, "e": event},
        )
    with superuser.begin() as conn:
        conn.execute(
            text(
                "UPDATE booking_events SET created_at = now() - CAST(:a AS interval) WHERE id = :i"
            ),
            {"a": age, "i": event_id},
        )
    return event_id


ROWS = text("""
SELECT id, host(ip) AS ip, user_agent, to_jsonb(e) - 'ip' - 'user_agent' AS rest
FROM booking_events e WHERE id = ANY(:ids)
""")


def event_rows(superuser: Engine, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
    with superuser.connect() as conn:
        return {row.id: row for row in conn.execute(ROWS, {"ids": ids})}


def origins_pass(worker_engine: Engine, function: str = "sweep_booking_origins") -> int:
    """One full walk over the businesses: calls until the cursor comes back empty."""
    cleared, start = 0, None
    for _ in range(1000):
        with worker_engine.begin() as conn:
            row = conn.execute(text(f"SELECT * FROM {function}(:f)"), {"f": start}).one()
        cleared += int(row.cleared)
        start = row.next_tenant
        if start is None:
            return cleared
    raise AssertionError("the walk never ended")


# 7: FENCE (review B2: the body is the only guard here). Wrong impl: an anchor on the event alone
# (an old event of a booking that ended recently, or is still to come, would lose the IP the ZB-19
# cap reads), on the booking alone, a cutoff other than 18 months, a sweep of one business only,
# or a statement that touches any other column.
def test_booking_origins_clear_only_when_event_and_appointment_are_both_past_18_months(
    people: People, superuser: Engine, worker_engine: Engine
) -> None:
    cleared, kept = [], []
    for tenant, user in ((people.a, people.both), (people.b, people.only_b)):
        old = seed_booking(superuser, tenant, user, f"{ORIGIN} 1 hour")
        recent = seed_booking(superuser, tenant, user, f"{ORIGIN} -1 hour")
        cleared.append(seed_event(superuser, tenant, old, f"{ORIGIN} 1 hour"))
        kept.append(seed_event(superuser, tenant, old, f"{ORIGIN} -1 hour"))
        kept.append(seed_event(superuser, tenant, recent, f"{ORIGIN} 1 hour"))
        kept.append(seed_event(superuser, tenant, recent, f"{ORIGIN} -1 hour"))
    before = event_rows(superuser, cleared + kept)

    origins_pass(worker_engine)

    after = event_rows(superuser, cleared + kept)
    assert {i for i in after if after[i].ip is None and after[i].user_agent is None} == set(cleared)
    assert all(after[i].ip == "192.0.2.10" and after[i].user_agent == "Agent" for i in kept)
    assert all(after[i].rest == before[i].rest for i in after)  # every other column unchanged


# M5 (review #5): R3 as it really is. An ended booking a merchant moves into the future (MOVE and
# reschedule check only its status) counts again in PENDING_COUNT, with its cleared address: it
# leaves the requester's own per-IP bucket and counts only toward the across-IP ceiling.
def test_a_revived_ended_booking_counts_only_toward_the_across_ip_ceiling(
    people: People, superuser: Engine, worker_engine: Engine
) -> None:
    booking_id = seed_booking(
        superuser, people.a, people.both, f"{ORIGIN} 1 hour", source="booking_page"
    )
    seed_event(superuser, people.a, booking_id, f"{ORIGIN} 1 hour")
    origins_pass(worker_engine)
    with superuser.begin() as conn:
        conn.execute(
            text("""
            UPDATE bookings SET starts_at = now() + interval '1 day',
                                ends_at = now() + interval '1 day 30 minutes'
            WHERE id = :b
            """),
            {"b": booking_id},
        )
    with tenant_context(people.a) as db:
        client_id, now = db.execute(
            text("SELECT client_id, now() FROM bookings WHERE id = :b"), {"b": booking_id}
        ).one()
        placed_from = db.scalars(
            bookings.PENDING_COUNT,
            {"client_id": client_id, "expiring": list(availability.EXPIRING), "now": now},
        ).all()

    assert placed_from == [None]
    assert limits.ip_key("booking", None) != limits.ip_key("booking", "192.0.2.10")


# M8 (review #1): the walk never waits on a booking event another transaction holds (an erasure,
# say): it skips it and clears the rest.
def test_the_origin_walk_skips_an_event_another_transaction_holds(
    people: People, superuser: Engine, worker_engine: Engine, migrate_engine: Engine
) -> None:
    booking_id = seed_booking(superuser, people.a, people.both, f"{ORIGIN} 2 hours")
    held = seed_event(superuser, people.a, booking_id, f"{ORIGIN} 2 hours")
    free = seed_event(superuser, people.a, booking_id, f"{ORIGIN} 2 hours")
    with migrate_engine.connect() as holder, holder.begin():
        holder.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        holder.execute(text("SELECT 1 FROM booking_events WHERE id = :i FOR UPDATE"), {"i": held})
        with worker_engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '5s'"))
            conn.execute(text("SELECT * FROM sweep_booking_origins(:t)"), {"t": people.a}).one()

    rows = event_rows(superuser, [held, free])
    assert rows[held].ip == "192.0.2.10" and rows[free].ip is None


# ---- consent origins (ZIF-156) ----


def seed_consent(
    superuser: Engine,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    purpose: str,
    age: str,
    *,
    granted: bool = True,
    source: str = "booking_page",
) -> uuid.UUID:
    """A consent given at now() minus `age`, with an address and browser (which 0096's trigger
    drops from a merchant row). Seed a client's rows oldest first: later is the higher id, as
    CURRENT_CONSENTS reads it."""
    with superuser.begin() as conn:
        consent_id: uuid.UUID = conn.scalar(
            text("""
            INSERT INTO consents (tenant_id, client_id, purpose, granted, text_shown,
                                  policy_version, source, ip, user_agent, created_at)
            VALUES (:t, :c, :p, :g, 'text', '2026-09-01', :s, '192.0.2.20', 'Agent',
                    now() - CAST(:a AS interval))
            RETURNING id
            """),
            {"t": tenant_id, "c": client_id, "p": purpose, "g": granted, "s": source, "a": age},
        )
    return consent_id


CONSENT_ROWS = text("""
SELECT id, host(ip) AS ip, user_agent, to_jsonb(c) - 'ip' - 'user_agent' AS rest
FROM consents c WHERE id = ANY(:ids)
""")


def consent_rows(superuser: Engine, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
    with superuser.connect() as conn:
        return {row.id: row for row in conn.execute(CONSENT_ROWS, {"ids": ids})}


def swept_consents(
    superuser: Engine, worker_engine: Engine, ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """One full pass of the consent sweep; the seeded rows it left with neither value, every other
    column unchanged and no row deleted."""
    before = consent_rows(superuser, ids)
    origins_pass(worker_engine, "sweep_consent_origins")
    after = consent_rows(superuser, ids)
    assert set(after) == set(before)  # no row deleted
    assert all(after[i].rest == before[i].rest for i in after)  # every other column unchanged
    return {i for i in after if after[i].ip is None and after[i].user_agent is None}


# ZIF-156 fence. Kills: an address kept past a year after the answer changed, cleared before the
# year, or cleared from the answer that stands (however old: owner ruling D4); a change looked for
# across clients or purposes; a sweep of one business only; or a statement that touches the
# wording, version, purpose, source or time (the Art. 7(1) evidence) or deletes a row.
def test_consent_origins_clear_a_year_after_the_answer_changes_and_never_while_it_stands(
    people: People, superuser: Engine, worker_engine: Engine
) -> None:
    cleared, kept = [], []
    for tenant in (people.a, people.b):
        first, second = seed_client(tenant), seed_client(tenant)
        # Granted, then withdrawn more than a year ago: the grant goes, the withdrawal stands.
        cleared.append(seed_consent(superuser, tenant, first, "marketing_email", "3 years"))
        kept.append(
            seed_consent(
                superuser, tenant, first, "marketing_email", "1 year 1 hour", granted=False
            )
        )
        # Withdrawn under a year ago: the grant keeps its proof until the year is up.
        kept.append(seed_consent(superuser, tenant, first, "sms", "3 years"))
        kept.append(seed_consent(superuser, tenant, first, "sms", "1 year -1 hour", granted=False))
        # Withdrawn, then granted again more than a year ago: the withdrawal goes.
        cleared.append(seed_consent(superuser, tenant, first, "whatsapp", "4 years", granted=False))
        kept.append(seed_consent(superuser, tenant, first, "whatsapp", "2 years"))
        # Another client, never changed, however old.
        kept.append(seed_consent(superuser, tenant, second, "marketing_email", "3 years"))

    gone = swept_consents(superuser, worker_engine, cleared + kept)

    assert gone == set(cleared)


# ZIF-156 fence. Kills: "replaced" read as any later row. The client's online grant keeps its proof
# when the receptionist records the same answer later (a merchant row, which holds no address), or
# when the client says yes again on a later booking: the answer has not changed.
def test_a_later_row_with_the_same_answer_keeps_the_proof(
    people: People, superuser: Engine, worker_engine: Engine
) -> None:
    client_id = seed_client(people.a)
    online = seed_consent(superuser, people.a, client_id, "marketing_email", "4 years")
    seed_consent(superuser, people.a, client_id, "marketing_email", "3 years", source="merchant")
    again = seed_consent(superuser, people.a, client_id, "marketing_email", "2 years")

    assert swept_consents(superuser, worker_engine, [online, again]) == set()


def backlog_events(superuser: Engine, people: People) -> list[uuid.UUID]:
    """1001 booking events past their period, in one business."""
    booking_id = seed_booking(superuser, people.a, people.both, f"{ORIGIN} 2 hours")
    with tenant_context(people.a) as db:
        ids = db.scalars(
            text("""
            INSERT INTO booking_events (tenant_id, booking_id, event, ip)
            SELECT current_setting('app.tenant_id')::uuid, :b, 'created', '192.0.2.11'
            FROM generate_series(1, 1001)
            RETURNING id
            """),
            {"b": booking_id},
        ).all()
    with superuser.begin() as conn:
        conn.execute(
            text(
                "UPDATE booking_events SET created_at = now() - interval '2 years'"
                " WHERE id = ANY(:i)"
            ),
            {"i": list(ids)},
        )
    return list(ids)


def backlog_consents(superuser: Engine, people: People) -> list[uuid.UUID]:
    """1001 grants withdrawn two years ago, for one client of one business."""
    client_id = seed_client(people.a)
    with superuser.begin() as conn:
        ids = conn.scalars(
            text("""
            INSERT INTO consents (tenant_id, client_id, purpose, granted, text_shown,
                                  policy_version, source, ip, created_at)
            SELECT :t, :c, 'marketing_email', true, 'text', '2026-09-01', 'booking_page',
                   '192.0.2.21', now() - interval '3 years'
            FROM generate_series(1, 1001)
            RETURNING id
            """),
            {"t": people.a, "c": client_id},
        ).all()
    seed_consent(superuser, people.a, client_id, "marketing_email", "2 years", granted=False)
    return list(ids)


# The walks' bounds (ZIF-156: both of them, one copied from the other).
BACKLOGS = {
    "sweep_booking_origins": ("booking_events", backlog_events),
    "sweep_consent_origins": ("consents", backlog_consents),
}


# The walk's bounds: at most 1000 rows per call, resuming at the business it stopped in.
@pytest.mark.parametrize("function", sorted(BACKLOGS))
def test_a_call_clears_at_most_1000_and_the_next_resumes_in_the_same_business(
    people: People, superuser: Engine, worker_engine: Engine, function: str
) -> None:
    table, backlog = BACKLOGS[function]
    ids = backlog(superuser, people)

    with worker_engine.begin() as conn:
        first = conn.execute(text(f"SELECT * FROM {function}(:t)"), {"t": people.a}).one()
    assert first.cleared == 1000 and first.next_tenant == people.a
    with worker_engine.begin() as conn:
        second = conn.execute(text(f"SELECT * FROM {function}(:t)"), {"t": people.a}).one()
    assert second.cleared >= 1
    with superuser.connect() as conn:
        assert (
            conn.scalar(
                text(f"SELECT count(*) FROM {table} WHERE id = ANY(:i) AND ip IS NOT NULL"),
                {"i": ids},
            )
            == 0
        )


# The walk's bounds: at most 200 businesses per call, so a call stays short however many there are.
@pytest.mark.parametrize("function", sorted(BACKLOGS))
def test_a_call_visits_at_most_200_businesses(
    migrate_engine: Engine, worker_engine: Engine, function: str
) -> None:
    with migrate_engine.begin() as conn:
        made = conn.scalars(
            text(
                "INSERT INTO tenants (name) SELECT 'walk' FROM generate_series(1, 201) RETURNING id"
            )
        ).all()
    try:
        with migrate_engine.connect() as conn:
            expected = conn.scalar(text("SELECT id FROM tenants ORDER BY id OFFSET 200 LIMIT 1"))
        with worker_engine.begin() as conn:
            row = conn.execute(text(f"SELECT * FROM {function}(NULL)")).one()
        assert row.next_tenant == expected
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = ANY(:i)"), {"i": list(made)})


# M1 (review B1, the reviewer's P1 as a regression test). A migrate-owned definer that reads
# bookings and booking_events for the caller's business, as ZIF-146's own_bookings or a ZIF-58
# erasure reader does, must return that business's rows only, even with app.retention_sweep on.
# The same test then adds the policies of the first design and shows it would have caught them.
PROBE = """
CREATE FUNCTION retention_probe() RETURNS SETOF uuid LANGUAGE sql SECURITY DEFINER
  SET search_path = pg_catalog, public, pg_temp
  AS 'SELECT b.tenant_id FROM bookings b UNION ALL SELECT e.tenant_id FROM booking_events e'
"""
OLD_DESIGN = [
    "CREATE POLICY retention_sweep ON bookings FOR SELECT TO ziftbook_migrate USING"
    " (current_setting('app.retention_sweep', true) = 'on'"
    " AND ends_at < now() - interval '18 months')",
    "CREATE POLICY retention_sweep_read ON booking_events FOR SELECT TO ziftbook_migrate USING"
    " (current_setting('app.retention_sweep', true) = 'on'"
    " AND created_at < now() - interval '18 months')",
]


def probe_as_app(conn: Connection, tenant: uuid.UUID) -> set[uuid.UUID]:
    conn.execute(text("SET LOCAL ROLE ziftbook_app"))
    conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
    conn.execute(text("SELECT set_config('app.retention_sweep', 'on', true)"))
    found = set(conn.scalars(text("SELECT * FROM retention_probe()")))
    conn.execute(text("RESET ROLE"))
    return found


def test_the_retention_flag_reads_no_other_business_through_a_definer(
    people: People, superuser: Engine
) -> None:
    for tenant, user in ((people.a, people.both), (people.b, people.only_b)):
        booking_id = seed_booking(superuser, tenant, user, "2 years")
        seed_event(superuser, tenant, booking_id, "2 years")
    with superuser.connect() as conn, conn.begin() as transaction:
        conn.execute(text("SET LOCAL ROLE ziftbook_migrate"))
        conn.execute(text(PROBE))
        conn.execute(text("GRANT EXECUTE ON FUNCTION retention_probe() TO ziftbook_app"))
        conn.execute(text("RESET ROLE"))
        as_built = probe_as_app(conn, people.a)
        conn.execute(text("SET LOCAL ROLE ziftbook_migrate"))
        for statement in OLD_DESIGN:
            conn.execute(text(statement))
        conn.execute(text("RESET ROLE"))
        first_design = probe_as_app(conn, people.a)
        transaction.rollback()

    assert as_built == {people.a}
    assert people.b in first_design  # the check would have caught the first design's leak


# ---- M2: what each function's body may write ----

# An allow-list (re-verify N2): the body is cut into statements (at `;` and after THEN, ELSE,
# LOOP, BEGIN and DECLARE, so nothing hides behind a condition), and each one must open with a
# plpgsql construct these functions use, or be one of the writes the function exists for. Anything
# else (DDL, COPY, GRANT, MERGE, TRUNCATE, INSERT, EXECUTE, CALL, a WITH that writes) is refused.
ALLOWED = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^$",
        r"^end(\s+(if|loop))?$",
        r"^(exit|return)$",
        r"^perform\s",
        r"^get\s+diagnostics\s+\w+\s*=\s*row_count$",
        r"^(if|elsif)\s",
        r"^for\s+\w+\s+in\s+select\s",
        r"^\w+\s*:=",  # an assignment
        r"^\w+\s+(text|uuid|integer|boolean)\b",  # a declaration
    )
]
# A write, with its table quoted, schema-qualified or neither.
TABLE = r'(?:"?public"?\.)?"?(\w+)"?'
WRITE = re.compile(rf"^(update|delete\s+from)\s+{TABLE}", re.IGNORECASE)
# Every write verb anywhere in the body, as a second layer under the allow-list; FOR UPDATE (a row
# lock) is not one.
WRITES_ANYWHERE = re.compile(
    rf"(?<!for )\b(update|insert\s+into|delete\s+from|merge\s+into|truncate(?:\s+table)?"
    rf"|alter\s+table|drop\s+table|create\s+table|copy|grant)\s+{TABLE}",
    re.IGNORECASE,
)
SPLIT = re.compile(r";|\b(?:then|else|loop|begin|declare)\b", re.IGNORECASE)
# Dynamic SQL: a write the static check cannot read.
DYNAMIC = re.compile(r"\bexecute\b", re.IGNORECASE)
CALLS = re.compile(r"\b(\w+)\s*\(")
CONSENT_ORIGIN_WRITE = re.compile(
    r"\bupdate\s+consents\s+c\s+set\s+ip\s*=\s*null\s*,\s*user_agent\s*=\s*null\s+where\b",
    re.IGNORECASE,
)
BOOKING_ORIGIN_WRITE = re.compile(
    r"\bupdate\s+booking_events\s+e\s+set\s+ip\s*=\s*null\s*,\s*user_agent\s*=\s*null"
    r"\s+where\b",
    re.IGNORECASE,
)


def statements(source: str) -> list[str]:
    return [" ".join(part.split()) for part in SPLIT.split(code(source))]


def writes(source: str) -> list[tuple[str, str]]:
    """The writes the body's statements make, and refused statements as ("refused", text)."""
    found: list[tuple[str, str]] = []
    for statement in statements(source):
        write = WRITE.match(statement)
        if write:
            found.append((write.group(1).lower().split()[0], write.group(2)))
        elif not any(pattern.match(statement) for pattern in ALLOWED):
            found.append(("refused", statement[:60]))
    anywhere = [
        (verb.lower().split()[0], table) for verb, table in WRITES_ANYWHERE.findall(code(source))
    ]
    if sorted(anywhere) != sorted(w for w in found if w[0] != "refused"):
        found.append(("hidden", repr(anywhere)))
    return found


def violations(source: str, own: str, schema_functions: set[str]) -> list[str]:
    """What a retention body may not hold: dynamic SQL, or a call to a function of our schema
    (whose own writes this check does not see)."""
    found = [f"execute: {m.group(0)}" for m in DYNAMIC.finditer(code(source))]
    found += [
        f"call: {name}"
        for name in CALLS.findall(code(source))
        if name in schema_functions and name != own
    ]
    return found


ORIGIN_WRITES = {
    "sweep_booking_origins": ("booking_events", BOOKING_ORIGIN_WRITE),
    "sweep_consent_origins": ("consents", CONSENT_ORIGIN_WRITE),
}


def origin_writes_ok(
    source: str, schema_functions: frozenset[str] = frozenset(), own: str = "sweep_booking_origins"
) -> bool:
    """The body's only write is the one statement that clears ip and user_agent, nothing else."""
    table, write = ORIGIN_WRITES[own]
    return (
        writes(source) == [("update", table)]
        and bool(write.search(code(source)))
        and not violations(source, own, set(schema_functions))
    )


EXPECTED_WRITES = {
    "sweep_audit_events": [
        ("delete", "audit_events"),
        ("delete", "audit_events"),
        ("delete", "platform_audit_events"),
        ("delete", "platform_audit_events"),
    ],
    "sweep_admin_sessions": [("delete", "admin_sessions")],
    "sweep_account_sessions": [("delete", "account_sessions")],
    "sweep_booking_origins": [("update", "booking_events")],
    "sweep_consent_origins": [("update", "consents")],
}
DEFINITIONS = text("""
SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND p.proname = ANY(:n)
""")
SCHEMA_FUNCTIONS = text("""
SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
""")


def definitions(conn: Connection) -> dict[str, str]:
    return {name: src for name, src in conn.execute(DEFINITIONS, {"n": sorted(EXPECTED_WRITES)})}


# M2: FENCE (review B2; build review, security #4). Wrong impl: a body that writes anything else:
# another column of a booking event (its evidence), another table (quoted or schema-qualified
# too), an INSERT, DELETE, MERGE or TRUNCATE, DDL, COPY or GRANT, a write behind a condition or in a
# WITH, dynamic SQL, or a call to another function of ours that writes for it. booking_events,
# platform events and admin sessions have no policy holding the line, so this and the boundary
# tests do.
def test_each_retention_function_writes_only_what_it_is_for(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        sources = definitions(conn)
        schema = frozenset(conn.scalars(SCHEMA_FUNCTIONS))
    assert {name: writes(src) for name, src in sources.items()} == EXPECTED_WRITES
    assert {name: violations(src, name, set(schema)) for name, src in sources.items()} == {
        name: [] for name in EXPECTED_WRITES
    }
    assert all(origin_writes_ok(sources[name], schema, name) for name in ORIGIN_WRITES)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL, event = 'x' WHERE true",
        "UPDATE booking_events e SET ip = NULL WHERE true",
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        " DELETE FROM booking_events WHERE true",
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        " MERGE INTO booking_events t USING x ON true WHEN MATCHED THEN DELETE",
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        " TRUNCATE platform_audit_events",
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        " EXECUTE 'UPDATE booking_events SET details = NULL'",
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        " PERFORM other_writer()",
        # Re-verify N2: forms a deny-list of verbs and bare names let through.
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        ' UPDATE "booking_events" SET details = NULL',
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        ' DELETE FROM "platform_audit_events"',
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        " UPDATE public.booking_events SET details = NULL",
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        " ALTER TABLE booking_events DROP COLUMN details",
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        " DROP TABLE platform_audit_events",
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        " CREATE TABLE copy_of AS SELECT * FROM booking_events",
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        " COPY audit_events FROM PROGRAM 'x'",
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        " GRANT DELETE ON booking_events TO ziftbook_app",
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        " IF true THEN DROP TABLE booking_events; END IF",
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true;"
        " WITH gone AS (DELETE FROM booking_events RETURNING 1) SELECT 1",
    ],
    ids=[
        "column",
        "half",
        "delete",
        "merge",
        "truncate",
        "execute",
        "call",
        "quoted update",
        "quoted delete",
        "schema update",
        "alter",
        "drop",
        "create",
        "copy",
        "grant",
        "after then",
        "cte",
    ],
)
def test_the_write_check_catches_a_wider_statement(statement: str) -> None:
    schema = frozenset({"other_writer", "sweep_booking_origins"})
    assert not origin_writes_ok(statement, schema)
    assert origin_writes_ok(
        "UPDATE booking_events e SET ip = NULL, user_agent = NULL WHERE true", schema
    )


# Build review (correctness #5): every batch takes its rows oldest first. Wrong impl: a batch
# statement with a LIMIT and no ORDER BY created_at (any expired rows may go, but the stated order
# and T9's claim would no longer hold; an index scan happens to return them in order today).
ORDERED_BATCH = re.compile(r"ORDER\s+BY\s+x\.created_at\s+LIMIT\b", re.IGNORECASE)


def unordered_batches(source: str) -> int:
    limits_found = len(re.findall(r"\bLIMIT\b", code(source), re.IGNORECASE))
    return limits_found - len(ORDERED_BATCH.findall(code(source)))


def test_every_event_batch_takes_the_oldest_first(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        sources = definitions(conn)
    assert unordered_batches(sources["sweep_audit_events"]) == 0
    assert unordered_batches(sources["sweep_booking_origins"]) == 0
    assert unordered_batches(sources["sweep_consent_origins"]) == 0
    assert unordered_batches("SELECT x.id FROM t x WHERE true LIMIT 1000") == 1
