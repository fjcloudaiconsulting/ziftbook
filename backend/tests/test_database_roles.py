import hashlib
import uuid
from collections.abc import Iterator

import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.db import SessionLocal, reset_session
from app.jobs import enqueue
from tests.conftest import fresh_email, mailed, token_in


def test_btree_gist_is_installed(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        installed = conn.scalar(
            text("SELECT count(*) FROM pg_extension WHERE extname = 'btree_gist'")
        )
    assert installed == 1


def test_app_role_has_no_elevated_attributes(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert tuple(row) == (False, False)


def test_app_role_owns_nothing(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        owned = conn.scalar(
            text("SELECT count(*) FROM pg_class WHERE relowner = current_user::regrole")
        )
    assert owned == 0


def test_migrate_role_owns_the_alembic_version_table(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        owner = conn.scalar(
            text("SELECT tableowner FROM pg_tables WHERE tablename = 'alembic_version'")
        )
    assert owner == "ziftbook_migrate"


@pytest.fixture
def probe_table(migrate_engine: Engine) -> Iterator[str]:
    # Migration #1 creates no tables yet, so create one the way a migration would:
    # as the migrate role.
    name = f"probe_{uuid.uuid4().hex[:12]}"
    with migrate_engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE {name} (id int PRIMARY KEY, note text)"))
    yield name
    with migrate_engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {name}"))


def test_app_role_gets_row_access_to_new_tables(app_engine: Engine, probe_table: str) -> None:
    with app_engine.begin() as conn:
        conn.execute(text(f"INSERT INTO {probe_table} (id, note) VALUES (1, 'hello')"))
        conn.execute(text(f"UPDATE {probe_table} SET note = 'updated' WHERE id = 1"))
        assert conn.scalar(text(f"SELECT note FROM {probe_table} WHERE id = 1")) == "updated"
        conn.execute(text(f"DELETE FROM {probe_table} WHERE id = 1"))


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE {table} ADD COLUMN extra int",
        "DROP TABLE {table}",
        "CREATE TABLE {table}_copy (id int)",
    ],
)
def test_app_role_cannot_change_the_schema(
    app_engine: Engine, probe_table: str, statement: str
) -> None:
    with app_engine.connect() as conn, pytest.raises(ProgrammingError) as error:
        conn.execute(text(statement.format(table=probe_table)))
    assert isinstance(error.value.orig, InsufficientPrivilege)


# ZB-10 FENCE. Wrong impl: any of these back on the app role. Sign-up, first-run setup and invites
# create users and businesses through SECURITY DEFINER functions owned by ziftbook_migrate; the
# mailer only inserts outbox rows and marks them sent. DELETE on memberships is revoked in a later
# contract step (see 0048).
@pytest.mark.parametrize(
    ("table", "privilege"),
    [
        ("users", "INSERT"),
        ("users", "UPDATE"),
        ("users", "DELETE"),
        ("tenants", "INSERT"),
        ("email_outbox", "UPDATE"),
        ("email_outbox", "DELETE"),
    ],
)
def test_app_role_lacks_writes_it_never_uses(
    migrate_engine: Engine, table: str, privilege: str
) -> None:
    with migrate_engine.connect() as conn:
        held = conn.scalar(
            text("SELECT has_table_privilege('ziftbook_app', :t, :p)"),
            {"t": table, "p": privilege},
        )
    assert held is False


# What app/mail.py's _outbox (ON CONFLICT ... SET subject) and _mark_sent set, and nothing else.
def test_app_role_updates_only_the_outbox_columns_the_mailer_sets(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        columns = set(
            conn.scalars(
                text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'email_outbox'
                  AND has_column_privilege('ziftbook_app', 'email_outbox', column_name, 'UPDATE')
                """)
            )
        )
    assert columns == {"status", "sent_at", "subject"}


@pytest.fixture
def probe_function(migrate_engine: Engine) -> Iterator[str]:
    # Created the way a migration would: as the migrate role.
    name = f"probe_{uuid.uuid4().hex[:12]}"
    with migrate_engine.begin() as conn:
        conn.execute(text(f"CREATE FUNCTION {name}() RETURNS int LANGUAGE sql AS 'SELECT 1'"))
    yield name
    with migrate_engine.begin() as conn:
        conn.execute(text(f"DROP FUNCTION IF EXISTS {name}()"))


# ZB-10 FENCE. Wrong impl: a function the migrate role creates is callable by everyone until its
# migration remembers to REVOKE it from PUBLIC.
def test_app_role_cannot_call_a_new_function_until_granted(
    app_engine: Engine, migrate_engine: Engine, probe_function: str
) -> None:
    with app_engine.connect() as conn, pytest.raises(ProgrammingError) as error:
        conn.execute(text(f"SELECT {probe_function}()"))
    assert isinstance(error.value.orig, InsufficientPrivilege)
    with migrate_engine.begin() as conn:
        conn.execute(text(f"GRANT EXECUTE ON FUNCTION {probe_function}() TO ziftbook_app"))
    with app_engine.connect() as conn:
        assert conn.scalar(text(f"SELECT {probe_function}()")) == 1


# ZB-08: the worker connects as ziftbook_worker, the app role plus what only background jobs may do.
def test_worker_role_has_no_elevated_attributes_and_owns_nothing(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        row = conn.execute(
            text("""
            SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb,
                   (SELECT count(*) FROM pg_class WHERE relowner = r.oid)
                   + (SELECT count(*) FROM pg_proc WHERE proowner = r.oid)
            FROM pg_roles r WHERE rolname = 'ziftbook_worker'
            """)
        ).one()
    assert tuple(row) == (True, False, False, False, False, 0)


def test_worker_role_has_every_privilege_of_the_app_role(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        inherits = conn.scalar(
            text("SELECT pg_has_role('ziftbook_worker', 'ziftbook_app', 'USAGE')")
        )
    assert inherits is True


# ZB-08 FENCE. Wrong impl: the worker reaches mint_email_token only through ziftbook_app, so taking
# it from the app role in the next release (the contract migration) would leave no role that can
# mint; or the worker gets grants of its own beyond it.
def test_worker_role_holds_only_mint_email_token_in_its_own_right(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        held = conn.scalars(
            text("""
            SELECT format('%s(%s) %s', p.proname, pg_get_function_identity_arguments(p.oid),
                          x.privilege_type)
            FROM pg_proc p, aclexplode(p.proacl) x
            WHERE x.grantee = 'ziftbook_worker'::regrole
            UNION ALL
            SELECT format('%s %s', c.relname, x.privilege_type)
            FROM pg_class c, aclexplode(c.relacl) x
            WHERE x.grantee = 'ziftbook_worker'::regrole
            UNION ALL
            SELECT format('%s.%s %s', c.relname, a.attname, x.privilege_type)
            FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid, aclexplode(a.attacl) x
            WHERE x.grantee = 'ziftbook_worker'::regrole
            ORDER BY 1
            """)
        ).all()
    assert list(held) == ["mint_email_token(p_id uuid, p_token_hash bytea) EXECUTE"]


# ZB-31 FENCE. Wrong impl: PUBLIC, or either role in its own right, keeps TEMPORARY on the
# database (0050 here, bootstrap.sql where the admin owns the database).
@pytest.mark.parametrize("role", ["ziftbook_app", "ziftbook_worker"])
def test_neither_runtime_role_can_create_temporary_tables(
    migrate_engine: Engine, role: str
) -> None:
    with migrate_engine.connect() as conn:
        held = conn.scalar(
            text("SELECT has_database_privilege(:role, current_database(), 'TEMPORARY')"),
            {"role": role},
        )
    assert held is False


# ZB-31 FENCE (bootstrap.sql). Wrong impl: a role left on the default search path, which looks in
# pg_temp first; or only ziftbook_app's set, which ziftbook_worker does not inherit.
def test_both_runtime_roles_search_pg_temp_last(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        settings = conn.execute(
            text("""
            SELECT rolname, rolconfig FROM pg_roles
            WHERE rolname IN ('ziftbook_app', 'ziftbook_worker') ORDER BY 1
            """)
        ).all()
    assert [tuple(row) for row in settings] == [
        ("ziftbook_app", ["search_path=public, pg_temp"]),
        ("ziftbook_worker", ["search_path=public, pg_temp"]),
    ]


# ZB-31 FENCE. Wrong impl: a pool reset without RESET ROLE (RESET ALL keeps a SET ROLE), so a job
# that switched ziftbook_worker to ziftbook_app hands its next user a connection running as that.
def test_a_pooled_worker_connection_is_handed_out_as_the_worker_role(
    worker_engine: Engine,
) -> None:
    engine = create_engine(worker_engine.url, pool_size=1, max_overflow=0)
    event.listen(engine, "reset", reset_session)
    try:
        with engine.begin() as conn:
            used = conn.scalar(text("SELECT pg_backend_pid()"))
            conn.execute(text("SET ROLE ziftbook_app"))
        with engine.connect() as conn:
            reused = conn.execute(text("SELECT pg_backend_pid(), current_user")).one()
    finally:
        engine.dispose()
    assert tuple(reused) == (used, "ziftbook_worker")


def test_the_email_token_job_runs_as_the_worker_role(
    app_engine: Engine, worker_engine: Engine, migrate_engine: Engine
) -> None:
    email = fresh_email()
    # What the sign-up endpoint does, as the app role: a pending request and its job.
    with Session(app_engine) as session, session.begin():
        token_id = str(
            session.scalar(text("SELECT start_email_token('sign_up', :e, 'en')"), {"e": email})
        )
        enqueue(session, "email.token", f"email.token:{token_id}", {"token_id": token_id})
    with worker_engine.connect() as conn:
        assert conn.scalar(text("SELECT current_user")) == "ziftbook_worker"
    SessionLocal.configure(bind=worker_engine)
    try:
        token = token_in(mailed(email))
    finally:
        SessionLocal.configure(bind=None)
    with migrate_engine.connect() as conn:
        stored = conn.scalar(
            text("SELECT token_hash FROM email_tokens WHERE id = :id"), {"id": token_id}
        )
        job = conn.execute(
            text("SELECT completed_at IS NOT NULL, last_error FROM jobs WHERE dedupe_key = :k"),
            {"k": f"email.token:{token_id}"},
        ).one()
    assert stored == hashlib.sha256(token.encode()).digest()
    assert tuple(job) == (True, None)
