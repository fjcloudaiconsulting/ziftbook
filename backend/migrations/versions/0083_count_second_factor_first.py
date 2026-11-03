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

pass_second_factor stays as it is for the release still running during the deploy; revoking and
dropping it is a later, contract migration.

The downgrade drops the column and both functions.

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


def upgrade() -> None:
    op.execute("""
    ALTER TABLE second_factors
      -- The attempts charge_second_factor counted whose check has not come yet: uuidv7s.
      ADD COLUMN charges uuid[] NOT NULL DEFAULT '{}'
    """)
    for signature, definition in FUNCTIONS.items():
        op.execute(definition)
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")


def downgrade() -> None:
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("ALTER TABLE second_factors DROP COLUMN charges")
