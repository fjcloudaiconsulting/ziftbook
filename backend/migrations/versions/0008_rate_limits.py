"""Rate limits: one row per key, counting attempts in a fixed window.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Global: keys are an action plus a hashed email or a client network, never a tenant's data.
    op.create_table(
        "rate_limits",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
    )
    op.create_index(None, "rate_limits", ["window_start"])  # the purge


def downgrade() -> None:
    op.drop_table("rate_limits")
