"""memberships.role 'removed': ending a membership keeps the row (ZIF-112).

Removing someone from a business no longer deletes their membership: bookings (0026) reference it
with no ON DELETE, and booking history must still resolve. An ended membership has role 'removed'
and removed_at set (a CHECK pairs them). The role is the gate, not the timestamp: the sessions
foreign key (0006) carries the role, so ending fails while a session survives, and ck_sessions_role
makes "a session exists, so its membership is live" a schema fact. keep_an_owner (0014) needs no
change: owner -> removed is an owner losing the role.

account_by_email picks the oldest ACTIVE membership; accept_invite revives a removed row as a
worker (same id, so old bookings stay on it). Both are replaced in place.

The downgrade refuses while any membership is removed (the old role CHECK fails on it): such rows
can be neither deleted (bookings reference them) nor silently re-admitted.

Revision ID: 0045
Revises: 0044
"""

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

ACTIVE = " AND m.role <> 'removed'"

DO_NOTHING = "ON CONFLICT (tenant_id, user_id) DO NOTHING RETURNING m.id INTO v_member;"

# A removed member comes back on the same row: their old bookings stay theirs. As a worker, with
# no hours, services or time off (removal deleted them). A live member matches nothing in the
# WHERE, so v_member stays NULL: already_member, as before. Only this tenant's row can conflict
# (the arbiter is (v.tenant_id, user)), so app.sign_in needs no clearing.
REVIVE = """ON CONFLICT (tenant_id, user_id) DO UPDATE
    SET role = 'worker', removed_at = NULL,
        display_name = coalesce(m.display_name, excluded.display_name)
    WHERE m.role = 'removed'
  RETURNING m.id INTO v_member;"""


def account_by_email(active: str) -> str:
    """0007's body, as CREATE OR REPLACE, with the membership filter given."""
    return f"""
CREATE OR REPLACE FUNCTION account_by_email(p_email text)
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
           (SELECT m.tenant_id FROM memberships m WHERE m.user_id = u.id{active}
            ORDER BY m.id LIMIT 1)
    FROM users u LEFT JOIN password_credentials c ON c.user_id = u.id
    WHERE u.email = p_email;
  PERFORM set_config('app.sign_in', coalesce(v_sign_in, ''), true);
  PERFORM set_config('app.tenant_id', coalesce(v_tenant, ''), true);
END $$"""


def accept_invite(conflict: str) -> str:
    """0032's NEW_ACCEPT_INVITE, as CREATE OR REPLACE, with the ON CONFLICT tail given."""
    return f"""
CREATE OR REPLACE FUNCTION accept_invite(p_token_hash bytea, p_password_hash text, p_name text)
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
  {conflict}
  IF v_member IS NULL THEN
    -- Joined since the invite was sent: the invite is used up, the role is left alone.
    RETURN QUERY SELECT 'already_member', v_user;
    RETURN;
  END IF;
  RETURN QUERY SELECT 'accepted', v_user;
END $$"""


def upgrade() -> None:
    op.execute("""
    ALTER TABLE memberships
      DROP CONSTRAINT ck_memberships_role,
      ADD CONSTRAINT ck_memberships_role CHECK (role IN ('owner', 'worker', 'removed')),
      ADD COLUMN removed_at timestamptz,
      ADD CONSTRAINT ck_memberships_removed_at CHECK ((role = 'removed') = (removed_at IS NOT NULL))
    """)
    # A session only for a live membership; with the composite foreign key, a session exists only
    # while its membership is live.
    op.execute(
        "ALTER TABLE sessions ADD CONSTRAINT ck_sessions_role CHECK (role IN ('owner', 'worker'))"
    )
    op.execute(account_by_email(ACTIVE))
    op.execute(accept_invite(REVIVE))


def downgrade() -> None:
    # The role CHECK first: a removed row fails it (23514, validated with row-level security
    # bypassed, so every tenant counts) and nothing has changed yet.
    op.execute("""
    ALTER TABLE memberships
      DROP CONSTRAINT ck_memberships_role,
      ADD CONSTRAINT ck_memberships_role CHECK (role IN ('owner', 'worker')),
      DROP CONSTRAINT ck_memberships_removed_at,
      DROP COLUMN removed_at
    """)
    op.execute("ALTER TABLE sessions DROP CONSTRAINT ck_sessions_role")
    op.execute(accept_invite(DO_NOTHING))
    op.execute(account_by_email(""))
