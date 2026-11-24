"""Expand calendar credentials and OAuth attempts without enabling enrollment (ZIF-65).

Revision ID: 0109
Revises: 0112
"""

from alembic import op

from app.db import enable_tenant_isolation

revision = "0109"
down_revision = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE google_calendar_connections (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  member_id uuid NOT NULL,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  key_id text,
  wrapped_dek bytea,
  wrap_nonce bytea,
  payload_nonce bytea,
  ciphertext bytea,
  format_version integer NOT NULL DEFAULT 1 CHECK (format_version = 1),
  wrap_revision bigint NOT NULL DEFAULT 1 CHECK (wrap_revision > 0),
  token_revision bigint NOT NULL DEFAULT 1 CHECK (token_revision > 0),
  -- Immutable AAD input, independent of the operational generation used to fence work.
  encryption_generation bigint NOT NULL DEFAULT 1 CHECK (encryption_generation > 0),
  generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
  state text NOT NULL DEFAULT 'initializing'
    CHECK (state IN ('initializing', 'fresh', 'stale', 'reconnect_required',
                     'revocation_pending', 'revoked')),
  next_sync_at timestamptz,
  next_revoke_at timestamptz,
  scheduling_state text NOT NULL DEFAULT 'runnable'
    CHECK (scheduling_state IN ('runnable', 'parked')),
  park_reason text
    CHECK (park_reason IN ('membership', 'tenant', 'configuration', 'authorization')),
  recheck_at timestamptz,
  fetch_started_at timestamptz,
  last_attempt_at timestamptz,
  last_success_at timestamptz,
  coverage_start timestamptz,
  coverage_end timestamptz,
  failure_count integer NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
  failure_episode uuid,
  last_notified_episode uuid,
  error_category text CHECK (error_category IN
    ('transient', 'quota', 'authorization', 'configuration',
     'crypto', 'invalid_response', 'missing')),
  lease_id uuid,
  lease_until timestamptz,
  lease_sequence bigint NOT NULL DEFAULT 0 CHECK (lease_sequence >= 0),
  disconnected_at timestamptz,
  revoked_at timestamptz,
  UNIQUE (tenant_id, member_id),
  UNIQUE (tenant_id, id, member_id),
  FOREIGN KEY (tenant_id, member_id) REFERENCES memberships(tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_calendar_connection_envelope CHECK (
    (state = 'revoked' AND
      (key_id, wrapped_dek, wrap_nonce, payload_nonce, ciphertext) IS NULL)
    OR (state <> 'revoked' AND
      (key_id, wrapped_dek, wrap_nonce, payload_nonce, ciphertext) IS NOT NULL
      AND length(key_id) BETWEEN 1 AND 128 AND octet_length(wrapped_dek) = 48
      AND octet_length(wrap_nonce) = 12 AND octet_length(payload_nonce) = 12
      AND octet_length(ciphertext) >= 16)),
  CONSTRAINT ck_calendar_connection_coverage CHECK (
    (coverage_start, coverage_end) IS NULL OR
    ((coverage_start, coverage_end) IS NOT NULL AND isfinite(coverage_start)
     AND isfinite(coverage_end) AND coverage_end > coverage_start)),
  CONSTRAINT ck_calendar_connection_lease CHECK (
    (lease_id, lease_until) IS NULL OR (lease_id, lease_until) IS NOT NULL),
  CONSTRAINT ck_calendar_connection_parking CHECK (
    (scheduling_state = 'runnable' AND park_reason IS NULL AND recheck_at IS NULL) OR
    (scheduling_state = 'parked' AND park_reason IS NOT NULL AND recheck_at IS NOT NULL)),
  CONSTRAINT ck_calendar_connection_revocation CHECK (
    (state NOT IN ('revocation_pending', 'revoked')
     AND disconnected_at IS NULL AND revoked_at IS NULL)
    OR (state = 'revocation_pending' AND disconnected_at IS NOT NULL AND revoked_at IS NULL
        AND next_revoke_at IS NOT NULL AND scheduling_state = 'runnable')
    OR (state = 'revoked' AND disconnected_at IS NOT NULL AND revoked_at IS NOT NULL))
)
""")
    enable_tenant_isolation("google_calendar_connections")
    op.execute("""
CREATE INDEX ix_calendar_due_sync ON google_calendar_connections(next_sync_at, id)
  WHERE scheduling_state = 'runnable' AND state IN ('initializing', 'fresh', 'stale');
CREATE INDEX ix_calendar_due_revoke ON google_calendar_connections(next_revoke_at, id)
  WHERE scheduling_state = 'runnable' AND state = 'revocation_pending';
CREATE INDEX ix_calendar_recheck ON google_calendar_connections(recheck_at, id)
  WHERE scheduling_state = 'parked';
CREATE TABLE google_calendar_oauth_attempts (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  member_id uuid NOT NULL,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  connection_id uuid,
  connection_generation bigint CHECK (connection_generation > 0),
  state_hash bytea NOT NULL CHECK (octet_length(state_hash) = 32),
  session_hash bytea NOT NULL CHECK (octet_length(session_hash) = 32),
  key_id text NOT NULL CHECK (length(key_id) BETWEEN 1 AND 128),
  wrapped_dek bytea NOT NULL CHECK (octet_length(wrapped_dek) = 48),
  wrap_nonce bytea NOT NULL CHECK (octet_length(wrap_nonce) = 12),
  payload_nonce bytea NOT NULL CHECK (octet_length(payload_nonce) = 12),
  ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) >= 16),
  format_version integer NOT NULL DEFAULT 1 CHECK (format_version = 1),
  wrap_revision bigint NOT NULL DEFAULT 1 CHECK (wrap_revision > 0),
  encryption_generation bigint NOT NULL DEFAULT 1 CHECK (encryption_generation > 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  state text NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'exchanging', 'finished', 'failed')),
  locale text NOT NULL CHECK (locale IN ('en', 'nl', 'pt')),
  return_path text NOT NULL,
  redirect_uri text NOT NULL CHECK (length(redirect_uri) BETWEEN 1 AND 2048),
  UNIQUE (tenant_id, state_hash),
  FOREIGN KEY (tenant_id, member_id) REFERENCES memberships(tenant_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, connection_id, member_id)
    REFERENCES google_calendar_connections(tenant_id, id, member_id) ON DELETE RESTRICT,
  CONSTRAINT ck_calendar_attempt_connection CHECK (
    (connection_id, connection_generation) IS NULL OR
    (connection_id, connection_generation) IS NOT NULL),
  CONSTRAINT ck_calendar_attempt_expiry CHECK (
    isfinite(created_at) AND isfinite(expires_at) AND expires_at > created_at
    AND expires_at <= created_at + interval '10 minutes'),
  CONSTRAINT ck_calendar_attempt_consumed CHECK (
    (state = 'pending' AND consumed_at IS NULL) OR
    (state IN ('exchanging', 'finished') AND consumed_at IS NOT NULL) OR state = 'failed'),
  CONSTRAINT ck_calendar_attempt_return CHECK (return_path = '/' || locale || '/my-hours')
)
""")
    enable_tenant_isolation("google_calendar_oauth_attempts")
    op.execute("""
