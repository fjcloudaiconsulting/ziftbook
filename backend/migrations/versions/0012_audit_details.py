"""audit_events.details: what an event changed, such as a setting's old and new value.

Never personal data or a secret: an owner reads these values in their own log.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("details", JSONB(), nullable=True))
    # 0010 grants INSERT column by column, so a new column needs its own grant.
    op.execute("GRANT INSERT (details) ON audit_events TO ziftbook_app")


def downgrade() -> None:
    op.drop_column("audit_events", "details")
