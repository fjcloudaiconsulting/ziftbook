"""Password credentials, email tokens, and the functions that are the app's only way to them.

The app role can't read or write password_credentials or email_tokens. It calls these SECURITY
DEFINER functions (owned by ziftbook_migrate), and none of them changes a password without a token.
Each pins search_path with pg_temp last, so a caller's temporary table can't stand in for a table.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

FUNCTIONS = {
    "account_by_email(text)": f"""
CREATE FUNCTION account_by_email(p_email text)
RETURNS TABLE (user_id uuid, password_hash text, tenant_id uuid)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Sign-in, before a tenant is known: the user, their hash and their oldest membership's tenant.
-- A function-level SET of these settings needs superuser, so they are set and restored here.
DECLARE
  v_sign_in text := current_setting('app.sign_in', true);
  v_tenant text := current_setting('app.tenant_id', true);
BEGIN
  PERFORM set_config('app.sign_in', 'on', true);
  -- tenant_isolation must not raise; this tenant matches no row.
  PERFORM set_config('app.tenant_id', '00000000-0000-0000-0000-000000000000', true);
  RETURN QUERY
    SELECT u.id, c.hash,
           (SELECT m.tenant_id FROM memberships m WHERE m.user_id = u.id ORDER BY m.id LIMIT 1)
    FROM users u LEFT JOIN password_credentials c ON c.user_id = u.id
    WHERE u.email = p_email;
  PERFORM set_config('app.sign_in', coalesce(v_sign_in, ''), true);
  PERFORM set_config('app.tenant_id', coalesce(v_tenant, ''), true);
END $$""",
    "start_email_token(text,text,text)": f"""
