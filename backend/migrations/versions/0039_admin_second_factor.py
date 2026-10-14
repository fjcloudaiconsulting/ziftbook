"""second_factors: a platform admin's TOTP factor and recovery codes (ZIF-71).

Adds admin_sessions.second_factor_at (NULL: only the password passed, a half session), the
second_factors table (one row per user: the CLI's enrolment ticket, the encrypted TOTP secret, the
replay and failure counters, the recovery codes), the live_admin_user helper, five SECURITY DEFINER
functions, and resolve_admin_session recreated with factor_passed, factor_fresh and enrolled.

The app role has no grant on second_factors. Every definer function finds the user through the live
admin session's hash, never through a user id the caller passes.

Proof rule: no function takes the value it stores. The app sends sha256(x) and the database stores
sha256(sha256(x)), for the TOTP secret, recovery codes and the enrolment ticket alike, so a dumped
row is never a credential, and a TOTP pass needs the secret, which only the app can decrypt
(ZIF_TOTP_KEY, never in the database).

The downgrade DROPS every TOTP enrolment and recovery code; every admin re-enrols with
`python -m app.admin enrol`.

Revision ID: 0039
Revises: 0038
"""

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
# The current 30-second TOTP step, by the database's clock.
NOW_STEP = "floor(extract(epoch FROM now()) / 30)::bigint"

# 0037's resolve_admin_session, verbatim, for the downgrade.
RESOLVE_0037 = f"""
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
END $$"""

LIVE_ADMIN_USER = f"""
CREATE FUNCTION live_admin_user(p_id_hash bytea) RETURNS uuid LANGUAGE sql STABLE {SEARCH_PATH}
AS $$
  -- Not a definer, and not executable by the app: only the definers below call it.
  -- The same liveness test as resolve_admin_session (0037), without the touch.
  SELECT s.user_id FROM admin_sessions s
  WHERE s.id_hash = p_id_hash AND s.expires_at > now()
    AND s.last_seen_at > now() - interval '30 minutes'
$$"""

