"""Admins suspend and reactivate a business (ZIF-73): the only writers of tenants.suspended_at.

Two SECURITY DEFINER functions, no table change and no column grant: tenants has no row-level
security, so a grant would let any app-role path flip any business (as 0066 refuses for currency).
Each takes the business's booking advisory lock (bookings.LOCK_KEY), so a booking in flight and a
suspension serialise, and each re-checks the admin's live session and a second factor passed in
the last 15 minutes, by the database's clock, so a caller that skipped the route guard still gets
'stale'. Both act on the business the caller's transaction is in (app.tenant_id).

The downgrade drops both functions.

Revision ID: 0100
Revises: 0099
"""

from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"


def function(name: str, params: str, body: str) -> str:
    return f"""
CREATE FUNCTION {name}({params})
RETURNS TABLE (outcome text, suspended_at timestamptz)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {SEARCH_PATH} AS $$
-- Every column is table-aliased: the OUT parameters would make bare names ambiguous (42702).
DECLARE
  v_tenant uuid;
  v_at timestamptz;
BEGIN
  PERFORM pg_advisory_xact_lock(51, hashtext(current_setting('app.tenant_id')));
  -- clock_timestamp(), not now(): now() was fixed before the lock wait, so a factor could lapse
  -- while this waited and still pass.
  IF live_admin_user(p_id_hash) IS NULL OR NOT EXISTS (
    SELECT FROM admin_sessions s
    WHERE s.id_hash = p_id_hash AND s.second_factor_at > clock_timestamp() - interval '15 minutes'
  ) THEN
    RETURN QUERY SELECT 'stale'::text, NULL::timestamptz;
    RETURN;
  END IF;
  v_tenant := current_setting('app.tenant_id')::uuid;
  -- No FOR UPDATE (it would block the key-share locks of booking inserts): the advisory lock
  -- already serialises every writer.
  SELECT t.suspended_at INTO v_at FROM tenants t WHERE t.id = v_tenant;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'not_found'::text, NULL::timestamptz;
    RETURN;
  END IF;
{body}
END $$"""


SUSPEND = function(
    "suspend_business",
    "p_id_hash bytea, p_reason text",
    """  IF v_at IS NOT NULL THEN
    RETURN QUERY SELECT 'already_suspended'::text, v_at;
    RETURN;
  END IF;
  UPDATE tenants t SET suspended_at = now(), suspension_reason = p_reason WHERE t.id = v_tenant;
  RETURN QUERY SELECT 'suspended'::text, now();""",
)
REACTIVATE = function(
    "reactivate_business",
    "p_id_hash bytea",
    """  IF v_at IS NULL THEN
    RETURN QUERY SELECT 'not_suspended'::text, NULL::timestamptz;
    RETURN;
  END IF;
  UPDATE tenants t SET suspended_at = NULL, suspension_reason = NULL WHERE t.id = v_tenant;
  RETURN QUERY SELECT 'reactivated'::text, v_at;""",
)
SIGNATURES = ("suspend_business(bytea, text)", "reactivate_business(bytea)")


def upgrade() -> None:
    for definition, signature in zip((SUSPEND, REACTIVATE), SIGNATURES, strict=True):
        op.execute(definition)
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")


def downgrade() -> None:
    for signature in SIGNATURES:
        op.execute(f"DROP FUNCTION {signature}")
