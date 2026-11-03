"""An admin's TOTP attempt counts in a transaction of its own, before its code is checked (ZIF-261).

Until now a wrong code was counted in the transaction that checked it, and only a pass also writes
admin_sessions. Someone who can run SQL as the app role can tell the two apart before the commit and
cut a failing one off, so its count rolls back and the ten-code lock never comes. Now an attempt is
charged first and checked second:

- charge_second_factor counts the attempt as a failure, by 0069's rules (the leak, the lock at 10),
  and answers a charge. It takes no code, so its transaction looks the same for every code. The API
  commits it before it checks anything.
- check_second_factor checks the code against that charge. The charge is used up either way; only a
  pass changes the count, giving back the one attempt its charge counted. A check cut off before its
  commit leaves the charge counted.
- A charge lives in second_factors.charges for 2 minutes: the check comes in the same request.

Everything else that resets the secret or the count voids the charges in flight, so no charge made
before it is checked after it: begin_totp_enrolment, confirm_totp_enrolment, use_recovery_code when
it lifts a lock (replaced in place) and the CLI's new ticket. A recovery code with no lock leaves
the count, and the charges with it.

pass_second_factor stays as it is for the release still running during the deploy; revoking and
dropping it is a later, contract migration.

The downgrade restores the three replaced bodies and drops the column and both functions.

Revision ID: 0083
Revises: 0069
"""

from alembic import op

revision = "0083"
down_revision = "0069"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
# The current 30-second TOTP step, by the database's clock.
NOW_STEP = "floor(extract(epoch FROM now()) / 30)::bigint"
# One failure leaks away per this many seconds (0069).
LEAK_SECONDS = 8 * 3600
LEAK = f"make_interval(secs => {LEAK_SECONDS})"
# A charge is a uuidv7: its own timestamp says when it was made. It lives 2 minutes.
CHARGE_LIFE = "interval '2 minutes'"

