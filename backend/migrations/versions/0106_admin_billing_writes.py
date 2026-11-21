"""Admins edit plans, the billing settings and a business's overrides, offers and grant (ZIF-78).

The writers of 0103's tables that the app role only reads: nine SECURITY DEFINER functions, each
re-checking the admin's live session and a second factor passed in the last 15 minutes by the
database's clock, as 0100's do, so a caller that skipped the route guard gets 'stale'. Each answers
an outcome: ok, stale, not_found or conflict (and in_use and mismatch, below).

- admin_save_plan inserts (p_id NULL) or updates a plan; a slug or Price another plan has is a
  conflict. Its Price never changes while any subscription is on the plan ('in_use'): Stripe keeps
  a subscriber on the Price they bought, so a new price for new customers is a new plan.
  admin_archive_plan archives one, never the default or the trial plan (a conflict).
- admin_set_billing sets the default plan, the trial plan and the trial's days; an archived plan
  is a conflict. It and admin_archive_plan lock platform_billing first, so neither can slip past
  the other's check.
- admin_set_override / admin_remove_override, admin_offer_plan / admin_withdraw_offer and
  admin_grant_plan / admin_end_grant act on p_tenant's rows, called inside that business (forced
  row-level security binds the definer too): an app.tenant_id other than p_tenant answers
  'mismatch' and writes nothing. An archived plan is never offered or given, nor a plan with no
  Price offered (nothing to check out).

Values are checked by the API against the registry (entitlements.validate) first; here, again, each
must be a boolean or a non-negative whole number written without a fraction: a stored 5.0 reads
back as a float and would fail every entitlements read. Anything else raises 22023.

The downgrade drops the nine functions.

Revision ID: 0106
Revises: 0105
"""

from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
# 0100's guard. clock_timestamp(), not now(): now() was fixed when the transaction began.
STALE = """live_admin_user(p_id_hash) IS NULL OR NOT EXISTS (
    SELECT FROM admin_sessions s
    WHERE s.id_hash = p_id_hash AND s.second_factor_at > clock_timestamp() - interval '15 minutes'
  )"""
# A boolean, or digits only: no sign, fraction or exponent.
VALUE_OK = (
    "(jsonb_typeof({v}) = 'boolean' OR (jsonb_typeof({v}) = 'number' AND {v}::text ~ '^[0-9]+$'))"
)
REFUSE_VALUE = """RAISE EXCEPTION 'a value must be a boolean or a whole number'
      USING ERRCODE = 'invalid_parameter_value';"""


def plain(name: str, params: str, body: str) -> str:
    """A function on the platform's own tables (no row-level security)."""
    return f"""
CREATE FUNCTION {name}(p_id_hash bytea, {params}) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {SEARCH_PATH} AS $$
BEGIN
  IF {STALE} THEN
    RETURN 'stale';
  END IF;
{body}
END $$"""


def per_business(name: str, params: str, body: str) -> str:
    """A function on one business's rows, called inside that business: body sets v_outcome."""
    return f"""
CREATE FUNCTION {name}(p_id_hash bytea, p_tenant uuid{params}) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {SEARCH_PATH} AS $$
DECLARE
  v_admin uuid := live_admin_user(p_id_hash);
  v_outcome text;
BEGIN
  IF {STALE} THEN
    RETURN 'stale';
  END IF;
  -- The caller's transaction must be in p_tenant: a mix-up writes nothing.
  IF p_tenant::text IS DISTINCT FROM current_setting('app.tenant_id', true) THEN
    RETURN 'mismatch';
  END IF;
  -- No row-level security on tenants: a plain existence check.
  IF NOT EXISTS (SELECT FROM tenants t WHERE t.id = p_tenant) THEN
    RETURN 'not_found';
  END IF;
{body}
  RETURN v_outcome;
END $$"""


# A plan that exists and is not archived, else the outcome; p_plan locked FOR SHARE so an archive
# waits for this transaction rather than slipping between the check and the write.
USABLE_PLAN = """  SELECT p.archived_at, p.stripe_price_id INTO v_archived, v_price
  FROM plans p WHERE p.id = p_plan FOR SHARE;
  IF NOT FOUND THEN
    v_outcome := 'not_found';
  ELSIF v_archived IS NOT NULL{also} THEN
    v_outcome := 'conflict';
  ELSE"""

