"""Clear the member's address and browser from existing merchant-entered consents (ZIF-156).

After 0096 has committed, so its trigger already strips every new merchant row: nothing written
during or after this update can bring a member's address back. Business by business under
tenant_isolation (consents has FORCE row-level security), so FORCE is never lifted and no ACCESS
EXCLUSIVE lock is taken: a plain UPDATE (ROW EXCLUSIVE, which reads and inserts do not wait on)
of the merchant rows that still hold either value. Only an erasure deleting one of those rows waits,
until this commits.

The downgrade changes nothing: cleared addresses do not come back.

Revision ID: 0097
Revises: 0096
"""

from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    DO $$
    DECLARE v_t uuid;
    BEGIN
      FOR v_t IN SELECT t.id FROM tenants t ORDER BY t.id LOOP
        PERFORM set_config('app.tenant_id', v_t::text, true);
        UPDATE consents SET ip = NULL, user_agent = NULL
        WHERE tenant_id = v_t AND source = 'merchant'
          AND (ip IS NOT NULL OR user_agent IS NOT NULL);
      END LOOP;
      PERFORM set_config('app.tenant_id', '', true);
    END $$""")


def downgrade() -> None:
    pass
