"""The API role can no longer mint email tokens (ZB-08, second of two releases).

mint_email_token (0007) sets the hash of the token a sign-up or reset email carries. Only the
email.token job calls it, and the worker connects as ziftbook_worker, which holds EXECUTE on it in
its own right since 0049. This is the contract step: ziftbook_app, the role the API connects as,
loses it. The API still starts a request (start_email_token) and queues its job; it never mints.

Precondition: the worker connects as ziftbook_worker in every environment (its own password secret
and ZIF_DATABASE_URL). A worker still on ziftbook_app's URL fails every email.token job after this,
so sign-up, password reset and the sign-in note for a registered email stop being sent. Invite
emails do not use this function.

Revision ID: 0051
Revises: 0050
"""

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("REVOKE EXECUTE ON FUNCTION mint_email_token(uuid, bytea) FROM ziftbook_app")


def downgrade() -> None:
    op.execute("GRANT EXECUTE ON FUNCTION mint_email_token(uuid, bytea) TO ziftbook_app")
