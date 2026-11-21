"""Plans, trial, overrides, offers, grants and subscriptions (ZIF-78).

- plans (global): what a business can use, as `values` read against the entitlements registry.
  stripe_price_id is NULL for a plan nobody pays for (Free). Archived, never deleted: only
  ziftbook_migrate may delete one, and subscriptions, offers and grants reference it.
- platform_billing (global, one row): the default plan (Free), and the trial's plan and days.
- tenant_billing (one row per business): when its trial ends, and since when it has had more
  members than its plan's places. The tenant_billing_on_tenant trigger writes the row whenever a
  business is created (complete_sign_up, or an operator), copying the trial from platform_billing.
- subscriptions: a business's Stripe subscription, written by the worker from Stripe only.
- business_overrides, plan_offers, plan_grants: an admin's per-business exceptions, each with the
  admin and the reason. Their writers are definer functions that come with the admin routes.

The app role reads every new table and writes none of them; the worker writes subscriptions and
tenant_billing.over_limit_since. The memberships columns are 0104's.

stripe_subscription_id is unique across businesses, not per business as CONTRIBUTING asks of a
tenant-owned table's keys: a Stripe id names one subscription, so a webhook must find exactly one
business for it, and only the worker writes it, from Stripe (payment_accounts' account id, 0043).

Existing businesses get a trial ending 30 days from now: until an operator sets a trial plan, it
resolves to the default plan, so nothing changes for them on deploy.

Lock: the new tables' foreign keys and the trigger take SHARE ROW EXCLUSIVE on tenants until
commit, which holds off a sign-up until the backfill has run. No other existing table is locked:
complete_sign_up inserts a tenant and then a membership, so a migration holding tenants that then
waited for memberships could deadlock with a sign-up mid-deploy. The memberships columns are
therefore 0104, in a transaction of their own (migrations/env.py commits each revision).

The downgrade drops everything this added, with the billing state in it.

Revision ID: 0103
Revises: 0102
"""

from alembic import op

from app.db import enable_tenant_isolation

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
# The reason an admin gives for a per-business exception: what platform_audit_events records too.
REASON = "reason text NOT NULL CHECK (char_length(reason) <= 500 AND reason ~ '[^[:space:]]')"
# The admin who made it. No foreign key, as platform_audit_events.actor_user_id: erasing that
# admin's account must not be blocked by, or remove, the record.
ADMIN = "admin_user_id uuid NOT NULL"
TENANT_TABLES = (
    "tenant_billing",
    "subscriptions",
    "business_overrides",
    "plan_offers",
    "plan_grants",
)


