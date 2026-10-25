"""Account sign-up, the worker's side and the database's (ZIF-146).

email_tokens gains the purpose account_sign_up: a person creates their own ziftbook account, with
no business. The CHECK is dropped and added in one ALTER TABLE statement, so there is no moment
without it.

mint_email_token is replaced, same signature and return type, so its grant stays the worker's alone
(0049, 0051): an account sign-up for an email that already has an account is `registered` (the row
goes, and the email says to sign in, with no link), and its link lives 24 hours, as a business
sign-up's does. Nothing else changes.

complete_account_sign_up uses up a live account_sign_up link and creates the person's account and
password, with no business and no membership; a business sign-up link never works here, nor this
link in complete_sign_up (it filters purpose 'sign_up'). An email that gained an account since the
link was sent is `already_registered`: no account is touched and no password written.

This ships one release before the routes that enqueue the purpose: an older worker would fail such
a job on its link page, or send the console's sign-in email.

The downgrade deletes the account sign-up links still pending, restores the CHECK and 0007's
mint_email_token, and drops complete_account_sign_up.

Revision ID: 0059
Revises: 0058
"""

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"


def mint_email_token(registered: str, expiry: str) -> str:
    """0007's mint_email_token, as CREATE OR REPLACE (its grants stay), with the given test for a
    sign-up and expiry. mint_email_token(*BEFORE) is 0007's body (tests/test_migrations.py)."""
    return f"""
CREATE OR REPLACE FUNCTION mint_email_token(p_id uuid, p_token_hash bytea)
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
  IF FOUND AND {registered} THEN
    DELETE FROM email_tokens e WHERE e.id = p_id;
    RETURN QUERY SELECT t.purpose, t.email, coalesce(v_locale, t.locale), true;
    RETURN;
  END IF;
  IF NOT FOUND AND t.purpose = 'password_reset' THEN
    DELETE FROM email_tokens e WHERE e.id = p_id;
    RETURN;
  END IF;
  UPDATE email_tokens e SET token_hash = p_token_hash,
    expires_at = now() + {expiry}
  WHERE e.id = p_id;
  RETURN QUERY SELECT t.purpose, t.email, coalesce(v_locale, t.locale), false;
END $$"""


BEFORE = (
    "t.purpose = 'sign_up'",
    """CASE t.purpose WHEN 'sign_up' THEN interval '24 hours'
                                        ELSE interval '15 minutes' END""",
)
AFTER = (
    "t.purpose IN ('sign_up', 'account_sign_up')",
    """CASE WHEN t.purpose IN ('sign_up', 'account_sign_up') THEN interval '24 hours'
                              ELSE interval '15 minutes' END""",
)

COMPLETE = f"""
CREATE FUNCTION complete_account_sign_up(
  p_token_hash bytea, p_password_hash text, p_name text, p_phone text)
RETURNS TABLE (outcome text, user_id uuid)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Uses up an account sign-up link and creates the person's account and password together: no
-- business, no membership. 'created', 'invalid_token' or 'already_registered'.
DECLARE
  t email_tokens;
  v_user uuid;
BEGIN
  DELETE FROM email_tokens e
  WHERE e.token_hash = p_token_hash AND e.purpose = 'account_sign_up' AND e.expires_at > now()
  RETURNING * INTO t;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'invalid_token', NULL::uuid;
    RETURN;
  END IF;
  -- ON CONFLICT: a second link for the same email, completed at the same time, waits here.
  INSERT INTO users AS u (id, email, locale, name, phone)
  VALUES (uuidv7(), t.email, t.locale, p_name, p_phone)
  ON CONFLICT (email) DO NOTHING RETURNING u.id INTO v_user;
  IF v_user IS NULL THEN
    RETURN QUERY SELECT 'already_registered', NULL::uuid;
    RETURN;
  END IF;
  INSERT INTO password_credentials (user_id, hash) VALUES (v_user, p_password_hash);
  RETURN QUERY SELECT 'created', v_user;
END $$"""


def upgrade() -> None:
    op.execute("""
    ALTER TABLE email_tokens
      DROP CONSTRAINT ck_email_tokens_purpose,
      ADD CONSTRAINT ck_email_tokens_purpose
        CHECK (purpose IN ('sign_up', 'password_reset', 'account_sign_up'))""")
    op.execute(mint_email_token(*AFTER))
    op.execute(COMPLETE)
    signature = "complete_account_sign_up(bytea,text,text,text)"
    op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION complete_account_sign_up(bytea,text,text,text)")
    op.execute(mint_email_token(*BEFORE))
    op.execute("DELETE FROM email_tokens WHERE purpose = 'account_sign_up'")
    op.execute("""
    ALTER TABLE email_tokens
      DROP CONSTRAINT ck_email_tokens_purpose,
      ADD CONSTRAINT ck_email_tokens_purpose CHECK (purpose IN ('sign_up', 'password_reset'))""")
