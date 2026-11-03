"""A Stripe account holds its business's currency in the database (ZIF-102).

A business's Stripe account is created in the business's currency (its default currency at Stripe,
app/payments.py connect), and every charge, payment method and fixed fee on it assumes that
currency. The owner may change the currency only before connecting Stripe (owner's ruling,
2026-10-25), and the database now says so: payment_accounts records the currency its account was
created with, with a foreign key to tenants (id, currency), NO ACTION. While the row exists the
business's currency cannot change; and an insert whose currency changed since the account was
created at Stripe is refused (connect answers 409 currency_changed).

Expand: the column is nullable, existing rows are backfilled from their business (looping over
tenants, as payment_accounts has FORCE row-level security), and the foreign key is added NOT VALID,
so no scan runs under its lock; 0068 validates it. The app role may insert the column, never update
it.

A NULL currency is not covered by the key. Only a row the previous release inserts during the
rollout can have one, and such a row may already disagree with its business: an owner served by
this release can change the currency while the previous release is still creating the account at
Stripe. So the contract step (a later release, before SET NOT NULL) must not fill those rows from
tenants.currency, which would hide that. It lists every NULL-currency row with its Stripe account's
default currency, fills a row only where the two agree with the business's currency, and flags the
rest for the owner and support, leaving them NULL until resolved.

Revision ID: 0062
Revises: 0061
"""

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE payment_accounts ADD COLUMN currency text")
    op.execute("""
    DO $$
    DECLARE t record;
    BEGIN
      FOR t IN SELECT id, currency FROM tenants LOOP
        PERFORM set_config('app.tenant_id', t.id::text, true);
        UPDATE payment_accounts SET currency = t.currency
        WHERE tenant_id = t.id AND currency IS NULL;
      END LOOP;
      PERFORM set_config('app.tenant_id', '', true);
    END $$""")
    op.execute("""
    ALTER TABLE payment_accounts ADD CONSTRAINT fk_payment_accounts_tenant_id_currency_tenants
      FOREIGN KEY (tenant_id, currency) REFERENCES tenants (id, currency) NOT VALID""")
    op.execute("GRANT INSERT (currency) ON payment_accounts TO ziftbook_app")


def downgrade() -> None:
    op.execute("REVOKE INSERT (currency) ON payment_accounts FROM ziftbook_app")
    op.execute("ALTER TABLE payment_accounts DROP COLUMN currency")  # takes its foreign key
