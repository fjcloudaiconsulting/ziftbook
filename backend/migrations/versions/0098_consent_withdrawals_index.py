"""The index that drives the consent-address sweep (ZIF-156, 0096's sweep_consent_origins).

consents (tenant_id, created_at) WHERE NOT granted: the sweep reads each business's withdrawals
older than a year, since every answer change a year old has one on one side or the other, then
looks up those clients' rows through 0024's (tenant_id, client_id, purpose, id) index. So a call
costs about the withdrawals past the year, not every consent that stands.

CREATE INDEX CONCURRENTLY in its own autocommit_block, as 0072 does (CONTRIBUTING, "Database
migrations"): the re-run of a failed build drops an invalid leftover first, and lock_timeout is 0
while building and back to 5 seconds after.

The downgrade drops the index, also concurrently.

Revision ID: 0098
Revises: 0097
"""

from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None

NAME = "ix_consents_tenant_id_created_at_withdrawals"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME}")
        op.execute("SET lock_timeout = 0")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {NAME} ON consents (tenant_id, created_at)"
            " WHERE NOT granted"
        )
        op.execute("SET lock_timeout = '5s'")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME}")
