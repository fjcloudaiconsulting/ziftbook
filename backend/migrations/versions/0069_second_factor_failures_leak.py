"""Wrong admin second-factor codes leak away with time instead of resetting on a pass (ZIF-234).

Until now a pass, or a recovery code, set second_factors.failed_attempts back to 0, so the ten
wrong codes that lock TOTP were ten per pass, with no bound over time. Now:

- failed_attempts is a count that only time lowers: one failure leaks away per 8 hours since
  failures_leaked_at, the anchor, which moves by whole intervals so a part interval still counts.
- 10 still locks, and a lock never leaks away. The only ways out are a recovery code or
  `python -m app.admin enrol`; neither time nor a password reset unlocks.
- A pass leaves the count and its anchor alone.
- A recovery code lifts a lock but leaves 9, leaking from now: the next wrong code within 8 hours
  locks again. Off a lock it leaves the count alone.
- failures_since_pass counts the failures since the last pass of either kind. It is only reported:
  pass_second_factor's `passed` answers it, as 0039's answered the failures since the last success,
  so the audit's failed_since_last keeps its meaning. `failed` answers the count, which locks at 10.
- Confirming an enrolment clears all three (a new secret, a new budget), whichever CLI version
  issued its ticket; the CLI's upsert resets only failed_attempts.

pass_second_factor, use_recovery_code and confirm_totp_enrolment are replaced in place: same
arguments and result columns, so their owner and grants stay, the API needs no change and the
release still running during the deploy keeps working. The grants are restated anyway.

The bound holds while each failure's transaction commits. One whose transaction never commits is
not counted (the API logs every wrong code before its commit); counting first, in a transaction of
its own, is ZIF-261.

Deploy: a use_recovery_code call that blocks on this migration's table lock finishes 0039's body
after the commit, so it may reset one count to 0, once. The downgrade restores 0039's bodies,
lowers each enrolled factor's unlocked count to its failures since the last pass (0039's meaning;
never higher, locks kept; a pending enrolment's count of wrong confirm codes stays as it is), and
drops both columns.

Revision ID: 0069
Revises: 0056
"""

from alembic import op

revision = "0069"
down_revision = "0056"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
# The current 30-second TOTP step, by the database's clock.
NOW_STEP = "floor(extract(epoch FROM now()) / 30)::bigint"
# One failure leaks away per this many seconds.
LEAK_SECONDS = 8 * 3600
LEAK = f"make_interval(secs => {LEAK_SECONDS})"

FUNCTIONS = {
    "pass_second_factor(bytea,bytea,bigint)": f"""
CREATE OR REPLACE FUNCTION pass_second_factor(p_id_hash bytea, p_proof bytea, p_step bigint)
RETURNS TABLE (outcome text, failures integer)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- 'passed' | 'failed' | 'locked' | 'none'. The app passes p_step NULL when no step matched the
-- code; that counts as a failure. A step is accepted once, and only within now +-1 step.
-- A pass never lowers the count: one failure leaks away per 8 hours, and 10 lock (0069).
DECLARE
  v_user uuid := live_admin_user(p_id_hash);
  v_now bigint := {NOW_STEP};
  f second_factors;
  v_anchor timestamptz;
  v_leaked bigint;
  v_level integer;
BEGIN
  SELECT * INTO f FROM second_factors x
  WHERE x.user_id = v_user AND x.confirmed_at IS NOT NULL
  FOR UPDATE;  -- concurrent codes serialise, and every failure counts
  IF NOT FOUND THEN RETURN QUERY SELECT 'none', 0; RETURN; END IF;
  -- Before any leak: a lock never leaks away. A recovery code or app.admin enrol lifts it.
  IF f.failed_attempts >= 10 THEN RETURN QUERY SELECT 'locked', f.failed_attempts; RETURN; END IF;
  IF f.proof_hash = sha256(p_proof) AND p_step > f.last_step
     AND p_step BETWEEN v_now - 1 AND v_now + 1 THEN
    -- The count and its anchor stay: only time lowers it.
    UPDATE second_factors x SET last_step = p_step, failures_since_pass = 0
    WHERE x.user_id = v_user;
    UPDATE admin_sessions s SET second_factor_at = now() WHERE s.id_hash = p_id_hash;
    RETURN QUERY SELECT 'passed', f.failures_since_pass;  -- the failures since the last pass
    RETURN;
  END IF;
  -- Whole intervals since the anchor, never below 0: now() is this transaction's start, which can
  -- be before the anchor another transaction just wrote. No anchor: none has leaked.
  v_anchor := coalesce(f.failures_leaked_at, now());
  v_leaked := greatest(floor(extract(epoch FROM now() - v_anchor) / {LEAK_SECONDS}), 0);
  v_level := f.failed_attempts - least(v_leaked, f.failed_attempts);
  -- Emptied: the leak starts again now. Otherwise the anchor moves by whole intervals only.
  v_anchor := CASE WHEN v_level = 0 THEN now() ELSE v_anchor + v_leaked * {LEAK} END;
  UPDATE second_factors x SET failed_attempts = v_level + 1, failures_leaked_at = v_anchor,
                              failures_since_pass = f.failures_since_pass + 1
  WHERE x.user_id = v_user;
  RETURN QUERY SELECT 'failed', v_level + 1;  -- 10: this failure locked it
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
}

# 0039's three bodies, verbatim, as CREATE OR REPLACE, for the downgrade.
FUNCTIONS_0039 = {
    "pass_second_factor(bytea,bytea,bigint)": f"""
CREATE OR REPLACE FUNCTION pass_second_factor(p_id_hash bytea, p_proof bytea, p_step bigint)
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
CREATE OR REPLACE FUNCTION use_recovery_code(p_id_hash bytea, p_code_proof bytea)
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
}


def install(functions: dict[str, str]) -> None:
    for signature, definition in functions.items():
        op.execute(definition)
        # Unchanged by CREATE OR REPLACE; restated so the grant is plain to see. REVOKE first.
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")


def upgrade() -> None:
    op.execute("""
    ALTER TABLE second_factors
      -- Where failed_attempts' leak counts from; NULL while nothing has failed.
      ADD COLUMN failures_leaked_at timestamptz
        CONSTRAINT failures_leaked_at_finite
        CHECK (failures_leaked_at IS NULL OR isfinite(failures_leaked_at)),
      -- The failures since the last pass, only reported.
      ADD COLUMN failures_since_pass integer NOT NULL DEFAULT 0
        CONSTRAINT failures_since_pass_counts CHECK (failures_since_pass >= 0)
    """)
    # A count already there starts leaking now, never earlier, and is all since the last pass
    # (0039 reset it on every pass).
    op.execute("""
    UPDATE second_factors SET failures_leaked_at = now(), failures_since_pass = failed_attempts
    WHERE failed_attempts > 0
    """)
    install(FUNCTIONS)


def downgrade() -> None:
    install(FUNCTIONS_0039)
    # 0039 reads an enrolled factor's failed_attempts as the failures since the last success: never
    # raise a count, keep every lock. A pending enrolment's count is its wrong confirm codes, which
    # failures_since_pass never counted: it stays.
    op.execute("""
    UPDATE second_factors SET failed_attempts = least(failed_attempts, failures_since_pass)
    WHERE failed_attempts < 10 AND confirmed_at IS NOT NULL
    """)
    op.execute("""
    ALTER TABLE second_factors DROP COLUMN failures_since_pass, DROP COLUMN failures_leaked_at
    """)