CREATE INDEX ix_calendar_attempt_member ON google_calendar_oauth_attempts(tenant_id, member_id);
CREATE INDEX ix_calendar_attempt_expiry ON google_calendar_oauth_attempts(expires_at);
-- Invoker trigger: the tenant policy still applies; no cross-tenant definer is needed here.
CREATE FUNCTION calendar_row_binding() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.encryption_generation <> OLD.encryption_generation THEN
    RAISE EXCEPTION 'calendar encryption generation is immutable' USING ERRCODE = '23514';
  END IF;
  IF NOT EXISTS (SELECT FROM memberships
                 WHERE tenant_id = NEW.tenant_id AND id = NEW.member_id
                   AND user_id = NEW.user_id) THEN
    RAISE EXCEPTION 'calendar member binding is invalid' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION calendar_row_binding() FROM PUBLIC;
CREATE TRIGGER calendar_connection_binding BEFORE INSERT OR UPDATE ON google_calendar_connections
  FOR EACH ROW EXECUTE FUNCTION calendar_row_binding();
CREATE TRIGGER calendar_attempt_binding BEFORE INSERT OR UPDATE ON google_calendar_oauth_attempts
  FOR EACH ROW EXECUTE FUNCTION calendar_row_binding();
REVOKE ALL ON google_calendar_connections, google_calendar_oauth_attempts FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON google_calendar_connections, google_calendar_oauth_attempts
  TO ziftbook_app;
ALTER TABLE time_off
  ADD COLUMN calendar_connection_id uuid,
  ADD CONSTRAINT fk_time_off_calendar_connection
    FOREIGN KEY (tenant_id, calendar_connection_id, member_id)
    REFERENCES google_calendar_connections(tenant_id, id, member_id) ON DELETE RESTRICT NOT VALID,
  ADD CONSTRAINT ck_time_off_calendar_connection
    CHECK ((source = 'google') = (calendar_connection_id IS NOT NULL)) NOT VALID,
  ADD CONSTRAINT ck_time_off_google_shape CHECK (source <> 'google' OR
    ((starts_at, ends_at) IS NOT NULL AND (first_day, last_day) IS NULL
     AND isfinite(starts_at) AND isfinite(ends_at) AND reason IS NULL
     AND starts_with(external_id, calendar_connection_id::text || ':'))) NOT VALID;
CREATE INDEX ix_time_off_calendar_connection ON time_off(tenant_id, calendar_connection_id)
""")


def downgrade() -> None:
    # Refuse destructive rollback while credentials or tombstones remain, across every tenant.
    op.execute("""
LOCK TABLE google_calendar_connections, google_calendar_oauth_attempts IN ACCESS EXCLUSIVE MODE;
DO $$
DECLARE v_tenant uuid; v_previous text := current_setting('app.tenant_id', true);
BEGIN
  FOR v_tenant IN SELECT id FROM tenants LOOP
    PERFORM set_config('app.tenant_id', v_tenant::text, true);
    IF EXISTS (SELECT FROM google_calendar_connections) OR
       EXISTS (SELECT FROM google_calendar_oauth_attempts) THEN
      RAISE EXCEPTION 'calendar storage must be empty before downgrade';
    END IF;
  END LOOP;
  PERFORM set_config('app.tenant_id', coalesce(v_previous, ''), true);
END $$;
ALTER TABLE time_off DROP CONSTRAINT ck_time_off_google_shape,
  DROP CONSTRAINT ck_time_off_calendar_connection, DROP CONSTRAINT fk_time_off_calendar_connection;
DROP INDEX ix_time_off_calendar_connection;
ALTER TABLE time_off DROP COLUMN calendar_connection_id;
DROP TABLE google_calendar_oauth_attempts, google_calendar_connections;
DROP FUNCTION calendar_row_binding()
""")
