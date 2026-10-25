"""Validate the payment_accounts currency key 0062 added NOT VALID (ZIF-102).

payment_accounts has FORCE row-level security, so the key is validated with FORCE lifted
(app.db.lift_force, CONTRIBUTING.md "Database migrations"). On its own: NO FORCE holds ACCESS
EXCLUSIVE on payment_accounts until commit, and no other table's lock is held meanwhile.

Revision ID: 0068
Revises: 0067
"""

from alembic import op

from app.db import lift_force

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    lift_force(
        op.execute,
        "payment_accounts",
        "ALTER TABLE payment_accounts VALIDATE CONSTRAINT "
        "fk_payment_accounts_tenant_id_currency_tenants",
    )


def downgrade() -> None:
    # 0062's downgrade drops the column and its key.
    pass