def upgrade() -> None:
    op.execute("""
    CREATE TABLE plans (
      id uuid CONSTRAINT pk_plans PRIMARY KEY DEFAULT uuidv7(),
      slug text NOT NULL CONSTRAINT uq_plans_slug UNIQUE
        CONSTRAINT ck_plans_slug CHECK (slug ~ '^[a-z0-9-]{1,40}$'),
      name jsonb NOT NULL CONSTRAINT ck_plans_name CHECK (
        jsonb_typeof(name) = 'object' AND name - 'en' - 'nl' - 'pt' = '{}'::jsonb
        AND jsonb_typeof(name -> 'en') = 'string' AND name ->> 'en' ~ '[^[:space:]]'
        AND jsonb_typeof(name -> 'nl') = 'string' AND name ->> 'nl' ~ '[^[:space:]]'
        AND jsonb_typeof(name -> 'pt') = 'string' AND name ->> 'pt' ~ '[^[:space:]]'),
      stripe_price_id text CONSTRAINT uq_plans_stripe_price_id UNIQUE
        CONSTRAINT ck_plans_stripe_price_id
          CHECK (stripe_price_id ~ '^price_[A-Za-z0-9]{1,64}$'),
      values jsonb NOT NULL DEFAULT '{}'
        CONSTRAINT ck_plans_values CHECK (jsonb_typeof(values) = 'object'),
      public boolean NOT NULL DEFAULT false,
      archived_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""")
    op.execute("""
    CREATE TABLE platform_billing (
      id boolean CONSTRAINT pk_platform_billing PRIMARY KEY DEFAULT true
        CONSTRAINT ck_platform_billing_one_row CHECK (id),
      default_plan_id uuid NOT NULL
        CONSTRAINT fk_platform_billing_default_plan_id_plans REFERENCES plans (id),
      -- NULL: no trial.
      trial_plan_id uuid CONSTRAINT fk_platform_billing_trial_plan_id_plans REFERENCES plans (id),
      trial_days integer NOT NULL DEFAULT 0
        CONSTRAINT ck_platform_billing_trial_days CHECK (trial_days BETWEEN 0 AND 90)
    )""")

    op.execute("""
    CREATE TABLE tenant_billing (
      -- Written by the trigger below for every business, so it goes with its business.
      tenant_id uuid CONSTRAINT pk_tenant_billing PRIMARY KEY
        CONSTRAINT fk_tenant_billing_tenant_id_tenants REFERENCES tenants (id) ON DELETE CASCADE,
      trial_ends_at timestamptz,
      over_limit_since timestamptz
    )""")
    op.execute("""
    CREATE TABLE subscriptions (
      tenant_id uuid CONSTRAINT pk_subscriptions PRIMARY KEY
        CONSTRAINT fk_subscriptions_tenant_id_tenants REFERENCES tenants (id),
      stripe_customer_id text NOT NULL CONSTRAINT ck_subscriptions_stripe_customer_id
        CHECK (stripe_customer_id ~ '^cus_[A-Za-z0-9]{1,64}$'),
      stripe_subscription_id text NOT NULL
        CONSTRAINT uq_subscriptions_stripe_subscription_id UNIQUE
        CONSTRAINT ck_subscriptions_stripe_subscription_id
          CHECK (stripe_subscription_id ~ '^sub_[A-Za-z0-9]{1,64}$'),
      -- NULL: a Price no plan carries.
      plan_id uuid CONSTRAINT fk_subscriptions_plan_id_plans REFERENCES plans (id),
      -- Stripe's, as it sends it: a status it adds later must not fail the webhook.
      status text NOT NULL,
      current_period_end timestamptz,
      cancel_at_period_end boolean NOT NULL DEFAULT false,
      updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""")
    op.execute(f"""
    CREATE TABLE business_overrides (
      id uuid CONSTRAINT pk_business_overrides PRIMARY KEY DEFAULT uuidv7(),
      tenant_id uuid NOT NULL
        CONSTRAINT fk_business_overrides_tenant_id_tenants REFERENCES tenants (id),
      key text NOT NULL,
      value jsonb NOT NULL,
      expires_at timestamptz,
      {REASON},
      {ADMIN},
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      CONSTRAINT uq_business_overrides_tenant_id_key UNIQUE (tenant_id, key)
    )""")
    op.execute(f"""
    CREATE TABLE plan_offers (
      id uuid CONSTRAINT pk_plan_offers PRIMARY KEY DEFAULT uuidv7(),
      tenant_id uuid NOT NULL CONSTRAINT fk_plan_offers_tenant_id_tenants REFERENCES tenants (id),
      plan_id uuid NOT NULL CONSTRAINT fk_plan_offers_plan_id_plans REFERENCES plans (id),
      stripe_coupon_id text,
      {REASON},
      {ADMIN},
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      CONSTRAINT uq_plan_offers_tenant_id_plan_id UNIQUE (tenant_id, plan_id)
    )""")
    op.execute(f"""
    CREATE TABLE plan_grants (
      tenant_id uuid CONSTRAINT pk_plan_grants PRIMARY KEY
        CONSTRAINT fk_plan_grants_tenant_id_tenants REFERENCES tenants (id),
      plan_id uuid NOT NULL CONSTRAINT fk_plan_grants_plan_id_plans REFERENCES plans (id),
      -- NULL: open-ended.
      ends_at timestamptz,
      {REASON},
      {ADMIN},
      created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""")
    # referenced=False where the primary key is tenant_id: one row per business, nothing refers
    # to it.
    enable_tenant_isolation("tenant_billing", referenced=False)
    enable_tenant_isolation("subscriptions", referenced=False)
    enable_tenant_isolation("business_overrides")
    enable_tenant_isolation("plan_offers")
    enable_tenant_isolation("plan_grants", referenced=False)

    # 0001's default privileges give the app role DML on every new table; it only reads these.
    # No DELETE for any runtime role: a plan is archived, the rest go through definer functions.
    for table in ("plans", "platform_billing", *TENANT_TABLES):
        op.execute(f"REVOKE ALL ON {table} FROM ziftbook_app")
        op.execute(f"GRANT SELECT ON {table} TO ziftbook_app")
    op.execute("GRANT INSERT, UPDATE ON subscriptions TO ziftbook_worker")
    op.execute("GRANT UPDATE (over_limit_since) ON tenant_billing TO ziftbook_worker")

    op.execute("""
    INSERT INTO plans (slug, name, values, public) VALUES
      ('free', '{"en": "Free", "nl": "Gratis", "pt": "Grátis"}',
       '{"places": 1, "email_reminders": true}', true)""")
    op.execute(
        "INSERT INTO platform_billing (default_plan_id) SELECT id FROM plans WHERE slug = 'free'"
    )

    # SECURITY DEFINER: the row is written whoever inserts the business, and tenant_billing has no
    # write grant. FORCE row-level security binds the definer too, so the new business's tenant is
    # set for the insert and the caller's put back after.
    op.execute(f"""
    CREATE FUNCTION tenant_billing_on_tenant() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
      DECLARE
        v_tenant text := current_setting('app.tenant_id', true);
      BEGIN
        PERFORM set_config('app.tenant_id', NEW.id::text, true);
        INSERT INTO tenant_billing (tenant_id, trial_ends_at) VALUES (NEW.id, (
          SELECT now() + make_interval(days => b.trial_days) FROM platform_billing b
          WHERE b.trial_plan_id IS NOT NULL AND b.trial_days > 0));
        PERFORM set_config('app.tenant_id', coalesce(v_tenant, ''), true);
        RETURN NULL;
      END $$""")
    # Firing a trigger checks no EXECUTE privilege (0030), so no role needs it.
    op.execute("REVOKE EXECUTE ON FUNCTION tenant_billing_on_tenant() FROM PUBLIC")
    op.execute("""
    CREATE TRIGGER tenant_billing_on_tenant AFTER INSERT ON tenants
      FOR EACH ROW EXECUTE FUNCTION tenant_billing_on_tenant()""")
    # After the trigger: its lock on tenants holds off a new business until this commits, so every
    # business either gets its row here or from the trigger.
    op.execute("""
    DO $$
    DECLARE v_t uuid;
    BEGIN
      FOR v_t IN SELECT t.id FROM tenants t ORDER BY t.id LOOP
        PERFORM set_config('app.tenant_id', v_t::text, true);
        INSERT INTO tenant_billing (tenant_id, trial_ends_at)
          VALUES (v_t, now() + interval '30 days');
      END LOOP;
      PERFORM set_config('app.tenant_id', '', true);
    END $$""")


def downgrade() -> None:
    op.execute("DROP TRIGGER tenant_billing_on_tenant ON tenants")
    op.execute("DROP FUNCTION tenant_billing_on_tenant()")
    op.execute(f"DROP TABLE {', '.join(TENANT_TABLES)}, platform_billing, plans")
