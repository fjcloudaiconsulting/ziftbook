"""Email outbox: what was sent to whom, never the rendered message.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

from app.db import enable_tenant_isolation

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_outbox",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        # No foreign key, so cleaning up old jobs never touches the record of what was sent.
        sa.Column("job_id", sa.Uuid(), nullable=False),
        # The addressee; a composite foreign key to users or customers comes when those exist.
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending', 'sent')", name="status"),
        sa.UniqueConstraint("tenant_id", "job_id"),
    )
    enable_tenant_isolation("email_outbox")


def downgrade() -> None:
    op.drop_table("email_outbox")
