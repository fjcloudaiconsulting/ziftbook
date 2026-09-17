"""time_off: blocks when a member can't be booked. Manual now; Google busy blocks later.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

from app.db import enable_tenant_isolation

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "time_off",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),  # exclusive
        # Private: only the member and owners read it (app/time_off.py). NULL when none.
        sa.Column("reason", sa.Text()),
        sa.Column("source", sa.Text(), nullable=False, server_default="manual"),
        sa.Column("external_id", sa.Text()),  # the calendar's event id, for source 'google'
        sa.CheckConstraint("ends_at > starts_at", name="ends_after_start"),
        # Multi-day, never open-ended. Elapsed time, so DST doesn't matter.
        sa.CheckConstraint("ends_at - starts_at <= interval '366 days'", name="at_most_366_days"),
        sa.CheckConstraint("char_length(reason) BETWEEN 1 AND 500", name="reason_length"),
        sa.CheckConstraint("source IN ('manual', 'google')", name="source"),
        # A synced block always has its event id; a manual one never does.
        sa.CheckConstraint("(source = 'manual') = (external_id IS NULL)", name="external_id"),
        # A sync upserts on this. Manual rows (NULL external_id) never collide: NULLS DISTINCT.
        sa.UniqueConstraint("tenant_id", "member_id", "source", "external_id"),
        # Removing a member removes their time off.
        sa.ForeignKeyConstraint(
            ["tenant_id", "member_id"],
            ["memberships.tenant_id", "memberships.id"],
            ondelete="CASCADE",
        ),
    )
    # The window query: one member's blocks by start.
    op.create_index(
        "ix_time_off_tenant_id_member_id_starts_at",
        "time_off",
        ["tenant_id", "member_id", "starts_at"],
    )
    enable_tenant_isolation("time_off")


def downgrade() -> None:
    op.drop_table("time_off")