FUNCTIONS = {
    "charge_second_factor(bytea)": f"""
CREATE FUNCTION charge_second_factor(p_id_hash bytea)
RETURNS TABLE (outcome text, failures integer, charge uuid)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- 'charged' | 'locked' | 'none'. Counts one TOTP attempt as a failure before its code is checked
-- (ZIF-261), by 0069's rules: the caller commits this, then calls check_second_factor with the
-- charge. Takes no code, so its transaction is the same for every code. failures: the count.
DECLARE
  v_user uuid := live_admin_user(p_id_hash);
  f second_factors;
  v_anchor timestamptz;
  v_leaked bigint;
  v_level integer;
  v_charge uuid := uuidv7();
BEGIN
  SELECT * INTO f FROM second_factors x
  WHERE x.user_id = v_user AND x.confirmed_at IS NOT NULL
  FOR UPDATE;  -- concurrent attempts serialise, and every one counts
  IF NOT FOUND THEN RETURN QUERY SELECT 'none', 0, NULL::uuid; RETURN; END IF;
  -- Before any leak: a lock never leaks away. A recovery code or app.admin enrol lifts it.
  IF f.failed_attempts >= 10 THEN
    RETURN QUERY SELECT 'locked', f.failed_attempts, NULL::uuid;
    RETURN;
  END IF;
  -- 0069's failure: whole intervals since the anchor, never below 0; an emptied count restarts its
  -- leak now, otherwise the anchor moves by whole intervals only.
  v_anchor := coalesce(f.failures_leaked_at, now());
  v_leaked := greatest(floor(extract(epoch FROM now() - v_anchor) / {LEAK_SECONDS}), 0);
  v_level := f.failed_attempts - least(v_leaked, f.failed_attempts);
  v_anchor := CASE WHEN v_level = 0 THEN now() ELSE v_anchor + v_leaked * {LEAK} END;
  UPDATE second_factors x SET failed_attempts = v_level + 1, failures_leaked_at = v_anchor,
    failures_since_pass = f.failures_since_pass + 1,
    -- Expired charges go: their checks never came, and the count keeps them.
    charges = ARRAY(SELECT c FROM unnest(f.charges) AS c
                    WHERE uuid_extract_timestamp(c) > now() - {CHARGE_LIFE}) || v_charge
  WHERE x.user_id = v_user;
  RETURN QUERY SELECT 'charged', v_level + 1, v_charge;  -- 10: this attempt locks the next
END $$""",
    "check_second_factor(bytea,uuid,bytea,bigint)": f"""
CREATE FUNCTION check_second_factor(p_id_hash bytea, p_charge uuid, p_proof bytea, p_step bigint)
RETURNS TABLE (outcome text, failures integer)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- 'passed' | 'failed' | 'none'. Checks a code charge_second_factor already counted (ZIF-261).
-- 'none': no confirmed factor, or p_charge is no live charge of this admin (never made, already
-- checked, expired or voided), and nothing is looked at. The charge is used up either way, and only
-- a pass changes the count: it gives back the one attempt its charge counted. The app passes p_step
-- NULL when no step matched the code. A step is accepted once, and only within now +-1 step.
DECLARE
  v_user uuid := live_admin_user(p_id_hash);
  v_now bigint := {NOW_STEP};
  f second_factors;
BEGIN
  SELECT * INTO f FROM second_factors x
  WHERE x.user_id = v_user AND x.confirmed_at IS NOT NULL
  FOR UPDATE;  -- concurrent codes serialise
  IF NOT FOUND THEN RETURN QUERY SELECT 'none', 0; RETURN; END IF;
  -- coalesce: a NULL charge is none too, never a check.
  IF NOT coalesce(p_charge = ANY (f.charges)
                  AND uuid_extract_timestamp(p_charge) > now() - {CHARGE_LIFE}, false) THEN
    RETURN QUERY SELECT 'none', 0;
    RETURN;
  END IF;
  IF f.proof_hash = sha256(p_proof) AND p_step > f.last_step
     AND p_step BETWEEN v_now - 1 AND v_now + 1 THEN
    -- The leak anchor stays: only time lowers the count (0069).
    UPDATE second_factors x SET last_step = p_step, failures_since_pass = 0,
      failed_attempts = greatest(x.failed_attempts - 1, 0),
      charges = array_remove(x.charges, p_charge)
    WHERE x.user_id = v_user;
    UPDATE admin_sessions s SET second_factor_at = now() WHERE s.id_hash = p_id_hash;
    -- The failures since the last pass, this attempt's charge not among them.
    RETURN QUERY SELECT 'passed', greatest(f.failures_since_pass - 1, 0);
    RETURN;
  END IF;
  UPDATE second_factors x SET charges = array_remove(x.charges, p_charge) WHERE x.user_id = v_user;
  RETURN QUERY SELECT 'failed', f.failed_attempts;  -- the charge counted it; 10: it locked
END $$""",
}

