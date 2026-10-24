"""The worker role may mint email tokens (ZB-08, first of two releases).

mint_email_token (0007) sets the hash of the token a sign-up or reset email carries. Only the
email.token job needs it, so it moves to ziftbook_worker, the role the worker now connects as
(bootstrap.sql creates it, a member of ziftbook_app). This grant is the expand step: ziftbook_app
keeps EXECUTE for now, so a worker still connecting as ziftbook_app, from a running older release
or a deployment whose worker URL has not moved yet, keeps sending links. The contract migration
in the next release revokes it from ziftbook_app.

ziftbook_worker must exist before this runs (bootstrap.sql, run before migrations); otherwise the
GRANT fails and the migration stops.

Revision ID: 0049
Revises: 0048
"""

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT EXECUTE ON FUNCTION mint_email_token(uuid, bytea) TO ziftbook_worker")


def downgrade() -> None:
    op.execute("REVOKE EXECUTE ON FUNCTION mint_email_token(uuid, bytea) FROM ziftbook_worker")
