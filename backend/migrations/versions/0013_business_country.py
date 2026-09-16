"""tenants.country and tenants.currency, chosen at sign-up; the app role may only rename a business.

A business's currency is fixed once it exists: prices will reference tenants (id, currency), and the
app role can no longer update it, nor delete a business. Existing businesses, and sign-ups from an
app that sends no country yet, are Dutch and charge in euros.

Apply this before the app that calls the five-argument complete_sign_up.

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
SIGNATURE = "complete_sign_up(bytea, text, text, text, text)"

# 0007's complete_sign_up, also storing the country and its currency. An overload, not a
# replacement: the running app calls the three-argument one until it is replaced. No DEFAULTs on
# the new parameters, or a three-argument call would match both functions.
# ponytail: drop complete_sign_up(bytea, text, text) in the PR that makes country required.
COMPLETE_SIGN_UP = f"""
CREATE FUNCTION complete_sign_up(p_token_hash bytea, p_password_hash text, p_business_name text,
                                 p_country text, p_currency text)
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
  INSERT INTO tenants AS n (name, country, currency)
  VALUES (p_business_name, p_country, p_currency) RETURNING n.id INTO v_tenant;
  INSERT INTO password_credentials (user_id, hash) VALUES (v_user, p_password_hash);
  v_previous := current_setting('app.tenant_id', true);
  PERFORM set_config('app.tenant_id', v_tenant::text, true);
  INSERT INTO memberships (tenant_id, user_id, role) VALUES (v_tenant, v_user, 'owner');
  PERFORM set_config('app.tenant_id', coalesce(v_previous, ''), true);
  RETURN QUERY SELECT 'created', v_tenant, v_user;
END $$"""


def upgrade() -> None:
    # Constant defaults: no table rewrite. Names follow app.db's naming convention.
    op.execute("""
    ALTER TABLE tenants
      ADD COLUMN country text NOT NULL DEFAULT 'NL'
        CONSTRAINT ck_tenants_country CHECK (country ~ '^[A-Z]{2}$'),
      ADD COLUMN currency text NOT NULL DEFAULT 'EUR'
        CONSTRAINT ck_tenants_currency CHECK (currency ~ '^[A-Z]{3}$'),
      ADD CONSTRAINT uq_tenants_id_currency UNIQUE (id, currency)
    """)
    # Revoke first: revoking a table privilege also takes it away from every column.
    op.execute("REVOKE UPDATE, DELETE ON tenants FROM ziftbook_app")
    # UPDATE on one column is also what SELECT ... FOR NO KEY UPDATE needs (ZIF-27's owner guard).
    op.execute("GRANT UPDATE (name) ON tenants TO ziftbook_app")
    op.execute(COMPLETE_SIGN_UP)
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO ziftbook_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")
    op.execute("REVOKE UPDATE (name) ON tenants FROM ziftbook_app")
    op.execute("GRANT UPDATE, DELETE ON tenants TO ziftbook_app")
    # Businesses lose their country and currency; a new upgrade makes every one NL/EUR again.
    op.execute(
        "ALTER TABLE tenants DROP CONSTRAINT uq_tenants_id_currency, "
        "DROP COLUMN currency, DROP COLUMN country"
    )
