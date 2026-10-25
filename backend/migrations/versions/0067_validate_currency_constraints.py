"""Validate the currency check on tenants and the cascading services price key (ZIF-102).

0066 added both NOT VALID, so their locks lasted only for the catalog change. Validating the check
takes SHARE UPDATE EXCLUSIVE on tenants and blocks no reads or writes. services has FORCE row-level
security, so its key is validated with FORCE lifted (app.db.lift_force, CONTRIBUTING.md "Database
migrations"): NO FORCE holds ACCESS EXCLUSIVE on services until commit, the length of the scan of a
small table. One FORCE-lifted table per migration: 0068 validates payment_accounts' key on its own,
so services is never held while another table's lock is awaited.

Revision ID: 0067
Revises: 0066
"""

from alembic import op

from app.db import lift_force

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants VALIDATE CONSTRAINT ck_tenants_currency_supported")
    lift_force(
        op.execute,
        "services",
        "ALTER TABLE services VALIDATE CONSTRAINT fk_services_tenant_id_price_currency_tenants",
    )


def downgrade() -> None:
    # A validated constraint cannot be marked NOT VALID again, and need not be: 0066's downgrade
    # drops them.
    pass
