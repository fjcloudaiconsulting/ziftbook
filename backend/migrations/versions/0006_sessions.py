"""Sessions: server-side, so signing someone out takes effect on their next request.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import BYTEA, INET

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Global, not tenant-owned: the cookie lookup is what tells us the tenant. Holds no tenant data,
    # only a token hash, ids and the client's address.
    op.create_table(
        "sessions",
        sa.Column("id_hash", BYTEA(), primary_key=True),  # sha256 of the cookie's random token
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),  # absolute limit
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("ip", INET()),
        sa.Column("user_agent", sa.Text()),
        sa.CheckConstraint("octet_length(id_hash) = 32", name="id_hash_length"),
        # A session exists only for the membership and role it was granted: removing the membership
        # deletes it, and changing the role fails until its sessions are deleted (that is the
        # rotation on a role change).
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id", "role"],
            ["memberships.tenant_id", "memberships.user_id", "memberships.role"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(None, "sessions", ["user_id"])  # sign out everywhere
    op.create_index(None, "sessions", ["expires_at"])  # the purge


def downgrade() -> None:
    op.drop_table("sessions")