FUNCTIONS = {
    "admin_save_plan(bytea, uuid, text, jsonb, text, jsonb, boolean)": f"""
CREATE FUNCTION admin_save_plan(
  p_id_hash bytea, p_id uuid, p_slug text, p_name jsonb, p_price text, p_values jsonb,
  p_public boolean
) RETURNS TABLE (outcome text, id uuid)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {SEARCH_PATH} AS $$
-- Every column is table-aliased: the OUT parameters would make bare names ambiguous (42702).
DECLARE
  v_id uuid;
  v_price text;
  v_caller text := current_setting('app.tenant_id', true);
  v_tenant uuid;
  v_subscribed boolean := false;
BEGIN
  IF {STALE} THEN
    RETURN QUERY SELECT 'stale'::text, NULL::uuid;
    RETURN;
  END IF;
  IF jsonb_typeof(p_values) IS DISTINCT FROM 'object' OR EXISTS (
    SELECT FROM jsonb_each(p_values) e WHERE NOT {VALUE_OK.format(v="e.value")}
  ) THEN
    {REFUSE_VALUE}
  END IF;
  BEGIN
    IF p_id IS NULL THEN
      INSERT INTO plans AS p (slug, name, stripe_price_id, values, public)
      VALUES (p_slug, p_name, p_price, p_values, p_public)
      RETURNING p.id INTO v_id;
    ELSE
      -- FOR UPDATE, not the UPDATE's NO KEY UPDATE: it conflicts with the KEY SHARE a subscription
      -- insert's foreign key takes, so none can land on this plan between the check and the write.
      SELECT p.stripe_price_id INTO v_price FROM plans p WHERE p.id = p_id FOR UPDATE;
      IF NOT FOUND THEN
        RETURN QUERY SELECT 'not_found'::text, NULL::uuid;
        RETURN;
      END IF;
      IF v_price IS DISTINCT FROM p_price THEN
        -- subscriptions is under forced row-level security: looked at one business at a time.
        -- shortcut: one statement per business on a Price change only; a set-based check needs a
        -- role that bypasses row-level security, which this repository never grants.
        FOR v_tenant IN SELECT t.id FROM tenants t ORDER BY t.id LOOP
          PERFORM set_config('app.tenant_id', v_tenant::text, true);
          v_subscribed := EXISTS (SELECT FROM subscriptions s WHERE s.plan_id = p_id);
          EXIT WHEN v_subscribed;
        END LOOP;
        PERFORM set_config('app.tenant_id', coalesce(v_caller, ''), true);
        IF v_subscribed THEN
          RETURN QUERY SELECT 'in_use'::text, NULL::uuid;
          RETURN;
        END IF;
      END IF;
      UPDATE plans p SET slug = p_slug, name = p_name, stripe_price_id = p_price,
        values = p_values, public = p_public, updated_at = clock_timestamp()
      WHERE p.id = p_id
      RETURNING p.id INTO v_id;
    END IF;
  EXCEPTION WHEN unique_violation THEN
    RETURN QUERY SELECT 'conflict'::text, NULL::uuid;
    RETURN;
  END;
  RETURN QUERY SELECT 'ok'::text, v_id;
END $$""",
    "admin_archive_plan(bytea, uuid)": plain(
        "admin_archive_plan",
        "p_id uuid",
        """  PERFORM FROM platform_billing FOR UPDATE;
  IF NOT EXISTS (SELECT FROM plans p WHERE p.id = p_id) THEN
    RETURN 'not_found';
  END IF;
  IF EXISTS (
    SELECT FROM platform_billing b WHERE p_id IN (b.default_plan_id, b.trial_plan_id)
  ) THEN
    RETURN 'conflict';
  END IF;
  -- Archiving again keeps the first time. Subscribers, offers and grants stay as they are.
  UPDATE plans p SET archived_at = coalesce(p.archived_at, clock_timestamp()),
    updated_at = clock_timestamp()
  WHERE p.id = p_id;
  RETURN 'ok';""",
    ),
    "admin_set_billing(bytea, uuid, uuid, integer)": plain(
        "admin_set_billing",
        "p_default uuid, p_trial uuid, p_days integer",
        """  PERFORM FROM platform_billing FOR UPDATE;
  IF NOT EXISTS (SELECT FROM plans p WHERE p.id = p_default)
     OR (p_trial IS NOT NULL AND NOT EXISTS (SELECT FROM plans p WHERE p.id = p_trial)) THEN
    RETURN 'not_found';
  END IF;
  IF EXISTS (
    SELECT FROM plans p WHERE p.id IN (p_default, p_trial) AND p.archived_at IS NOT NULL
  ) THEN
    RETURN 'conflict';
  END IF;
  UPDATE platform_billing b
  SET default_plan_id = p_default, trial_plan_id = p_trial, trial_days = p_days;
  RETURN 'ok';""",
    ),
    "admin_set_override(bytea, uuid, text, jsonb, timestamptz, text)": per_business(
        "admin_set_override",
        ", p_key text, p_value jsonb, p_expires timestamptz, p_reason text",
        f"""  IF NOT {VALUE_OK.format(v="p_value")} THEN
    {REFUSE_VALUE}
  END IF;
  INSERT INTO business_overrides AS o
    (tenant_id, key, value, expires_at, reason, admin_user_id)
  VALUES (p_tenant, p_key, p_value, p_expires, p_reason, v_admin)
  ON CONFLICT (tenant_id, key) DO UPDATE SET value = excluded.value,
    expires_at = excluded.expires_at, reason = excluded.reason,
    admin_user_id = excluded.admin_user_id;
  v_outcome := 'ok';""",
    ),
    "admin_remove_override(bytea, uuid, text)": per_business(
        "admin_remove_override",
        ", p_key text",
        """  DELETE FROM business_overrides o WHERE o.tenant_id = p_tenant AND o.key = p_key;
  v_outcome := CASE WHEN FOUND THEN 'ok' ELSE 'not_found' END;""",
    ),
    "admin_offer_plan(bytea, uuid, uuid, text, text)": per_business(
        "admin_offer_plan",
        ", p_plan uuid, p_coupon text, p_reason text",
        f"""  DECLARE
    v_archived timestamptz;
    v_price text;
  BEGIN
{USABLE_PLAN.format(also=" OR v_price IS NULL")}
    INSERT INTO plan_offers AS o (tenant_id, plan_id, stripe_coupon_id, reason, admin_user_id)
    VALUES (p_tenant, p_plan, p_coupon, p_reason, v_admin)
    ON CONFLICT (tenant_id, plan_id) DO UPDATE SET stripe_coupon_id = excluded.stripe_coupon_id,
      reason = excluded.reason, admin_user_id = excluded.admin_user_id;
    v_outcome := 'ok';
  END IF;
  END;""",
    ),
    "admin_withdraw_offer(bytea, uuid, uuid)": per_business(
        "admin_withdraw_offer",
        ", p_plan uuid",
        """  -- A subscription already on the plan stays: only the offer goes.
  DELETE FROM plan_offers o WHERE o.tenant_id = p_tenant AND o.plan_id = p_plan;
  v_outcome := CASE WHEN FOUND THEN 'ok' ELSE 'not_found' END;""",
    ),
    "admin_grant_plan(bytea, uuid, uuid, timestamptz, text)": per_business(
        "admin_grant_plan",
        ", p_plan uuid, p_ends timestamptz, p_reason text",
        f"""  DECLARE
    v_archived timestamptz;
    v_price text;
  BEGIN
{USABLE_PLAN.format(also="")}
    INSERT INTO plan_grants AS g (tenant_id, plan_id, ends_at, reason, admin_user_id)
    VALUES (p_tenant, p_plan, p_ends, p_reason, v_admin)
    ON CONFLICT (tenant_id) DO UPDATE SET plan_id = excluded.plan_id,
      ends_at = excluded.ends_at, reason = excluded.reason,
      admin_user_id = excluded.admin_user_id, created_at = clock_timestamp();
    v_outcome := 'ok';
  END IF;
  END;""",
    ),
    "admin_end_grant(bytea, uuid)": per_business(
        "admin_end_grant",
        "",
        """  -- Its record is the audit row: the effective plan falls through at once.
  DELETE FROM plan_grants g WHERE g.tenant_id = p_tenant;
  v_outcome := CASE WHEN FOUND THEN 'ok' ELSE 'not_found' END;""",
    ),
}


def upgrade() -> None:
    for signature, definition in FUNCTIONS.items():
        op.execute(definition)
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")


def downgrade() -> None:
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
