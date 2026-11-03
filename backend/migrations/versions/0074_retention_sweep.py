"""The worker's retention functions (ZIF-235).

Four SECURITY DEFINER functions owned by ziftbook_migrate, executable by ziftbook_worker only
(never ziftbook_app, never PUBLIC). The caller chooses no period: each is fixed here, and the
worker (app.retention) calls them in batches on its hourly sweep.

- sweep_audit_events(): deletes, 1000 at a time each, failed sign-ins older than 30 days and every
  other event older than a year, in audit_events (through 0073's policies, opened by
  app.retention_sweep for the call only) and in platform_audit_events (owner ruling D3: its three
  failure actions at 30 days, the rest, business_read included, at a year). A non-blocking
  advisory lock (235, 0) per call: a second replica gets busy = true at once and changes nothing.
  app.audit_review is cleared for the call (CONTRIBUTING, "Audit log") and the caller's settings
  are restored. No EXCEPTION handler and no RETURN after the first set_config: on an error the
  transaction's rollback restores them.
- sweep_admin_sessions(): deletes admin sessions an hour after they died (expired, or idle past
  resolve_admin_session's 30 minutes). The hour is a margin: no reader whose transaction began
  before the sweep can still see a deleted session as live.
- sweep_account_sessions(): deletes account sessions (ZIF-146, 0057) an hour after they died
  (expired, or idle past 0057's 7 days), with the same margin. FOR UPDATE SKIP LOCKED: a booking
  that holds its session FOR KEY SHARE (hold_account_session, 0058) is skipped, never waited on.
  One a console sign-in started also goes with its business session (0057's foreign key).
- sweep_booking_origins(p_from): sets ip and user_agent of a booking event to NULL once both the
  event and its booking's end are 18 months old (owner ruling D2). The row stays: it is evidence.
  Business by business under tenant_isolation, as 0058's own_bookings reads (no policy widens what
  anyone reads across businesses), FOR UPDATE SKIP LOCKED (never waits on, or deadlocks with, a
  transaction that holds an event), at most 1000 rows and 200 businesses per call. next_tenant is
  where the next call resumes (inclusive), NULL once the walk has passed the last business.
  An ended booking a merchant later moves into the future counts again in the ZB-19 per-IP pending
  cap with its cleared address: then only toward the cap across addresses.

No table is locked by this migration: CREATE FUNCTION, REVOKE and GRANT only.

The downgrade drops the four functions. Deleted events and cleared addresses do not come back.

Revision ID: 0074
Revises: 0073
"""

from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
# Copied, not imported: 0072 and app.retention state the same list, and tests compare them.
PLATFORM_FAILURES = "'admin_sign_in_failed', 'second_factor_failed', 'totp_enrolment_failed'"

