"""admin_sessions: a platform admin's own short-lived session, apart from business ones (ZIF-70).

The app role has no grant on the table: no app-role bug or injection can forge, read or extend an
admin session. Three SECURITY DEFINER functions are its only way in, and they fix the lifetimes
(30 minutes idle, 8 hours absolute, last_seen_at touched at most once a minute), so the caller can't
choose one. start_admin_session starts one only for an admin whose stored password hash is the one
the caller just verified. resolve_admin_session is keyed by the session hash alone: it never reads a
user by id.

reset_password is replaced, same signature and return type, so a password reset also ends the
person's admin sessions. Revoking an admin ends them through the foreign key's ON DELETE CASCADE.

The downgrade restores 0010's reset_password and drops the functions and the table, ending every
admin session.

Revision ID: 0037
Revises: 0036
"""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

FUNCTIONS = {
    "start_admin_session(uuid,text,bytea,inet,text)": f"""
CREATE FUNCTION start_admin_session(
  p_user uuid, p_password_hash text, p_id_hash bytea, p_ip inet, p_user_agent text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Starts an admin session only for an admin whose stored hash is the one just verified.
-- Lifetimes are fixed here, never chosen by the caller.
DECLARE v_started boolean;
BEGIN
  INSERT INTO admin_sessions (id_hash, user_id, expires_at, ip, user_agent)
  SELECT p_id_hash, a.user_id, now() + interval '8 hours', p_ip, left(p_user_agent, 512)
  FROM platform_admins a JOIN password_credentials c ON c.user_id = a.user_id
  WHERE a.user_id = p_user AND c.hash = p_password_hash;
  v_started := FOUND;
  IF v_started THEN
    -- ponytail: purges on every admin sign-in; move to the ZIF-5 sweeper. SKIP LOCKED, as
    -- auth.create: never wait on a row another transaction holds.
    DELETE FROM admin_sessions s WHERE s.id_hash IN (
      SELECT x.id_hash FROM admin_sessions x WHERE x.expires_at <= now() FOR UPDATE SKIP LOCKED);
  END IF;
  RETURN v_started;
END $$""",
    "resolve_admin_session(bytea)": f"""
CREATE FUNCTION resolve_admin_session(p_id_hash bytea)
RETURNS TABLE (user_id uuid, email text, name text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {SEARCH_PATH} AS $$
-- auth.RESOLVE for admin sessions: the live session (neither idle 30 minutes nor past its absolute
-- expiry), last_seen_at refreshed only when older than a minute. No row: not signed in.
-- Every column is table-aliased: the OUT parameters would make bare names ambiguous (42702).
BEGIN
  RETURN QUERY
    WITH live AS (
      SELECT s.id_hash, s.user_id, s.last_seen_at FROM admin_sessions s
      WHERE s.id_hash = p_id_hash AND s.expires_at > now()
        AND s.last_seen_at > now() - interval '30 minutes'
    ), touched AS (
      UPDATE admin_sessions t SET last_seen_at = now() FROM live l
      WHERE t.id_hash = l.id_hash AND l.last_seen_at < now() - interval '1 minute'
    )
    SELECT l.user_id, u.email, u.name FROM live l JOIN users u ON u.id = l.user_id;
END $$""",
    "end_admin_session(bytea)": f"""
CREATE FUNCTION end_admin_session(p_id_hash bytea)
RETURNS uuid LANGUAGE sql SECURITY DEFINER {SEARCH_PATH} AS $$
  -- Whose session ended; NULL when there was none.
  DELETE FROM admin_sessions s WHERE s.id_hash = p_id_hash RETURNING s.user_id
$$""",
}


def reset_password(end_admin_sessions: str) -> str:
    """0010's reset_password body, as CREATE OR REPLACE (its grants stay), plus the given line."""
    return f"""
CREATE OR REPLACE FUNCTION reset_password(p_token_hash bytea, p_password_hash text)
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
  DELETE FROM sessions s WHERE s.user_id = v_user;{end_admin_sessions}
  -- SKIP LOCKED: a second link being used at this moment holds its row; waiting on it deadlocks.
  DELETE FROM email_tokens e WHERE e.id IN (
    SELECT x.id FROM email_tokens x WHERE x.email = t.email AND x.purpose = 'password_reset'
    FOR UPDATE SKIP LOCKED);
  RETURN v_user;
END $$"""


END_ADMIN_SESSIONS = "\n  DELETE FROM admin_sessions s WHERE s.user_id = v_user;"


def upgrade() -> None:
    op.execute("""
    CREATE TABLE admin_sessions (
      id_hash bytea PRIMARY KEY CHECK (octet_length(id_hash) = 32),
      user_id uuid NOT NULL REFERENCES platform_admins (user_id) ON DELETE CASCADE,
      expires_at timestamptz NOT NULL,
      last_seen_at timestamptz NOT NULL DEFAULT now(),
      ip inet,
      user_agent text CHECK (char_length(user_agent) <= 512)
    )""")
    op.execute("CREATE INDEX ON admin_sessions (user_id)")  # revoke and reset
    op.execute("CREATE INDEX ON admin_sessions (expires_at)")  # the purge
    # Default privileges from 0001 gave the app role DML on every new table; none comes back.
    op.execute("REVOKE ALL ON admin_sessions FROM ziftbook_app")
    for signature, definition in FUNCTIONS.items():
        op.execute(definition)
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")
    op.execute(reset_password(END_ADMIN_SESSIONS))


def downgrade() -> None:
    op.execute(reset_password(""))
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("DROP TABLE admin_sessions")
