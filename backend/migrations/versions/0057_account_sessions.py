"""Account sessions: a person signed in to their own account, with or without a business (ZIF-146).

account_sessions holds a session of a person, not of a membership: it has no tenant and no role, so
no console route can be reached with one (auth.resolve reads only `sessions`). It is a secrets table
like admin_sessions (0037): the app role has no grant on it, and five SECURITY DEFINER functions are
its only way in. They fix the lifetimes (30 days absolute, 7 days idle, last_seen_at touched at most
every 5 minutes, "fresh" for 24 hours), so the caller cannot choose one.

- An account session a console sign-in starts is bound to that business session (started_by): it
  ends when the business session ends or expires, so on a shop's shared device a member's personal
  session never outlives their console session or their membership. One the person starts on the
  booking page, on their own device, is not bound.
- start_account_session starts one only with the stored password hash the API just verified, taking
  that password_credentials row FOR SHARE first, so a password reset racing the sign-in either wins
  (the answer is 'stale') or waits and then deletes the new session. A platform admin gets none
  ('admin'): a password alone must not give an admin identity a session that can rename them.
- resolve_account_session, end_account_session and set_own_profile are keyed by the session hash
  alone: none reads or changes a user by id. resolve_account_session and set_own_profile answer
  nothing for a platform admin's session (one made before the person was granted admin; the CLI's
  grant also ends them).
- end_account_sessions ends every account session of the person holding a live account or business
  session hash (sign out everywhere).

reset_password is replaced, same signature, from 0037's own template plus one line, so a reset also
ends the person's account sessions; the downgrade puts 0037's body back byte for byte.

users gains phone (E.164, nullable, never unique: a duplicate error would tell who holds a number).
The app role's SELECT on users becomes a column list without it, so no business reads a member's
phone through the users policy: only the definers above read it. These statements come last, so
the ACCESS EXCLUSIVE lock on users is held for the shortest time; if this migration ever times out
on it, split them into a migration of their own.

Trust, stated: the database cannot check a password. An app role that runs SQL of its own can read a
hash with account_by_email and start an account session for anyone but a platform admin, and then
read and change that person's name and phone. It could already insert business sessions.

The downgrade drops the functions and the table (every account session ends), restores table-level
SELECT on users and drops users.phone (stored phones are lost).

Revision ID: 0057
Revises: 0056
"""

from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

# The live account session for a hash: neither idle 7 days nor past its absolute expiry. Shared by
# every function below, so the lifetimes live in one place.
LIVE = """s.id_hash = p_id_hash AND s.expires_at > now()
        AND s.last_seen_at > now() - interval '7 days'"""
# LIVE, and never a platform admin's (code review N-1): a person made an admin after signing in
# keeps no password-only session that reads or changes the admin identity. `grant` also ends them.
USABLE = (
    LIVE
    + """
        AND NOT EXISTS (SELECT FROM platform_admins a WHERE a.user_id = s.user_id)
        AND (s.started_by IS NULL OR EXISTS (
          SELECT FROM sessions b WHERE b.id_hash = s.started_by AND b.expires_at > now()
            AND b.last_seen_at > now() - interval '7 days'))"""
)
# The last clause (security code review N-2): an account session a console sign-in started lives
# only as long as that business session. On a shop's shared device it ends with the console's
# sign-out, its idle or absolute expiry, sign-out everywhere, a password reset and the member's
# removal, and it is never idle longer than the business session. The foreign key deletes it with
# that row; this clause covers the expiries, which delete nothing.

