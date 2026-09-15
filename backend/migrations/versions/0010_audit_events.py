"""audit_events: an append-only record of sign-ins, sign-outs, resets and new businesses.

The app role may add events and read its own business's, never change, remove or backdate them.
Events of no business (failed sign-ins, password resets) are invisible to the app; an operator reads
every event as ziftbook_migrate with app.audit_review on (CONTRIBUTING, "Audit log").

The log is append-only, not unforgeable: the app role sets app.tenant_id itself, so it can add false
events. That errs towards more businesses looking affected in an incident, never fewer.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

# 0007's complete_password_reset body, now returning whose password changed.
RESET_PASSWORD = f"""
CREATE FUNCTION reset_password(p_token_hash bytea, p_password_hash text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Uses up the reset link, sets the password, and signs the user out everywhere.
-- Returns the user whose password changed; NULL when nothing was reset.
DECLARE
  t email_tokens;
  v_user uuid;
BEGIN
  DELETE FROM email_tokens e
  WHERE e.token_hash = p_token_hash AND e.purpose = 'password_reset' AND e.expires_at > now()
  RETURNING * INTO t;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT u.id INTO v_user FROM users u WHERE u.email = t.email;
  IF NOT FOUND THEN RETURN NULL; END IF;
  INSERT INTO password_credentials AS c (user_id, hash) VALUES (v_user, p_password_hash)
  ON CONFLICT (user_id) DO UPDATE SET hash = excluded.hash;
  DELETE FROM sessions s WHERE s.user_id = v_user;
  -- SKIP LOCKED: a second link being used at this moment holds its row; waiting on it deadlocks.
  DELETE FROM email_tokens e WHERE e.id IN (
    SELECT x.id FROM email_tokens x WHERE x.email = t.email AND x.purpose = 'password_reset'
    FOR UPDATE SKIP LOCKED);
  RETURN v_user;
END $$"""

# A new name instead of a new return type: a connection that prepared the old call would fail with
# "cached plan must not change result type" until replaced.
# ponytail: drop complete_password_reset once no deployed app calls it.
COMPLETE_PASSWORD_RESET = f"""
CREATE OR REPLACE FUNCTION complete_password_reset(p_token_hash bytea, p_password_hash text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER {SEARCH_PATH} AS $$
  SELECT reset_password(p_token_hash, p_password_hash) IS NOT NULL
$$"""

ORIGINAL_COMPLETE_PASSWORD_RESET = f"""
CREATE OR REPLACE FUNCTION complete_password_reset(p_token_hash bytea, p_password_hash text)
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
END $$"""


def upgrade() -> None:
    op.execute("""
    CREATE TABLE audit_events (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      -- clock_timestamp, not now(): a transaction held open would otherwise backdate its events.
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      -- No foreign keys: events outlive erased users and removed businesses.
      tenant_id uuid DEFAULT nullif(current_setting('app.tenant_id', true), '')::uuid,
      actor_user_id uuid,
      action text NOT NULL,
      target text,
      ip inet,
      user_agent text CHECK (char_length(user_agent) <= 512)
    )""")
    op.execute("CREATE INDEX ON audit_events (tenant_id, id)")
    op.execute("CREATE INDEX ON audit_events USING brin (created_at)")  # incident time windows
    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_events FORCE ROW LEVEL SECURITY")
    # Not enable_tenant_isolation: its check refuses events of no business. TO ziftbook_app, so an
    # operator's review isn't stopped by a missing app.tenant_id.
    op.execute(
        "CREATE POLICY tenant_isolation ON audit_events FOR SELECT TO ziftbook_app "
        "USING (tenant_id = current_setting('app.tenant_id')::uuid)"
    )
    op.execute(
        "CREATE POLICY record ON audit_events FOR INSERT TO ziftbook_app WITH CHECK "
        "(tenant_id IS NOT DISTINCT FROM nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE POLICY audit_review ON audit_events FOR SELECT TO ziftbook_migrate "
        "USING (current_setting('app.audit_review', true) = 'on')"
    )
    # ponytail: kept forever until the ZIF-5 sweeper purges sign_in_failed after 30 days and the
    # rest after a year (a definer function with its own DELETE policy).
    op.execute("REVOKE ALL ON audit_events FROM ziftbook_app")
    op.execute("GRANT SELECT ON audit_events TO ziftbook_app")
    op.execute(
        "GRANT INSERT (tenant_id, actor_user_id, action, target, ip, user_agent) "
        "ON audit_events TO ziftbook_app"
    )

    op.execute(RESET_PASSWORD)
    op.execute("REVOKE EXECUTE ON FUNCTION reset_password(bytea, text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION reset_password(bytea, text) TO ziftbook_app")
    op.execute(COMPLETE_PASSWORD_RESET)


def downgrade() -> None:
    op.execute(ORIGINAL_COMPLETE_PASSWORD_RESET)
    op.execute("DROP FUNCTION reset_password(bytea, text)")
    op.execute("DROP TABLE audit_events")
