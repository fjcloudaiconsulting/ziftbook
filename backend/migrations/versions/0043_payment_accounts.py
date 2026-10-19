"""payment_accounts: a business's Stripe account, and the webhook's way to it (ZIF-158).

The account id is write-once for every role (a trigger: the owner role could otherwise rewrite where
payouts go), and who pays Stripe's fees is fixed with the account (Stripe fixes it at creation).
The webhook arrives before any tenant is known, so stripe_account_tenant() is the one way from an
account id to its business, shaped like 0007's account_by_email.

Revision ID: 0043
Revises: 0042
"""

from alembic import op

from app.db import enable_tenant_isolation

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"


def upgrade() -> None:
    op.execute("""
    CREATE TABLE payment_accounts (
      tenant_id uuid CONSTRAINT pk_payment_accounts PRIMARY KEY
        CONSTRAINT fk_payment_accounts_tenant_id_tenants REFERENCES tenants (id),
      stripe_account_id text NOT NULL
        CONSTRAINT uq_payment_accounts_stripe_account_id UNIQUE
        CONSTRAINT ck_payment_accounts_stripe_account_id
          CHECK (stripe_account_id ~ '^acct_[A-Za-z0-9]{1,64}$'),
      charges_enabled boolean NOT NULL DEFAULT false,
      fee_mode text NOT NULL DEFAULT 'business_pays' CONSTRAINT ck_payment_accounts_fee_mode
        CHECK (fee_mode IN
               ('business_pays', 'business_pays_plus_fee', 'platform_pays')),
      application_fee_bp integer NOT NULL DEFAULT 0
        CONSTRAINT ck_payment_accounts_application_fee_bp
          CHECK (application_fee_bp BETWEEN 0 AND 10000),
      application_fee_fixed_minor integer NOT NULL DEFAULT 0
        CONSTRAINT ck_payment_accounts_application_fee_fixed_minor
          CHECK (application_fee_fixed_minor BETWEEN 0 AND 1000000),
      CONSTRAINT ck_payment_accounts_fee_only_in_mode_2
        CHECK (fee_mode = 'business_pays_plus_fee'
               OR (application_fee_bp = 0 AND application_fee_fixed_minor = 0)),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
    # referenced=False: nothing references payment_accounts, and the PK leads with tenant_id.
    enable_tenant_isolation("payment_accounts", referenced=False)
    # The REVOKE runs before the GRANTs (CONTRIBUTING.md). No DELETE: payouts follow this id.
    op.execute("REVOKE ALL ON payment_accounts FROM ziftbook_app")
    op.execute("GRANT SELECT ON payment_accounts TO ziftbook_app")
    op.execute(
        "GRANT INSERT (tenant_id, stripe_account_id, fee_mode) ON payment_accounts TO ziftbook_app"
    )
    op.execute("GRANT UPDATE (charges_enabled) ON payment_accounts TO ziftbook_app")
    # The future platform-admin route writes the fee policy; granted now so it needs no migration.
    # No owner or public route writes it.
    op.execute(
        "GRANT UPDATE (fee_mode, application_fee_bp, application_fee_fixed_minor) "
        "ON payment_accounts TO ziftbook_app"
    )

    op.execute(f"""
    CREATE FUNCTION payment_account_write_once() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER {SEARCH_PATH} AS $$
      BEGIN
        RAISE EXCEPTION 'a business''s Stripe account is set once'
          USING ERRCODE = '23514', CONSTRAINT = 'payment_account_write_once';
      END $$""")
    op.execute("""
    CREATE TRIGGER payment_account_write_once BEFORE UPDATE OF stripe_account_id, tenant_id
      ON payment_accounts
      FOR EACH ROW WHEN (NEW.stripe_account_id IS DISTINCT FROM OLD.stripe_account_id
                         OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id)
      EXECUTE FUNCTION payment_account_write_once()""")
    op.execute(f"""
    CREATE FUNCTION payment_account_fee_payer_fixed() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER {SEARCH_PATH} AS $$
      BEGIN
        RAISE EXCEPTION 'who pays Stripe''s fees is fixed when the account is created'
          USING ERRCODE = '23514', CONSTRAINT = 'payment_account_fee_payer_fixed';
      END $$""")
    op.execute("""
    CREATE TRIGGER payment_account_fee_payer_fixed BEFORE UPDATE OF fee_mode ON payment_accounts
      FOR EACH ROW WHEN (NEW.fee_mode IS DISTINCT FROM OLD.fee_mode
                         AND 'platform_pays' IN (OLD.fee_mode, NEW.fee_mode))
      EXECUTE FUNCTION payment_account_fee_payer_fixed()""")

    # FORCE RLS applies to the definer (ziftbook_migrate), so a plain SELECT would see nothing.
    # 0007's pattern: a migrate-only SELECT policy opened by a transaction-local flag that only this
    # function sets, and app.tenant_id parked on the nil uuid so tenant_isolation does not raise;
    # both are restored before returning.
    op.execute(
        "CREATE POLICY stripe_webhook ON payment_accounts FOR SELECT TO ziftbook_migrate "
        "USING (current_setting('app.stripe_webhook', true) = 'on')"
    )
    op.execute(f"""
    CREATE FUNCTION stripe_account_tenant(p_account text) RETURNS uuid
      LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
      DECLARE
        v_flag text := current_setting('app.stripe_webhook', true);
        v_tenant text := current_setting('app.tenant_id', true);
        v_found uuid;
      BEGIN
        PERFORM set_config('app.stripe_webhook', 'on', true);
        PERFORM set_config('app.tenant_id', '00000000-0000-0000-0000-000000000000', true);
        SELECT a.tenant_id INTO v_found FROM payment_accounts a
          WHERE a.stripe_account_id = p_account;
        PERFORM set_config('app.stripe_webhook', coalesce(v_flag, ''), true);
        PERFORM set_config('app.tenant_id', coalesce(v_tenant, ''), true);
        RETURN v_found;
      END $$""")
    op.execute("REVOKE EXECUTE ON FUNCTION stripe_account_tenant(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION stripe_account_tenant(text) TO ziftbook_app")


def downgrade() -> None:
    # Loses every business's connection to Stripe: each reconnects (the account itself lives on at
    # Stripe). Dropping the table drops its policies and triggers.
    op.execute("DROP FUNCTION stripe_account_tenant(text)")
    op.execute("DROP TABLE payment_accounts")
    op.execute("DROP FUNCTION payment_account_write_once()")
    op.execute("DROP FUNCTION payment_account_fee_payer_fixed()")
