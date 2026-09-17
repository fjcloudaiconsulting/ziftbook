"""Drop the 3-argument complete_sign_up, dead since ZIF-96 made country required.

Contract step for ZIF-96 (#90): 0013 kept 0007's three-argument complete_sign_up beside the new
five-argument one so an app that didn't yet send a country could still sign up, with the plan to
drop it once country became required. It's now required, and app/accounts.py calls only the
five-argument function, so this is releasable immediately: no running version calls the one
dropped here.

Revision ID: 0022
Revises: 0021
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
SIGNATURE = "complete_sign_up(bytea, text, text)"

# 0007's complete_sign_up, byte for byte: 0013 added a five-argument overload beside it and never
# touched this one's body or grants.
COMPLETE_SIGN_UP = f"""
CREATE FUNCTION complete_sign_up(p_token_hash bytea, p_password_hash text, p_business_name text)
RETURNS TABLE (outcome text, tenant_id uuid, user_id uuid)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Uses up the sign-up link and creates the business, its owner and their password together.
DECLARE
  t email_tokens;
  v_user uuid;
  v_tenant uuid;
  v_previous text;
BEGIN
  DELETE FROM email_tokens e
  WHERE e.token_hash = p_token_hash AND e.purpose = 'sign_up' AND e.expires_at > now()
  RETURNING * INTO t;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'invalid_token', NULL::uuid, NULL::uuid;
    RETURN;
  END IF;
  -- ON CONFLICT: a second link for the same email, completed at the same time, waits here.
  INSERT INTO users AS u (id, email, locale) VALUES (uuidv7(), t.email, t.locale)
  ON CONFLICT (email) DO NOTHING RETURNING u.id INTO v_user;
  IF v_user IS NULL THEN
    RETURN QUERY SELECT 'already_registered', NULL::uuid, NULL::uuid;
    RETURN;
  END IF;
  INSERT INTO tenants AS n (name) VALUES (p_business_name) RETURNING n.id INTO v_tenant;
  INSERT INTO password_credentials (user_id, hash) VALUES (v_user, p_password_hash);
  v_previous := current_setting('app.tenant_id', true);
  PERFORM set_config('app.tenant_id', v_tenant::text, true);
  INSERT INTO memberships (tenant_id, user_id, role) VALUES (v_tenant, v_user, 'owner');
  PERFORM set_config('app.tenant_id', coalesce(v_previous, ''), true);
  RETURN QUERY SELECT 'created', v_tenant, v_user;
END $$"""


def upgrade() -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")


def downgrade() -> None:
    op.execute(COMPLETE_SIGN_UP)
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO ziftbook_app")
