"""Validate the currency constraints 0062 and 0066 added NOT VALID (ZIF-102).

ck_tenants_currency_supported, the cascading services price key and the payment_accounts currency
key were added without a scan, so their locks lasted only for the catalog change. Validating takes
SHARE UPDATE EXCLUSIVE on the table checked and ROW SHARE on tenants, and blocks no reads or writes.

services and payment_accounts have FORCE row-level security, and a foreign key's validation scan
runs as the table owner under it: with no app.tenant_id it fails (42704), and with one it would
check a single business's rows and still mark the key valid. So FORCE is lifted around each
validation, inside this transaction; ALTER TABLE ... NO FORCE takes ACCESS EXCLUSIVE on that one
table until commit, which on these small tables is the length of the scan. No other session sees
the lifted state: the lock excludes them, and FORCE binds only the owner.

Revision ID: 0067
Revises: 0066
"""

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants VALIDATE CONSTRAINT ck_tenants_currency_supported")
    for table, key in (
        ("services", "fk_services_tenant_id_price_currency_tenants"),
        ("payment_accounts", "fk_payment_accounts_tenant_id_currency_tenants"),
    ):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {key}")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # A validated constraint cannot be marked NOT VALID again, and need not be: 0066's and 0062's
    # downgrades drop them.
    pass