FUNCTIONS = {
    "sweep_audit_events()": f"""
CREATE FUNCTION sweep_audit_events(
  OUT busy boolean, OUT failed_sign_ins integer, OUT older_events integer,
  OUT platform_failures integer, OUT platform_events integer)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
DECLARE
  v_flag text := current_setting('app.retention_sweep', true);
  v_review text := current_setting('app.audit_review', true);
  v_tenant text := current_setting('app.tenant_id', true);
BEGIN
  failed_sign_ins := 0; older_events := 0; platform_failures := 0; platform_events := 0;
  busy := NOT pg_try_advisory_xact_lock(235, 0);
  IF busy THEN RETURN; END IF;
  PERFORM set_config('app.retention_sweep', 'on', true);
  PERFORM set_config('app.audit_review', '', true);
  -- tenant_isolation must not raise; this business matches no event.
  PERFORM set_config('app.tenant_id', '00000000-0000-0000-0000-000000000000', true);
  -- = ANY(ARRAY(...)), not IN (...): the planner may run an IN subquery with LIMIT more than once.
  DELETE FROM audit_events e WHERE e.id = ANY(ARRAY(
    SELECT x.id FROM audit_events x
    WHERE x.action = 'sign_in_failed' AND x.created_at < now() - interval '30 days'
    ORDER BY x.created_at LIMIT 1000));
  GET DIAGNOSTICS failed_sign_ins = ROW_COUNT;
  DELETE FROM audit_events e WHERE e.id = ANY(ARRAY(
    SELECT x.id FROM audit_events x WHERE x.created_at < now() - interval '1 year'
    ORDER BY x.created_at LIMIT 1000));
  GET DIAGNOSTICS older_events = ROW_COUNT;
  DELETE FROM platform_audit_events e WHERE e.id = ANY(ARRAY(
    SELECT x.id FROM platform_audit_events x
    WHERE x.action IN ({PLATFORM_FAILURES}) AND x.created_at < now() - interval '30 days'
    ORDER BY x.created_at LIMIT 1000));
  GET DIAGNOSTICS platform_failures = ROW_COUNT;
  DELETE FROM platform_audit_events e WHERE e.id = ANY(ARRAY(
    SELECT x.id FROM platform_audit_events x WHERE x.created_at < now() - interval '1 year'
    ORDER BY x.created_at LIMIT 1000));
  GET DIAGNOSTICS platform_events = ROW_COUNT;
  PERFORM set_config('app.retention_sweep', coalesce(v_flag, ''), true);
  PERFORM set_config('app.audit_review', coalesce(v_review, ''), true);
  PERFORM set_config('app.tenant_id', coalesce(v_tenant, ''), true);
END $$""",
    "sweep_admin_sessions()": f"""
CREATE FUNCTION sweep_admin_sessions(OUT deleted integer)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
BEGIN
  DELETE FROM admin_sessions s WHERE s.id_hash = ANY(ARRAY(
    SELECT x.id_hash FROM admin_sessions x
    WHERE x.expires_at <= now() - interval '1 hour'
       OR x.last_seen_at <= now() - interval '90 minutes'
    LIMIT 1000 FOR UPDATE SKIP LOCKED));
  GET DIAGNOSTICS deleted = ROW_COUNT;
END $$""",
    "sweep_account_sessions()": f"""
CREATE FUNCTION sweep_account_sessions(OUT deleted integer)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
BEGIN
  DELETE FROM account_sessions s WHERE s.id_hash = ANY(ARRAY(
    SELECT x.id_hash FROM account_sessions x
    WHERE x.expires_at <= now() - interval '1 hour'
       OR x.last_seen_at <= now() - interval '7 days 1 hour'
    LIMIT 1000 FOR UPDATE SKIP LOCKED));
  GET DIAGNOSTICS deleted = ROW_COUNT;
END $$""",
    "sweep_booking_origins(uuid)": f"""
CREATE FUNCTION sweep_booking_origins(p_from uuid, OUT cleared integer, OUT next_tenant uuid)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
DECLARE
  v_tenant text := current_setting('app.tenant_id', true);
  v_t uuid;
  v_n integer;
  v_visited integer := 0;
BEGIN
  cleared := 0;
  next_tenant := NULL;
  FOR v_t IN SELECT t.id FROM tenants t WHERE p_from IS NULL OR t.id >= p_from ORDER BY t.id LOOP
    IF v_visited >= 200 THEN
      next_tenant := v_t;
      EXIT;
    END IF;
    v_visited := v_visited + 1;
    -- Under this business's tenant_isolation: no policy reaches another business's rows.
    PERFORM set_config('app.tenant_id', v_t::text, true);
    UPDATE booking_events e SET ip = NULL, user_agent = NULL
    WHERE e.tenant_id = v_t AND e.id = ANY(ARRAY(
      SELECT x.id FROM booking_events x
      JOIN bookings b ON b.tenant_id = x.tenant_id AND b.id = x.booking_id
      WHERE x.tenant_id = v_t AND (x.ip IS NOT NULL OR x.user_agent IS NOT NULL)
        AND x.created_at < now() - interval '18 months'
        AND b.ends_at < now() - interval '18 months'
      ORDER BY x.created_at LIMIT 1000 - cleared
      FOR UPDATE OF x SKIP LOCKED));
    GET DIAGNOSTICS v_n = ROW_COUNT;
    cleared := cleared + v_n;
    IF cleared >= 1000 THEN
      next_tenant := v_t;  -- it may have more: the next call starts here
      EXIT;
    END IF;
  END LOOP;
  PERFORM set_config('app.tenant_id', coalesce(v_tenant, ''), true);
END $$""",
}


def upgrade() -> None:
    for signature, definition in FUNCTIONS.items():
        op.execute(definition)
        # 0048's default privileges already give PUBLIC nothing; said here as well, before the
        # GRANT it narrows to.
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_worker")


def downgrade() -> None:
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
