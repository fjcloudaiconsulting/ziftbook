"""Foundation: btree_gist and default privileges for the app role.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Needed by the booking exclusion constraint (worker_id WITH =, time range WITH &&).
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    # Every table and sequence the migrate role creates from now on is usable,
    # not ownable, by the app.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ziftbook_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO ziftbook_app"
    )


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE USAGE, SELECT ON SEQUENCES FROM ziftbook_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM ziftbook_app"
    )
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
