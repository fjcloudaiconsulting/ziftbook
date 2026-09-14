from collections.abc import Iterator

import pytest
from alembic import op
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, text

from app.db import enable_tenant_isolation, metadata

# Every table with a tenant_id column must be isolated, and every foreign key between isolated
# tables must pair tenant_id with tenant_id (Postgres checks foreign keys with RLS bypassed).
VIOLATIONS = text("""
SELECT c.relname || ': has tenant_id but no forced RLS with the tenant_isolation policy'
FROM pg_class c
WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
  AND c.relname <> 'jobs'  -- global on purpose: claimed across tenants
  AND EXISTS (
    SELECT FROM pg_attribute a
    WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped)
  AND NOT (
    c.relrowsecurity AND c.relforcerowsecurity
    AND EXISTS (
      SELECT FROM pg_policy p WHERE p.polrelid = c.oid AND p.polname = 'tenant_isolation'))
UNION ALL
SELECT con.conname || ': foreign key between tenant-owned tables without tenant_id'
FROM pg_constraint con
JOIN pg_class src ON src.oid = con.conrelid
JOIN pg_class dst ON dst.oid = con.confrelid
WHERE con.contype = 'f' AND src.relrowsecurity AND dst.relrowsecurity
  AND NOT EXISTS (
    SELECT FROM unnest(con.conkey, con.confkey) AS k(src_col, dst_col)
    JOIN pg_attribute s ON s.attrelid = con.conrelid AND s.attnum = k.src_col
    JOIN pg_attribute d ON d.attrelid = con.confrelid AND d.attnum = k.dst_col
    WHERE s.attname = 'tenant_id' AND d.attname = 'tenant_id')
""")


def violations(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(conn.scalars(VIOLATIONS))


def test_schema_has_no_tenant_isolation_violations(migrate_engine: Engine) -> None:
    assert violations(migrate_engine) == []


@pytest.fixture
def broken_tables(migrate_engine: Engine) -> Iterator[None]:
    with migrate_engine.begin() as conn:
        conn.execute(
            text("""
            CREATE TABLE broken_parents (
              id uuid PRIMARY KEY DEFAULT uuidv7(),
              tenant_id uuid NOT NULL REFERENCES tenants (id));
            CREATE TABLE broken_children (
              id uuid PRIMARY KEY DEFAULT uuidv7(),
              tenant_id uuid NOT NULL REFERENCES tenants (id),
              parent_id uuid REFERENCES broken_parents (id));
            CREATE TABLE broken_unisolated (tenant_id uuid REFERENCES tenants (id));
            """)
        )
        ctx = MigrationContext.configure(conn, opts={"target_metadata": metadata})
        with Operations.context(ctx):
            enable_tenant_isolation("broken_parents")
            enable_tenant_isolation("broken_children")
            # The correct shape next to the broken ones: must not be reported.
            op.execute("""
            CREATE TABLE good_children (
              id uuid PRIMARY KEY DEFAULT uuidv7(),
              tenant_id uuid NOT NULL REFERENCES tenants (id),
              parent_id uuid,
              FOREIGN KEY (tenant_id, parent_id) REFERENCES broken_parents (tenant_id, id))
            """)
            enable_tenant_isolation("good_children")
    yield
    with migrate_engine.begin() as conn:
        conn.execute(
            text("DROP TABLE good_children, broken_children, broken_parents, broken_unisolated")
        )


def test_the_check_catches_plain_foreign_keys_and_unisolated_tables(
    migrate_engine: Engine, broken_tables: None
) -> None:
    assert sorted(violations(migrate_engine)) == [
        "broken_children_parent_id_fkey: foreign key between tenant-owned tables without tenant_id",
        "broken_unisolated: has tenant_id but no forced RLS with the tenant_isolation policy",
    ]
