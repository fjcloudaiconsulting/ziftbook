"""users.name: the person's own name, given once at registration (ZIF-131).

NULL means an account from before registration asked for one; the API refuses every session route
but three until it is filled in (app/auth.py, `named`). No backfill: an email address never stands
in for a name. The app role still changes no users row (no UPDATE policy, 0005): set_own_name is
its only way to fill the name in, and only for someone in the business it is acting for.

complete_sign_up and accept_invite gain p_name and the old signatures are dropped (no production
yet, so no release runs the old app against the new database). A membership's display_name is a
stored copy of the name at joining, never a live view.

The downgrade restores the old functions with their grants and DROPS THE COLUMN: every name is lost.

Revision ID: 0032
Revises: 0031
"""

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
COMPLETE_SIGN_UP = "complete_sign_up(bytea, text, text, text, text, text)"
OLD_COMPLETE_SIGN_UP = "complete_sign_up(bytea, text, text, text, text)"
ACCEPT_INVITE = "accept_invite(bytea, text, text)"
OLD_ACCEPT_INVITE = "accept_invite(bytea, text)"
SET_OWN_NAME = "set_own_name(uuid, text)"

# 0013's complete_sign_up plus the name: the user's, and a copy on the owner's membership.
NEW_COMPLETE_SIGN_UP = f"""
CREATE FUNCTION complete_sign_up(p_token_hash bytea, p_password_hash text, p_business_name text,
                                 p_country text, p_currency text, p_name text)
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
  RETURN QUERY SELECT 'created', v_tenant, v_user;
END $$"""

OLD_COMPLETE_SIGN_UP_BODY = f"""
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

# 0015's accept_invite plus the name: p_name names a NEW account and is ignored for an existing
# one; the membership's display_name copies whatever name the account has.
NEW_ACCEPT_INVITE = f"""
CREATE FUNCTION accept_invite(p_token_hash bytea, p_password_hash text, p_name text)
RETURNS TABLE (outcome text, account_id uuid)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Uses up an invite and makes its email a worker of the business. With p_password_hash, the
-- account is created with that password and p_name; without, it must exist (the caller checked
-- its password) and keeps its own name.
DECLARE
  v invites;
  v_user uuid;
  v_member uuid;
BEGIN
  -- FOR UPDATE: a second accept of the same link waits, then finds nothing.
  SELECT * INTO v FROM invites i
  WHERE i.token_hash = p_token_hash AND i.expires_at > now()
  FOR UPDATE;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'invalid_token', NULL::uuid;
    RETURN;
  END IF;
  IF p_password_hash IS NULL THEN
    -- ponytail: an account erased by an operator mid-accept raises (500).
    SELECT u.id INTO STRICT v_user FROM users u WHERE u.email = v.email;
  ELSE
    -- Never touches an existing account's password: an invite is not a password reset.
    INSERT INTO users AS u (id, email, name) VALUES (uuidv7(), v.email, p_name)
    ON CONFLICT (email) DO NOTHING RETURNING u.id INTO v_user;
    IF v_user IS NULL THEN
      -- Signed up since the page looked. The invite stays, for their existing password.
      RETURN QUERY SELECT 'account_exists', NULL::uuid;
      RETURN;
    END IF;
    INSERT INTO password_credentials (user_id, hash) VALUES (v_user, p_password_hash);
  END IF;
  DELETE FROM invites i WHERE i.id = v.id;
  INSERT INTO memberships AS m (tenant_id, user_id, role, display_name)
  VALUES (v.tenant_id, v_user, 'worker', (SELECT u.name FROM users u WHERE u.id = v_user))
  ON CONFLICT (tenant_id, user_id) DO NOTHING RETURNING m.id INTO v_member;
  IF v_member IS NULL THEN
    -- Joined since the invite was sent: the invite is used up, the role is left alone.
    RETURN QUERY SELECT 'already_member', v_user;
    RETURN;
  END IF;
  RETURN QUERY SELECT 'accepted', v_user;
END $$"""

OLD_ACCEPT_INVITE_BODY = f"""
CREATE FUNCTION accept_invite(p_token_hash bytea, p_password_hash text)
RETURNS TABLE (outcome text, account_id uuid)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Uses up an invite and makes its email a worker of the business. With p_password_hash, the
-- account is created with that password; without, it must exist (the caller checked its password).
DECLARE
  v invites;
  v_user uuid;
  v_member uuid;
