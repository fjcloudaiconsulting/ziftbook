"""keep_an_owner: a business with members always has an owner.

A trigger, not app code: two owners demoting or removing each other at the same moment would each
see the other still an owner. The trigger locks the business row, so the second waits, and then
checks again with a fresh snapshot.

Revision ID: 0014
Revises: 0016
"""

from alembic import op

revision = "0014"
down_revision = "0016"
branch_labels = None
depends_on = None

KEEP_AN_OWNER = """
CREATE FUNCTION keep_an_owner() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = pg_catalog, public, pg_temp AS $$
-- VOLATILE: every statement below takes a new snapshot (READ COMMITTED), so the check sees an owner
-- change another transaction committed while this one waited on the lock.
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.role = 'owner' THEN
    RETURN NULL;  -- owner to owner: nothing is lost
  END IF;
  IF current_setting('transaction_isolation') <> 'read committed' THEN
    -- A snapshot taken before the lock wait would miss the other owner's change.
    RAISE EXCEPTION 'keep_an_owner needs READ COMMITTED' USING ERRCODE = '0A000';
  END IF;
  -- Its own statement: the check below must be planned and run after the lock is granted.
  -- NO KEY UPDATE: a foreign key check on tenants (a new membership, a saved setting) takes
  -- KEY SHARE and isn't blocked.
  PERFORM FROM tenants WHERE id = OLD.tenant_id FOR NO KEY UPDATE;
  -- No member left is a business being torn down (all its memberships in one statement; row
  -- triggers fire after the whole statement), not a business without an owner.
  IF NOT EXISTS (SELECT FROM memberships WHERE tenant_id = OLD.tenant_id AND role = 'owner')
     AND EXISTS (SELECT FROM memberships WHERE tenant_id = OLD.tenant_id) THEN
    RAISE EXCEPTION 'a business needs an owner'
      USING ERRCODE = '23514', CONSTRAINT = 'last_owner';
  END IF;
  RETURN NULL;
END $$"""


def upgrade() -> None:
    op.execute(KEEP_AN_OWNER)
    # WHEN can't name NEW: the trigger also fires on DELETE.
    op.execute(
        "CREATE TRIGGER keep_an_owner AFTER UPDATE OF role OR DELETE ON memberships "
        "FOR EACH ROW WHEN (OLD.role = 'owner') EXECUTE FUNCTION keep_an_owner()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER keep_an_owner ON memberships")
    op.execute("DROP FUNCTION keep_an_owner()")
