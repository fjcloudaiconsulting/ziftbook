"""Jobs: deferred and scheduled work, claimed by the worker across tenants.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Global, not tenant-owned: the worker claims jobs for every tenant and runs each handler inside
    # tenant_context(tenant_id). Payloads carry ids only, never a tenant's data.
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        # Makes enqueueing idempotent: a key runs once, ever.
        sa.Column("dedupe_key", sa.Text(), nullable=False, unique=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE")),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        # When the job may next be claimed: the due time, then the lease and backoff after a claim.
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        # Completed without running, because it was overdue beyond its kind's grace.
        sa.Column("skipped", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # What the worker's claim scans; 5 is the attempt cap.
    op.create_index(
        "ix_jobs_claimable",
        "jobs",
        ["next_attempt_at"],
        postgresql_where=sa.text("completed_at IS NULL AND attempts < 5"),
    )


def downgrade() -> None:
    op.drop_table("jobs")