FUNCTIONS = {
    "start_account_session(uuid,text,bytea,inet,text,bytea)": f"""
CREATE FUNCTION start_account_session(
  p_user uuid, p_password_hash text, p_id_hash bytea, p_ip inet, p_user_agent text,
  p_started_by bytea)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- 'started', 'stale' (the stored hash is not the one just verified: a reset won), or 'admin' (a
-- platform admin gets no account session). Lock order: password_credentials, then account_sessions.
-- p_started_by: the business session a console sign-in started with it, or NULL (the account's own
-- sign-in): the account session then lives only as long as that one.
BEGIN
  PERFORM FROM password_credentials c
  WHERE c.user_id = p_user AND c.hash = p_password_hash FOR SHARE;
  IF NOT FOUND THEN RETURN 'stale'; END IF;
  IF EXISTS (SELECT FROM platform_admins a WHERE a.user_id = p_user) THEN RETURN 'admin'; END IF;
  INSERT INTO account_sessions (id_hash, user_id, expires_at, ip, user_agent, started_by)
  VALUES (p_id_hash, p_user, now() + interval '30 days', p_ip, left(p_user_agent, 512),
          p_started_by);
  -- ponytail: purges on every start; move to the ZIF-5 sweeper. SKIP LOCKED, as auth.create:
  -- never wait on a row another transaction holds.
  DELETE FROM account_sessions s WHERE s.id_hash IN (
    SELECT x.id_hash FROM account_sessions x WHERE x.expires_at <= now() FOR UPDATE SKIP LOCKED);
  RETURN 'started';
END $$""",
    "resolve_account_session(bytea)": f"""
CREATE FUNCTION resolve_account_session(p_id_hash bytea)
RETURNS TABLE (user_id uuid, email text, name text, phone text, fresh boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {SEARCH_PATH} AS $$
-- The session holder's own account, or no row. last_seen_at refreshed only when older than 5
-- minutes. fresh: the session started within the last 24 hours (history asks for it).
-- Every column is table-aliased: the OUT parameters would make bare names ambiguous (42702).
BEGIN
  RETURN QUERY
    WITH live AS (
      SELECT s.id_hash, s.user_id, s.started_at, s.last_seen_at FROM account_sessions s
      WHERE {USABLE}
    ), touched AS (
      UPDATE account_sessions t SET last_seen_at = now() FROM live l
      WHERE t.id_hash = l.id_hash AND l.last_seen_at < now() - interval '5 minutes'
    )
    SELECT l.user_id, u.email, u.name, u.phone, l.started_at > now() - interval '24 hours'
    FROM live l JOIN users u ON u.id = l.user_id;
END $$""",
    "end_account_session(bytea)": f"""
CREATE FUNCTION end_account_session(p_id_hash bytea)
RETURNS uuid LANGUAGE sql SECURITY DEFINER {SEARCH_PATH} AS $$
  -- Whose session ended; NULL when there was none.
  DELETE FROM account_sessions s WHERE s.id_hash = p_id_hash RETURNING s.user_id
$$""",
    "end_account_sessions(bytea)": f"""
CREATE FUNCTION end_account_sessions(p_id_hash bytea)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Sign out everywhere: every account session of the person holding this live account or business
-- session hash. Returns that person, whose id the caller already has; NULL when the hash is dead.
DECLARE v_user uuid;
BEGIN
  SELECT s.user_id INTO v_user FROM account_sessions s WHERE {LIVE};
  IF NOT FOUND THEN
    SELECT s.user_id INTO v_user FROM sessions s
    WHERE s.id_hash = p_id_hash AND s.expires_at > now()
      AND s.last_seen_at > now() - interval '7 days';
  END IF;
  IF v_user IS NULL THEN RETURN NULL; END IF;
  DELETE FROM account_sessions s WHERE s.user_id = v_user;
  RETURN v_user;
END $$""",
    "set_own_profile(bytea,text,text,boolean)": f"""
CREATE FUNCTION set_own_profile(p_id_hash bytea, p_name text, p_phone text, p_set_phone boolean)
RETURNS TABLE (email text, name text, phone text, previous_name text, previous_phone text)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- The session holder changes their own name (NULL keeps it) and, when p_set_phone, their phone
-- (NULL clears it). Only what was sent is written, in this one statement, so two edits at once
-- never lose one another's field. Never a membership's display_name, unlike set_own_name: a
-- business sees the new name only where it has no display name of its own. No row: no live account
-- session.
BEGIN
  RETURN QUERY
    UPDATE users u SET name = coalesce(p_name, u.name),
                       phone = CASE WHEN p_set_phone THEN p_phone ELSE u.phone END
    FROM account_sessions s
    WHERE {USABLE} AND u.id = s.user_id
    RETURNING new.email, new.name, new.phone, old.name, old.phone;
END $$""",
}


