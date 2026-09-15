"""settings: one row per business and key, holding the value its owner saved.

What a key means, its type and its default live in app/business_settings.py; the database only
keeps a business's saved values apart from every other business's.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from app.db import enable_tenant_isolation

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        # No CHECK on keys or values: the registry is their one definition.
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", JSONB(), nullable=False),
        sa.UniqueConstraint("tenant_id", "key"),
    )
    enable_tenant_isolation("settings")


def downgrade() -> None:
    op.drop_table("settings")
