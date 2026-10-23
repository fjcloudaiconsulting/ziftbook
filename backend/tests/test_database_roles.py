import uuid
from collections.abc import Iterator

import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError


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