FUNCTIONS = {
    "resolve_admin_session(bytea)": f"""
CREATE FUNCTION resolve_admin_session(p_id_hash bytea)
RETURNS TABLE (user_id uuid, email text, name text,
               factor_passed boolean, factor_fresh boolean, enrolled boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {SEARCH_PATH} AS $$
-- auth.RESOLVE for admin sessions: the live session (neither idle 30 minutes nor past its absolute
-- expiry), last_seen_at refreshed only when older than a minute. No row: not signed in.
-- factor_passed: a second factor passed in this session. factor_fresh: within the last 15 minutes,
-- fixed here. enrolled: the user has a confirmed TOTP factor. Not "second_factor": the API's
-- AdminSession.second_factor is a string, and a boolean of that name would fail to validate.
-- Every column is table-aliased: the OUT parameters would make bare names ambiguous (42702).
BEGIN
  RETURN QUERY
    WITH live AS (
      SELECT s.id_hash, s.user_id, s.last_seen_at, s.second_factor_at FROM admin_sessions s
      WHERE s.id_hash = p_id_hash AND s.expires_at > now()
        AND s.last_seen_at > now() - interval '30 minutes'
    ), touched AS (
      UPDATE admin_sessions t SET last_seen_at = now() FROM live l
      WHERE t.id_hash = l.id_hash AND l.last_seen_at < now() - interval '1 minute'
    )
    SELECT l.user_id, u.email, u.name,
           l.second_factor_at IS NOT NULL,
           coalesce(l.second_factor_at > now() - interval '15 minutes', false),
           coalesce(f.confirmed_at IS NOT NULL, false)
    FROM live l JOIN users u ON u.id = l.user_id
    LEFT JOIN second_factors f ON f.user_id = l.user_id;
END $$""",
    "totp_ciphertext(bytea)": f"""
CREATE FUNCTION totp_ciphertext(p_id_hash bytea)
RETURNS bytea LANGUAGE sql STABLE SECURITY DEFINER {SEARCH_PATH} AS $$
  -- The session user's secret, begun or confirmed. Useless without ZIF_TOTP_KEY.
  SELECT f.secret_ct FROM second_factors f WHERE f.user_id = live_admin_user(p_id_hash)
$$""",
    "begin_totp_enrolment(bytea,bytea,bytea,bytea)": f"""
CREATE FUNCTION begin_totp_enrolment(
  p_id_hash bytea, p_ticket_proof bytea, p_secret_ct bytea, p_proof bytea)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- 'begun' | 'invalid_ticket' | 'already_enrolled'. Stores a new, unconfirmed secret for the session
-- user, given the CLI's ticket.
DECLARE v_user uuid := live_admin_user(p_id_hash);
BEGIN
  -- Never overwrites a confirmed secret: re-enrolling goes through the CLI.
  IF EXISTS (SELECT FROM second_factors f WHERE f.user_id = v_user AND f.confirmed_at IS NOT NULL)
  THEN
    RETURN 'already_enrolled';
  END IF;
  UPDATE second_factors f SET secret_ct = p_secret_ct, proof_hash = sha256(p_proof),
                              last_step = 0, failed_attempts = 0
  WHERE f.user_id = v_user AND f.ticket_hash = sha256(p_ticket_proof)
    AND f.ticket_expires_at > now();
  -- The ticket is not used up here: an abandoned QR can be restarted within the hour.
  RETURN CASE WHEN FOUND THEN 'begun' ELSE 'invalid_ticket' END;
END $$""",
    "confirm_totp_enrolment(bytea,bytea,bigint,bytea[])": f"""
CREATE FUNCTION confirm_totp_enrolment(
  p_id_hash bytea, p_proof bytea, p_step bigint, p_code_proofs bytea[])
RETURNS TABLE (outcome text, failures integer)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- 'enrolled' | 'failed' | 'locked' | 'none'. Confirms the begun enrolment with a code, stores the
-- ten recovery codes, uses up the ticket and passes this session's second factor.
DECLARE
  v_user uuid := live_admin_user(p_id_hash);
  v_now bigint := {NOW_STEP};
  f second_factors;
BEGIN
  IF cardinality(p_code_proofs) IS DISTINCT FROM 10 THEN
    RAISE EXCEPTION 'ten recovery codes';
  END IF;
  SELECT * INTO f FROM second_factors x
  WHERE x.user_id = v_user AND x.confirmed_at IS NULL AND x.secret_ct IS NOT NULL
    AND x.ticket_expires_at > now()
  FOR UPDATE;
  IF NOT FOUND THEN RETURN QUERY SELECT 'none', 0; RETURN; END IF;
  IF f.failed_attempts >= 10 THEN RETURN QUERY SELECT 'locked', f.failed_attempts; RETURN; END IF;
  IF f.proof_hash = sha256(p_proof) AND p_step BETWEEN v_now - 1 AND v_now + 1 THEN
    UPDATE second_factors x SET confirmed_at = now(), last_step = p_step, failed_attempts = 0,
      ticket_hash = NULL, ticket_expires_at = NULL,
      recovery_code_hashes = ARRAY(SELECT sha256(c) FROM unnest(p_code_proofs) AS c)
    WHERE x.user_id = v_user;
    -- Whoever else held this admin's password and a half session is out.
    DELETE FROM admin_sessions s WHERE s.user_id = v_user AND s.id_hash <> p_id_hash;
    UPDATE admin_sessions s SET second_factor_at = now() WHERE s.id_hash = p_id_hash;
    RETURN QUERY SELECT 'enrolled', 0;
    RETURN;
  END IF;
  -- A password holder must not be able to guess-confirm the operator's half-finished enrolment.
  UPDATE second_factors x SET failed_attempts = x.failed_attempts + 1 WHERE x.user_id = v_user;
  RETURN QUERY SELECT 'failed', f.failed_attempts + 1;
END $$""",
    "pass_second_factor(bytea,bytea,bigint)": f"""
CREATE FUNCTION pass_second_factor(p_id_hash bytea, p_proof bytea, p_step bigint)
RETURNS TABLE (outcome text, failures integer)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- 'passed' | 'failed' | 'locked' | 'none'. The app passes p_step NULL when no step matched the
-- code; that counts as a failure. A step is accepted once, and only within now +-1 step.
DECLARE
  v_user uuid := live_admin_user(p_id_hash);
  v_now bigint := {NOW_STEP};
  f second_factors;
BEGIN
  SELECT * INTO f FROM second_factors x
  WHERE x.user_id = v_user AND x.confirmed_at IS NOT NULL
  FOR UPDATE;  -- concurrent codes serialise
  IF NOT FOUND THEN RETURN QUERY SELECT 'none', 0; RETURN; END IF;
  IF f.failed_attempts >= 10 THEN RETURN QUERY SELECT 'locked', f.failed_attempts; RETURN; END IF;
  IF f.proof_hash = sha256(p_proof) AND p_step > f.last_step
     AND p_step BETWEEN v_now - 1 AND v_now + 1 THEN
    UPDATE second_factors x SET last_step = p_step, failed_attempts = 0 WHERE x.user_id = v_user;
    UPDATE admin_sessions s SET second_factor_at = now() WHERE s.id_hash = p_id_hash;
    RETURN QUERY SELECT 'passed', f.failed_attempts;  -- the failures since the last success
    RETURN;
  END IF;
  UPDATE second_factors x SET failed_attempts = x.failed_attempts + 1 WHERE x.user_id = v_user;
  RETURN QUERY SELECT 'failed', f.failed_attempts + 1;  -- >= 10: this failure locked it
END $$""",
    "use_recovery_code(bytea,bytea)": f"""
CREATE FUNCTION use_recovery_code(p_id_hash bytea, p_code_proof bytea)
RETURNS TABLE (codes_left integer, was_locked boolean)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- No row: no match. A match uses the code up, unlocks TOTP and passes this session's factor.
DECLARE v_user uuid := live_admin_user(p_id_hash);
BEGIN
  RETURN QUERY
    UPDATE second_factors f
    SET recovery_code_hashes = array_remove(f.recovery_code_hashes, sha256(p_code_proof)),
        failed_attempts = 0
    WHERE f.user_id = v_user AND f.confirmed_at IS NOT NULL
      AND sha256(p_code_proof) = ANY (f.recovery_code_hashes)
    RETURNING cardinality(new.recovery_code_hashes), old.failed_attempts >= 10;  -- PG 18
  IF FOUND THEN
    UPDATE admin_sessions s SET second_factor_at = now() WHERE s.id_hash = p_id_hash;
  END IF;
END $$""",
}