# Every other place that resets the secret or the count voids the attempts in flight, so a charge
# made before it is never checked after it. Replaced in place: same arguments and results.
REPLACED = {
    "begin_totp_enrolment(bytea,bytea,bytea,bytea)": f"""
CREATE OR REPLACE FUNCTION begin_totp_enrolment(
  p_id_hash bytea, p_ticket_proof bytea, p_secret_ct bytea, p_proof bytea)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- 'begun' | 'invalid_ticket' | 'already_enrolled'. Stores a new, unconfirmed secret for the session
-- user, given the CLI's ticket. Voids the attempts in flight: none is checked against the new
-- secret (0083).
DECLARE v_user uuid := live_admin_user(p_id_hash);
BEGIN
  -- Never overwrites a confirmed secret: re-enrolling goes through the CLI.
  IF EXISTS (SELECT FROM second_factors f WHERE f.user_id = v_user AND f.confirmed_at IS NOT NULL)
  THEN
    RETURN 'already_enrolled';
  END IF;
  UPDATE second_factors f SET secret_ct = p_secret_ct, proof_hash = sha256(p_proof),
                              last_step = 0, failed_attempts = 0, charges = '{{}}'
  WHERE f.user_id = v_user AND f.ticket_hash = sha256(p_ticket_proof)
    AND f.ticket_expires_at > now();
  -- The ticket is not used up here: an abandoned QR can be restarted within the hour.
  RETURN CASE WHEN FOUND THEN 'begun' ELSE 'invalid_ticket' END;
END $$""",
    "confirm_totp_enrolment(bytea,bytea,bigint,bytea[])": f"""
CREATE OR REPLACE FUNCTION confirm_totp_enrolment(
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
    -- A new secret starts with no failures, whichever CLI version issued the ticket (0069), and no
    -- attempt in flight (0083).
    UPDATE second_factors x SET confirmed_at = now(), last_step = p_step, failed_attempts = 0,
      failures_leaked_at = NULL, failures_since_pass = 0, charges = '{{}}',
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
    "use_recovery_code(bytea,bytea)": f"""
CREATE OR REPLACE FUNCTION use_recovery_code(p_id_hash bytea, p_code_proof bytea)
RETURNS TABLE (codes_left integer, was_locked boolean)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- No row: no match. A match uses the code up, lifts a TOTP lock and passes this session's factor.
-- Lifting a lock leaves 9, leaking from now: no pass refills the count (0069), and it voids the
-- attempts in flight, so no pass gives back one the 9 already forgot (0083).
DECLARE v_user uuid := live_admin_user(p_id_hash);
BEGIN
  RETURN QUERY
    UPDATE second_factors f
    SET recovery_code_hashes = array_remove(f.recovery_code_hashes, sha256(p_code_proof)),
        failed_attempts = least(f.failed_attempts, 9),
        failures_leaked_at = CASE WHEN f.failed_attempts >= 10 THEN now()
                                  ELSE f.failures_leaked_at END,
        failures_since_pass = 0,
        charges = CASE WHEN f.failed_attempts >= 10 THEN '{{}}' ELSE f.charges END
    WHERE f.user_id = v_user AND f.confirmed_at IS NOT NULL
      AND sha256(p_code_proof) = ANY (f.recovery_code_hashes)
    RETURNING cardinality(new.recovery_code_hashes), old.failed_attempts >= 10;  -- PG 18
  IF FOUND THEN
    UPDATE admin_sessions s SET second_factor_at = now() WHERE s.id_hash = p_id_hash;
  END IF;
END $$""",
}

# What they were (0039's begin, 0069's others), verbatim, for the downgrade.
REPLACED_BEFORE = {
    "begin_totp_enrolment(bytea,bytea,bytea,bytea)": f"""
CREATE OR REPLACE FUNCTION begin_totp_enrolment(
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
CREATE OR REPLACE FUNCTION confirm_totp_enrolment(
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
    -- A new secret starts with no failures, whichever CLI version issued the ticket (0069).
    UPDATE second_factors x SET confirmed_at = now(), last_step = p_step, failed_attempts = 0,
      failures_leaked_at = NULL, failures_since_pass = 0,
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
    "use_recovery_code(bytea,bytea)": f"""
CREATE OR REPLACE FUNCTION use_recovery_code(p_id_hash bytea, p_code_proof bytea)
RETURNS TABLE (codes_left integer, was_locked boolean)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- No row: no match. A match uses the code up, lifts a TOTP lock and passes this session's factor.
-- Lifting a lock leaves 9, leaking from now: no pass refills the count (0069).
DECLARE v_user uuid := live_admin_user(p_id_hash);
BEGIN
  RETURN QUERY
    UPDATE second_factors f
    SET recovery_code_hashes = array_remove(f.recovery_code_hashes, sha256(p_code_proof)),
        failed_attempts = least(f.failed_attempts, 9),
        failures_leaked_at = CASE WHEN f.failed_attempts >= 10 THEN now()
                                  ELSE f.failures_leaked_at END,
        failures_since_pass = 0
    WHERE f.user_id = v_user AND f.confirmed_at IS NOT NULL
      AND sha256(p_code_proof) = ANY (f.recovery_code_hashes)
    RETURNING cardinality(new.recovery_code_hashes), old.failed_attempts >= 10;  -- PG 18
  IF FOUND THEN
    UPDATE admin_sessions s SET second_factor_at = now() WHERE s.id_hash = p_id_hash;
  END IF;
END $$""",
}


def install(functions: dict[str, str]) -> None:
    for signature, definition in functions.items():
        op.execute(definition)
        # A replaced function keeps its grants; restated anyway so they are plain to see.
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")


def upgrade() -> None:
    op.execute("""
    ALTER TABLE second_factors
      -- The attempts charge_second_factor counted whose check has not come yet: uuidv7s.
      ADD COLUMN charges uuid[] NOT NULL DEFAULT '{}'
    """)
    install(FUNCTIONS)
    install(REPLACED)


def downgrade() -> None:
    install(REPLACED_BEFORE)
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("ALTER TABLE second_factors DROP COLUMN charges")
