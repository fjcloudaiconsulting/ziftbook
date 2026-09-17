"""invites: an owner's request for someone to join their business as a worker; accept_invite.

A link carries <tenant_id>.<secret>; only sha256(secret) is stored, and only by the email job,
which mints it. accept_invite is here, not with the endpoints, so it is deployed a release before
the app that calls it (the email job ships the same way).

Revision ID: 0015
Revises: 0014
"""

from alembic import op

from app.db import enable_tenant_isolation

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
SIGNATURE = "accept_invite(bytea, text)"

# Runs in the caller's tenant_context: invites has FORCE row-level security, so this definer (the
# table's owner) sees only the link's business's invites, and with no tenant it raises. It never
# sets app.tenant_id itself: taking the business from the invite would let a token work under any
# tenant id. It reads no memberships (the INSERT's RETURNING gives back only its own row), so
# app.sign_in needs no clearing. Out column account_id, not user_id: an OUT parameter named like a
# column makes ON CONFLICT (tenant_id, user_id) ambiguous in plpgsql.
ACCEPT_INVITE = f"""
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


def upgrade() -> None:
    op.execute("""
    CREATE TABLE invites (
      id uuid CONSTRAINT pk_invites PRIMARY KEY DEFAULT uuidv7(),
      tenant_id uuid NOT NULL CONSTRAINT fk_invites_tenant_id_tenants REFERENCES tenants (id),
      email text NOT NULL CONSTRAINT ck_invites_email_lowercase CHECK (email = lower(email)),
      -- NULL until the email job mints the link it sends.
      token_hash bytea
        CONSTRAINT ck_invites_token_hash_length CHECK (octet_length(token_hash) = 32),
      expires_at timestamptz NOT NULL,
      CONSTRAINT uq_invites_tenant_id_email UNIQUE (tenant_id, email),
      CONSTRAINT uq_invites_tenant_id_token_hash UNIQUE (tenant_id, token_hash)
    )""")
    enable_tenant_isolation("invites")
    op.execute(ACCEPT_INVITE)
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SIGNATURE} FROM PUBLIC")
    # Risk (accepted): the app role can already insert sessions and memberships directly, so this
    # grant lets it also call accept_invite with its own hash and argon2 hash, creating a
    # password-bearing account for an unregistered email with no mailed secret. The owner can still
    # reset that account's password.
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO ziftbook_app")


def downgrade() -> None:
    # Pending invites are lost; their links stop working.
    op.execute(f"DROP FUNCTION {SIGNATURE}")
    op.execute("DROP TABLE invites")
