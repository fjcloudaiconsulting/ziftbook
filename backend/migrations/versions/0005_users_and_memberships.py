"""Users and memberships: who can sign in, and their role in each tenant.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

from app.db import enable_tenant_isolation

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Global: one account per person, across tenants. Emails are stored lowercase so lookups stay
    # `email = :email`.
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("locale", sa.Text()),  # NULL: the tenant's default
        sa.CheckConstraint("email = lower(email)", name="email_lowercase"),
        sa.CheckConstraint("locale IN ('en', 'nl', 'pt')", name="locale"),
    )
    op.create_table(
        "memberships",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        # Checked with RLS bypassed: a tenant that knows a user's id could add them and then read
        # their email. Memberships come only from trusted flows (sign-up, invites), never from a
        # client-supplied user_id.
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'worker')", name="role"),
        sa.UniqueConstraint("tenant_id", "user_id"),
        # The target of the sessions foreign key, so a session carries the role it was granted.
        sa.UniqueConstraint("tenant_id", "user_id", "role"),
    )
    enable_tenant_isolation("memberships")
    # A user's tenants, and the foreign key check when a user is deleted.
    op.create_index(None, "memberships", ["user_id"])

    # The app role reads a user only through a membership in the current tenant, so one tenant can't
    # read another's staff by id; with no tenant, the memberships subquery raises. It may add users
    # but never change or delete one: an email is the person's identity in every tenant (so
    # SELECT ... FOR UPDATE on users returns nothing). Not FORCE, so sign-in functions owned by
    # ziftbook_migrate can look a user up before a tenant is known; for the same reason a view over
    # users needs security_invoker.
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_members ON users FOR SELECT "
        "USING (id IN (SELECT user_id FROM memberships))"
    )
    op.execute("CREATE POLICY create_users ON users FOR INSERT WITH CHECK (true)")


def downgrade() -> None:
    op.execute("DROP POLICY tenant_members ON users")  # it depends on memberships
    op.drop_table("memberships")
    op.drop_table("users")
