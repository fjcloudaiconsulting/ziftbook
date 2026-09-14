"""Tenants: the global table every tenant-owned row belongs to.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Global, not tenant-owned, so no RLS: onboarding, sign-in and slug lookup read it before a
    # tenant is known. Keep it to non-sensitive identity; private tenant data goes in tenant-owned
    # tables.
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tenants")
