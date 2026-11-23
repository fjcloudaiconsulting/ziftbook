"""Share signup creation with the admin console's passwordless owner onboarding (ZIF-297).

The internal helper has no app grant; the admin wrapper checks the live second factor in SQL.
Downgrade reverses the anchored signup patch before dropping the new functions.

Revision ID: 0108
Revises: 0107
"""

import sqlalchemy as sa
from alembic import op

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None

SIGN_UP = "complete_sign_up(bytea,text,text,text,text,text)"
CREATE = "create_business_owner(text,text,text,text,text,text,text)"
ADMIN = "admin_create_business(bytea,text,text,text,text,text,text)"
OLD = """  -- ON CONFLICT: a second link for the same email, completed at the same time, waits here.
  INSERT INTO users AS u (id, email, locale, name) VALUES (uuidv7(), t.email, t.locale, p_name)
  ON CONFLICT (email) DO NOTHING RETURNING u.id INTO v_user;
  IF v_user IS NULL THEN
    RETURN QUERY SELECT 'already_registered', NULL::uuid, NULL::uuid;
    RETURN;
  END IF;
  INSERT INTO tenants AS n (name, country, currency)
  VALUES (p_business_name, p_country, p_currency) RETURNING n.id INTO v_tenant;
  INSERT INTO password_credentials (user_id, hash) VALUES (v_user, p_password_hash);
  v_previous := current_setting('app.tenant_id', true);
  PERFORM set_config('app.tenant_id', v_tenant::text, true);
  INSERT INTO memberships (tenant_id, user_id, role, display_name)
  VALUES (v_tenant, v_user, 'owner', p_name);
  PERFORM set_config('app.tenant_id', coalesce(v_previous, ''), true);
  RETURN QUERY SELECT 'created', v_tenant, v_user;"""
NEW = """  RETURN QUERY SELECT * FROM create_business_owner(
    t.email, t.locale, p_password_hash, p_business_name, p_country, p_currency, p_name);"""


def edit(old: str, new: str) -> None:
    bind = op.get_bind()
    definition = bind.scalar(
        sa.text("SELECT pg_get_functiondef(to_regprocedure(:signature))"), {"signature": SIGN_UP}
    )
    if definition is None or definition.count(old) != 1:
        raise RuntimeError("0108: expected signup creation fragment exactly once")
    bind.exec_driver_sql(definition.replace(old, new))


def upgrade() -> None:
    op.execute("""
CREATE FUNCTION create_business_owner(
  p_email text, p_locale text, p_password_hash text, p_business_name text,
  p_country text, p_currency text, p_name text
) RETURNS TABLE (outcome text, tenant_id uuid, user_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_user uuid;
  v_tenant uuid;
  v_previous text;
BEGIN
  -- ON CONFLICT: a second link for the same email, completed at the same time, waits here.
  INSERT INTO users AS u (id, email, locale, name) VALUES (uuidv7(), p_email, p_locale, p_name)
  ON CONFLICT (email) DO NOTHING RETURNING u.id INTO v_user;
  IF v_user IS NULL THEN
    RETURN QUERY SELECT 'already_registered', NULL::uuid, NULL::uuid;
    RETURN;
  END IF;
  INSERT INTO tenants AS n (name, country, currency)
  VALUES (p_business_name, p_country, p_currency) RETURNING n.id INTO v_tenant;
  IF p_password_hash IS NOT NULL THEN
    INSERT INTO password_credentials (user_id, hash) VALUES (v_user, p_password_hash);
  END IF;
  v_previous := current_setting('app.tenant_id', true);
  PERFORM set_config('app.tenant_id', v_tenant::text, true);
  INSERT INTO memberships (tenant_id, user_id, role, display_name)
  VALUES (v_tenant, v_user, 'owner', p_name);
  PERFORM set_config('app.tenant_id', coalesce(v_previous, ''), true);
  RETURN QUERY SELECT 'created', v_tenant, v_user;
END $$""")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {CREATE} FROM PUBLIC")
    edit(OLD, NEW)
    op.execute("""
CREATE FUNCTION admin_create_business(
  p_id_hash bytea, p_email text, p_locale text, p_business_name text,
  p_country text, p_currency text, p_name text
) RETURNS TABLE (outcome text, tenant_id uuid, user_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF live_admin_user(p_id_hash) IS NULL OR NOT EXISTS (
    SELECT FROM admin_sessions s
    WHERE s.id_hash = p_id_hash AND s.second_factor_at > clock_timestamp() - interval '15 minutes'
  ) THEN
    RETURN QUERY SELECT 'stale'::text, NULL::uuid, NULL::uuid;
    RETURN;
  END IF;
  RETURN QUERY SELECT * FROM create_business_owner(
    p_email, p_locale, NULL, p_business_name, p_country, p_currency, p_name);
END $$""")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {ADMIN} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {ADMIN} TO ziftbook_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {ADMIN}")
    edit(NEW, OLD)
    op.execute(f"DROP FUNCTION {CREATE}")
