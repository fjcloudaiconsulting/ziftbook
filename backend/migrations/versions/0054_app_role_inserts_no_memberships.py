"""The app role can no longer insert memberships or change whose they are (ZB-10, contract step).

0001's default privileges gave ziftbook_app table-level INSERT and UPDATE on memberships, so it
could add anyone to a business or move a membership to another user or business. It needs
neither: sign-up and invites create memberships through SECURITY DEFINER functions owned by
ziftbook_migrate (complete_sign_up; accept_invite, which also revives a removed member's row), and
set_own_name fills display names the same way. The app's own updates
(app/members.py) set the role (change_role), the role and removed_at (remove), and the display
name (set_display_name). So ziftbook_app (and ziftbook_worker, through it) loses INSERT, and
keeps UPDATE of only those three columns; UPDATE of one column is enough for the row locks
members.py and auth.py take on memberships.

Precondition: 0052's (no deployed release deletes a membership). No release up to v0.31.0
inserts a membership as the app role or updates a column other than role, removed_at and
display_name.

Revision ID: 0054
Revises: 0053
"""

from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

# What app/members.py's change_role, remove and set_display_name set.
MEMBERSHIP_UPDATABLE = "role, removed_at, display_name"


def upgrade() -> None:
    # The REVOKE runs before the GRANT (CONTRIBUTING.md): revoking a table-level privilege also
    # takes every column grant of it.
    op.execute("REVOKE INSERT, UPDATE ON memberships FROM ziftbook_app")
    op.execute(f"GRANT UPDATE ({MEMBERSHIP_UPDATABLE}) ON memberships TO ziftbook_app")


def downgrade() -> None:
    # Back to what 0053 left: table-level INSERT and UPDATE. The table-level REVOKE takes the
    # column grants first, so none is left beside the table-level one.
    op.execute("REVOKE UPDATE ON memberships FROM ziftbook_app")
    op.execute("GRANT INSERT, UPDATE ON memberships TO ziftbook_app")
