"""An erased client is a final tombstone (ZIF-58).

clients gains erased_at. An erased row keeps its id, so its bookings, payments and events keep
their foreign key, and holds nothing about the person: one CHECK replaces ck_clients_name_length
and says that a live row has a name of 1 to 100 characters, and an erased row has the name '' and
no user_id, email, phone, locale or notes. name stays NOT NULL, so a release that predates this
migration reads '' for an erased client, never NULL.

A trigger refuses any UPDATE of an erased row, whoever runs it: an erasure is never undone and its
time is never rewritten. The CHECK alone cannot say that, because an UPDATE that sets erased_at
back to NULL and a new name passes it.

Only the erase function (0064, owned by ziftbook_migrate) writes erased_at. The app role's INSERT
and UPDATE on clients become column lists naming exactly the columns it held before, so the
running release keeps every write it makes and gains none of erased_at.

MAINTENANCE TRAP, as bookings' (0047): a column added to clients later that the app writes needs
its own GRANT INSERT (column) or GRANT UPDATE (column) in its migration.

Downgrade: refuses while an erased row exists ('' fails the old CHECK), which is right: the old
schema cannot hold an erased client.

Revision ID: 0063
Revises: 0056
"""

from alembic import op

revision = "0063"
down_revision = "0056"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

# The app role's writable columns: every column before this migration, so nothing it writes today
# is refused, and not erased_at.
WRITABLE = "id, tenant_id, user_id, name, email, phone, locale, client_note, internal_note"


def upgrade() -> None:
    # One ALTER: the column, and the CHECK swapped in one step, so the table is scanned once and
    # is never without a name check.
    op.execute("""
    ALTER TABLE clients
      ADD COLUMN erased_at timestamptz,
      DROP CONSTRAINT ck_clients_name_length,
      ADD CONSTRAINT ck_clients_name_or_erased CHECK (
        CASE WHEN erased_at IS NULL THEN char_length(name) BETWEEN 1 AND 100
             ELSE name = ''
                  AND num_nonnulls(user_id, email, phone, locale, client_note, internal_note) = 0
        END)""")
    # 23514 by name, as 0047's trigger raises: the row is refused as a constraint would refuse it.
    op.execute(f"""
    CREATE FUNCTION clients_erased_final() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER {SEARCH_PATH} AS $$
      BEGIN
        RAISE EXCEPTION 'an erased client is final'
          USING ERRCODE = '23514', CONSTRAINT = 'clients_erased_final';
      END $$""")
    op.execute("""
    CREATE TRIGGER clients_erased_final BEFORE UPDATE ON clients
      FOR EACH ROW WHEN (OLD.erased_at IS NOT NULL)
      EXECUTE FUNCTION clients_erased_final()""")
    # Firing a trigger checks no EXECUTE privilege (0030), so no role needs it.
    op.execute("REVOKE EXECUTE ON FUNCTION clients_erased_final() FROM PUBLIC")
    # Each REVOKE runs before its GRANT (CONTRIBUTING.md): revoking a table-level privilege also
    # takes every column grant of it, so the other order would leave nothing. SELECT stays
    # table-level, and DELETE stays revoked (0024).
    op.execute("REVOKE INSERT, UPDATE ON clients FROM ziftbook_app")
    op.execute(f"GRANT INSERT ({WRITABLE}) ON clients TO ziftbook_app")
    op.execute(f"GRANT UPDATE ({WRITABLE}) ON clients TO ziftbook_app")


def downgrade() -> None:
    # Back to 0024's table-level INSERT and UPDATE. REVOKE first, as above.
    op.execute("REVOKE INSERT, UPDATE ON clients FROM ziftbook_app")
    op.execute("GRANT INSERT, UPDATE ON clients TO ziftbook_app")
    op.execute("DROP TRIGGER clients_erased_final ON clients")
    op.execute("DROP FUNCTION clients_erased_final()")
    op.execute("""
    ALTER TABLE clients
      DROP CONSTRAINT ck_clients_name_or_erased,
      ADD CONSTRAINT ck_clients_name_length CHECK (char_length(name) BETWEEN 1 AND 100),
      DROP COLUMN erased_at""")
