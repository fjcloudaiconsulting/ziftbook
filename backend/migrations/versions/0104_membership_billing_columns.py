"""Who keeps access when a business has more members than its plan's places (ZIF-78).

- memberships.keeps_access: an owner's pick of who stays within the places; the app role may set it.
- memberships.access_paused_at: set by the worker when the grace period ends.

Apart from 0103 on purpose: this takes ACCESS EXCLUSIVE on memberships, and 0103 holds SHARE ROW
EXCLUSIVE on tenants until it commits. complete_sign_up inserts a tenant and then a membership, so
one transaction taking both could deadlock with a sign-up mid-deploy. Here memberships is the only
existing table locked, briefly: both columns are added without a rewrite.

The downgrade drops both columns, and their grants with them.

Revision ID: 0104
Revises: 0103
"""

from alembic import op

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE memberships
      ADD COLUMN keeps_access boolean NOT NULL DEFAULT false,
      ADD COLUMN access_paused_at timestamptz
    """)
    op.execute("GRANT UPDATE (keeps_access) ON memberships TO ziftbook_app")
    op.execute("GRANT UPDATE (access_paused_at) ON memberships TO ziftbook_worker")


def downgrade() -> None:
    # No REVOKE: DROP COLUMN takes the privilege with it (0029's downgrade comment).
    op.execute("ALTER TABLE memberships DROP COLUMN keeps_access, DROP COLUMN access_paused_at")