BEGIN
  -- FOR UPDATE: a second accept of the same link waits, then finds nothing.
  SELECT * INTO v FROM invites i
  WHERE i.token_hash = p_token_hash AND i.expires_at > now()
  FOR UPDATE;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'invalid_token', NULL::uuid;
    RETURN;
  END IF;
  IF p_password_hash IS NULL THEN
    -- ponytail: an account erased by an operator mid-accept raises (500).
    SELECT u.id INTO STRICT v_user FROM users u WHERE u.email = v.email;
  ELSE
    -- Never touches an existing account's password: an invite is not a password reset.
    INSERT INTO users AS u (id, email) VALUES (uuidv7(), v.email)
    ON CONFLICT (email) DO NOTHING RETURNING u.id INTO v_user;
    IF v_user IS NULL THEN
      -- Signed up since the page looked. The invite stays, for their existing password.
      RETURN QUERY SELECT 'account_exists', NULL::uuid;
      RETURN;
    END IF;
    INSERT INTO password_credentials (user_id, hash) VALUES (v_user, p_password_hash);
  END IF;
  DELETE FROM invites i WHERE i.id = v.id;
  INSERT INTO memberships AS m (tenant_id, user_id, role) VALUES (v.tenant_id, v_user, 'worker')
  ON CONFLICT (tenant_id, user_id) DO NOTHING RETURNING m.id INTO v_member;
  IF v_member IS NULL THEN
    -- Joined since the invite was sent: the invite is used up, the role is left alone.
    RETURN QUERY SELECT 'already_member', v_user;
    RETURN;
  END IF;
  RETURN QUERY SELECT 'accepted', v_user;
END $$"""

# The name is filled in once, and only for someone in the caller's business: the EXISTS reads
# memberships under the caller's tenant (FORCE row-level security), so a compromised app role can
# name nobody else. The stored display names are then filled in in EVERY business of the person,
# which the caller's tenant cannot see: like account_by_email (0007), list them under app.sign_in,
# then visit each tenant, and restore both settings.
SET_OWN_NAME_FUNCTION = f"""
CREATE FUNCTION set_own_name(p_user uuid, p_name text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Gives a nameless person their name; false when they already have one or are not in this business.
DECLARE
  v_sign_in text := current_setting('app.sign_in', true);
  v_tenant text := current_setting('app.tenant_id', true);
  v_tenants uuid[];
  t uuid;
BEGIN
  UPDATE users u SET name = p_name
  WHERE u.id = p_user AND u.name IS NULL
    AND EXISTS (SELECT 1 FROM memberships m WHERE m.user_id = p_user);
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  PERFORM set_config('app.sign_in', 'on', true);
  SELECT array_agg(m.tenant_id) INTO v_tenants FROM memberships m WHERE m.user_id = p_user;
  PERFORM set_config('app.sign_in', coalesce(v_sign_in, ''), true);
  FOREACH t IN ARRAY v_tenants LOOP
    PERFORM set_config('app.tenant_id', t::text, true);
    UPDATE memberships SET display_name = p_name WHERE user_id = p_user AND display_name IS NULL;
  END LOOP;
  PERFORM set_config('app.tenant_id', coalesce(v_tenant, ''), true);
  RETURN true;
END $$"""


def upgrade() -> None:
    # No grant change: 0001's default privileges give ziftbook_app UPDATE on users, but 0005 gave
    # the table no UPDATE policy, so it changes 0 rows. Keep it that way. The check is named
    # ck_users_name_length by the naming convention in app/db.py.
    op.execute("""
    ALTER TABLE users ADD COLUMN name text
      CONSTRAINT ck_users_name_length CHECK (char_length(name) BETWEEN 1 AND 60)
    """)
    op.execute(f"DROP FUNCTION {OLD_COMPLETE_SIGN_UP}")
    op.execute(f"DROP FUNCTION {OLD_ACCEPT_INVITE}")
    for create, signature in (
        (NEW_COMPLETE_SIGN_UP, COMPLETE_SIGN_UP),
        (NEW_ACCEPT_INVITE, ACCEPT_INVITE),
        (SET_OWN_NAME_FUNCTION, SET_OWN_NAME),
    ):
        op.execute(create)
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SET_OWN_NAME}")
    op.execute(f"DROP FUNCTION {ACCEPT_INVITE}")
    op.execute(f"DROP FUNCTION {COMPLETE_SIGN_UP}")
    for create, signature in (
        (OLD_COMPLETE_SIGN_UP_BODY, OLD_COMPLETE_SIGN_UP),
        (OLD_ACCEPT_INVITE_BODY, OLD_ACCEPT_INVITE),
    ):
        op.execute(create)
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")
    # Every name is lost.
    op.execute("ALTER TABLE users DROP COLUMN name")
