"""merge_heads: the two heads left by invites (0015) and working hours (0017).

Both revised 0014 and were merged one after the other. They touch unrelated tables, so the order
they run in doesn't matter. A merge revision, not an edited down_revision: a database that already
applied either one upgrades cleanly.

Revision ID: 0018
Revises: 0015, 0017
"""

revision = "0018"
down_revision = ("0015", "0017")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
