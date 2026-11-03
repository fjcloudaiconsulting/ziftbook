"""The worker may ask whether an erased client is still owed money (ZIF-58).

The worker deletes an erased client's contact hold (0064) once nothing is left to tell the client:
no money still owed an answer, among other things. client_money_owed (0064) is the one rule for
that, shared with the erase core, which writes the hold on it; it is SECURITY INVOKER, so the
worker reads through it with its own rights, under row-level security, in the job's business. Only
the worker gets it: the API never purges a hold.

Revision ID: 0077
Revises: 0059
"""

from alembic import op

revision = "0077"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT EXECUTE ON FUNCTION client_money_owed(uuid, uuid) TO ziftbook_worker")


def downgrade() -> None:
    op.execute("REVOKE EXECUTE ON FUNCTION client_money_owed(uuid, uuid) FROM ziftbook_worker")