CREATE FUNCTION start_email_token(p_purpose text, p_email text, p_locale text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- A pending request, whether or not an account exists; the email job mints its token.
DECLARE v_id uuid;
BEGIN
  -- ponytail: purges on every request; move to the ZIF-5 sweeper.
  DELETE FROM email_tokens e WHERE e.id IN (
    SELECT x.id FROM email_tokens x WHERE x.expires_at <= now()
    LIMIT 100 FOR UPDATE SKIP LOCKED);
  INSERT INTO email_tokens (purpose, email, locale, expires_at)
  VALUES (p_purpose, p_email, p_locale, now() + interval '1 hour')
  RETURNING id INTO v_id;
  RETURN v_id;
END $$""",
    "mint_email_token(uuid,bytea)": f"""
CREATE FUNCTION mint_email_token(p_id uuid, p_token_hash bytea)
RETURNS TABLE (purpose text, email text, locale text, registered boolean)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Called by the email job with the hash of the token it is about to send. No row: send nothing.
-- registered: a sign-up for an email that already has an account; the row is gone and the email
-- says to sign in instead, with no link. Minting again replaces the hash: the newest link works.
DECLARE
  t email_tokens;
  v_locale text;
BEGIN
  SELECT * INTO t FROM email_tokens e WHERE e.id = p_id AND e.expires_at > now() FOR UPDATE;
  IF NOT FOUND THEN RETURN; END IF;
  SELECT u.locale INTO v_locale FROM users u WHERE u.email = t.email;
  IF FOUND AND t.purpose = 'sign_up' THEN
    DELETE FROM email_tokens e WHERE e.id = p_id;
    RETURN QUERY SELECT t.purpose, t.email, coalesce(v_locale, t.locale), true;
    RETURN;
  END IF;
  IF NOT FOUND AND t.purpose = 'password_reset' THEN
    DELETE FROM email_tokens e WHERE e.id = p_id;
    RETURN;
  END IF;
  UPDATE email_tokens e SET token_hash = p_token_hash,
    expires_at = now() + CASE t.purpose WHEN 'sign_up' THEN interval '24 hours'
                                        ELSE interval '15 minutes' END
  WHERE e.id = p_id;
  RETURN QUERY SELECT t.purpose, t.email, coalesce(v_locale, t.locale), false;
END $$""",
    "email_token_live(bytea,text)": f"""
CREATE FUNCTION email_token_live(p_token_hash bytea, p_purpose text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER {SEARCH_PATH} AS $$
  -- Lets an endpoint turn away a dead link before it spends a password hash.
  SELECT EXISTS (SELECT FROM email_tokens e WHERE e.token_hash = p_token_hash
                 AND e.purpose = p_purpose AND e.expires_at > now())
$$""",
    "complete_sign_up(bytea,text,text)": f"""
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
END $$""",
    "complete_password_reset(bytea,text)": f"""
CREATE FUNCTION complete_password_reset(p_token_hash bytea, p_password_hash text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Uses up the reset link, sets the password, and signs the user out everywhere.
DECLARE
  t email_tokens;
  v_user uuid;
BEGIN
  DELETE FROM email_tokens e
  WHERE e.token_hash = p_token_hash AND e.purpose = 'password_reset' AND e.expires_at > now()
  RETURNING * INTO t;
  IF NOT FOUND THEN RETURN false; END IF;
  SELECT u.id INTO v_user FROM users u WHERE u.email = t.email;
  IF NOT FOUND THEN RETURN false; END IF;
  INSERT INTO password_credentials AS c (user_id, hash) VALUES (v_user, p_password_hash)
  ON CONFLICT (user_id) DO UPDATE SET hash = excluded.hash;
  DELETE FROM sessions s WHERE s.user_id = v_user;
  -- SKIP LOCKED: a second link being used at this moment holds its row; waiting on it deadlocks.
  DELETE FROM email_tokens e WHERE e.id IN (
    SELECT x.id FROM email_tokens x WHERE x.email = t.email AND x.purpose = 'password_reset'
    FOR UPDATE SKIP LOCKED);
  RETURN true;
END $$""",
}


def upgrade() -> None:
    # Only the functions below turn this on: every other use of ziftbook_migrate, such as a data
    # migration, still sees one tenant's memberships at a time.
    op.execute(
        "CREATE POLICY sign_in ON memberships FOR SELECT TO ziftbook_migrate "
        "USING (current_setting('app.sign_in', true) = 'on')"
    )
    op.execute("""
        CREATE TABLE password_credentials (
          user_id uuid CONSTRAINT pk_password_credentials PRIMARY KEY
            CONSTRAINT fk_password_credentials_user_id_users REFERENCES users (id),
          hash text NOT NULL
            CONSTRAINT ck_password_credentials_argon2id CHECK (hash LIKE '$argon2id$%'))
    """)
    op.execute("""
        CREATE TABLE email_tokens (
          id uuid CONSTRAINT pk_email_tokens PRIMARY KEY DEFAULT uuidv7(),
          purpose text NOT NULL
            CONSTRAINT ck_email_tokens_purpose CHECK (purpose IN ('sign_up', 'password_reset')),
          email text NOT NULL
            CONSTRAINT ck_email_tokens_email_lowercase CHECK (email = lower(email)),
          locale text NOT NULL
            CONSTRAINT ck_email_tokens_locale CHECK (locale IN ('en', 'nl', 'pt')),
          -- NULL until the email job mints the token it sends.
          token_hash bytea CONSTRAINT uq_email_tokens_token_hash UNIQUE
            CONSTRAINT ck_email_tokens_token_hash_length CHECK (octet_length(token_hash) = 32),
          expires_at timestamptz NOT NULL)
    """)
    op.execute("CREATE INDEX ix_email_tokens_expires_at ON email_tokens (expires_at)")
    # Default privileges from 0001 gave the app role DML on every new table.
    op.execute("REVOKE ALL ON password_credentials, email_tokens FROM ziftbook_app")
    for signature, definition in FUNCTIONS.items():
        op.execute(definition)
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")


def downgrade() -> None:
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("DROP TABLE email_tokens, password_credentials")
    op.execute("DROP POLICY sign_in ON memberships")
