"""The app role can no longer delete memberships (ZB-10, contract step).

Removing a member sets role 'removed' and removed_at (ZIF-112, 0045): the row stays, because
bookings and their history reference it. No app code deletes a membership any more, so
ziftbook_app (and ziftbook_worker, through it) loses DELETE on memberships. 0048 left it in
place for the deploy overlap.

Precondition: no deployed release still runs DELETE FROM memberships, as every release up to
v0.30.0 does to remove a member. Applied beside such a release, its member removal fails.

Revision ID: 0052
Revises: 0051
"""

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("REVOKE DELETE ON memberships FROM ziftbook_app")


def downgrade() -> None:
    op.execute("GRANT DELETE ON memberships TO ziftbook_app")
