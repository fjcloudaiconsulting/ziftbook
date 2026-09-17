"""service_workers: which members perform each service. No row, no availability for that service.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

from app.db import enable_tenant_isolation

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_workers",
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        # A link, not an entity: no id. tenant_id first, so a duplicate reveals nothing across
        # businesses and "the workers of a service" is an index scan.
        sa.PrimaryKeyConstraint("tenant_id", "service_id", "member_id"),
        # Services are archived, never deleted by the app; only ziftbook_migrate deletes a
        # business's services, and their assignments go with them.
        sa.ForeignKeyConstraint(
            ["tenant_id", "service_id"], ["services.tenant_id", "services.id"], ondelete="CASCADE"
        ),
        # Removing a member removes their assignments.
        sa.ForeignKeyConstraint(
            ["tenant_id", "member_id"],
            ["memberships.tenant_id", "memberships.id"],
            ondelete="CASCADE",
        ),
    )
    # The cascade's lookup when a member is removed, and "the services of a member".
    op.create_index(
        "ix_service_workers_tenant_id_member_id", "service_workers", ["tenant_id", "member_id"]
    )
    enable_tenant_isolation("service_workers", referenced=False)


def downgrade() -> None:
    op.drop_table("service_workers")