def reset_password(end_sessions: str) -> str:
    """0037's reset_password template, as CREATE OR REPLACE (its grants stay), plus the given lines.
    reset_password(END_ADMIN_SESSIONS) is 0037's body exactly (tests/test_migrations.py)."""
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
  DELETE FROM sessions s WHERE s.user_id = v_user;{end_sessions}
  -- SKIP LOCKED: a second link being used at this moment holds its row; waiting on it deadlocks.
  DELETE FROM email_tokens e WHERE e.id IN (
    SELECT x.id FROM email_tokens x WHERE x.email = t.email AND x.purpose = 'password_reset'
    FOR UPDATE SKIP LOCKED);
  RETURN v_user;
END $$"""


END_ADMIN_SESSIONS = "\n  DELETE FROM admin_sessions s WHERE s.user_id = v_user;"
END_ACCOUNT_SESSIONS = "\n  DELETE FROM account_sessions s WHERE s.user_id = v_user;"
# Every column users had before this migration (0005, 0032): what the running release reads.
APP_USER_COLUMNS = "id, email, locale, name"


def upgrade() -> None:
    op.execute("""
    CREATE TABLE account_sessions (
      id_hash bytea CONSTRAINT pk_account_sessions PRIMARY KEY
        CONSTRAINT ck_account_sessions_id_hash_length CHECK (octet_length(id_hash) = 32),
      -- The app role cannot insert, so this foreign key is no existence oracle. ON DELETE CASCADE:
      -- erasing a user ends their sessions.
      user_id uuid NOT NULL
        CONSTRAINT fk_account_sessions_user_id_users REFERENCES users (id) ON DELETE CASCADE,
      started_at timestamptz NOT NULL DEFAULT now(),
      expires_at timestamptz NOT NULL,
      last_seen_at timestamptz NOT NULL DEFAULT now(),
      ip inet,
      user_agent text
        CONSTRAINT ck_account_sessions_user_agent_length CHECK (char_length(user_agent) <= 512),
      -- The business session a console sign-in started this one with (USABLE): deleting it deletes
      -- this one. NULL for the account's own sign-in, on the person's own device.
      started_by bytea
        CONSTRAINT fk_account_sessions_started_by_sessions REFERENCES sessions (id_hash)
          ON DELETE CASCADE
    )""")
    op.execute("CREATE INDEX ix_account_sessions_user_id ON account_sessions (user_id)")
    op.execute("CREATE INDEX ix_account_sessions_expires_at ON account_sessions (expires_at)")
    # The foreign key's cascade: a business session's delete finds its account session.
    op.execute("CREATE INDEX ix_account_sessions_started_by ON account_sessions (started_by)")
    # Default privileges from 0001 gave the app role DML on every new table; none comes back.
    op.execute("REVOKE ALL ON account_sessions FROM ziftbook_app")
    for signature, definition in FUNCTIONS.items():
        op.execute(definition)
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")
    op.execute(reset_password(END_ADMIN_SESSIONS + END_ACCOUNT_SESSIONS))
    # Last: these lock users.
    op.execute(
        "ALTER TABLE users ADD COLUMN phone text"
        " CONSTRAINT ck_users_phone CHECK (phone ~ '^\\+[1-9][0-9]{1,14}$')"
    )
    op.execute("REVOKE SELECT ON users FROM ziftbook_app")
    op.execute(f"GRANT SELECT ({APP_USER_COLUMNS}) ON users TO ziftbook_app")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ({APP_USER_COLUMNS}) ON users FROM ziftbook_app")
    op.execute("GRANT SELECT ON users TO ziftbook_app")
    op.execute("ALTER TABLE users DROP COLUMN phone")
    op.execute(reset_password(END_ADMIN_SESSIONS))
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("DROP TABLE account_sessions")