def grant(signature: str) -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")


def upgrade() -> None:
    # NULL: only the password passed.
    op.execute("ALTER TABLE admin_sessions ADD COLUMN second_factor_at timestamptz")
    op.execute("""
    CREATE TABLE second_factors (
      -- users, not platform_admins: business owners can opt in later.
      user_id uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
      ticket_hash bytea CHECK (octet_length(ticket_hash) = 32),  -- sha256(sha256(ticket))
      ticket_expires_at timestamptz,
      secret_ct bytea,  -- version byte || nonce || AES-GCM ciphertext and tag; the key never here
      proof_hash bytea CHECK (octet_length(proof_hash) = 32),  -- sha256(sha256(secret))
      confirmed_at timestamptz,  -- NULL with secret_ct set: enrolment begun, not confirmed
      last_step bigint NOT NULL DEFAULT 0,  -- replay: accepted steps only go up
      failed_attempts integer NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
      recovery_code_hashes bytea[] NOT NULL DEFAULT '{}',  -- sha256(sha256(code)) each
      CONSTRAINT ticket_whole CHECK ((ticket_hash IS NULL) = (ticket_expires_at IS NULL)),
      CONSTRAINT secret_whole CHECK ((secret_ct IS NULL) = (proof_hash IS NULL)),
      CONSTRAINT confirmed_has_secret CHECK (confirmed_at IS NULL OR secret_ct IS NOT NULL)
    )""")
    # Default privileges from 0001 gave the app role DML on every new table; none comes back.
    op.execute("REVOKE ALL ON second_factors FROM ziftbook_app")
    op.execute(LIVE_ADMIN_USER)
    op.execute("REVOKE EXECUTE ON FUNCTION live_admin_user(bytea) FROM PUBLIC")
    # Its OUT columns change: CREATE OR REPLACE would fail (42P13).
    op.execute("DROP FUNCTION resolve_admin_session(bytea)")
    for signature, definition in FUNCTIONS.items():
        op.execute(definition)
        grant(signature)


def downgrade() -> None:
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("DROP FUNCTION live_admin_user(bytea)")
    op.execute(RESOLVE_0037)
    grant("resolve_admin_session(bytea)")
    op.execute("DROP TABLE second_factors")
    op.execute("ALTER TABLE admin_sessions DROP COLUMN second_factor_at")
