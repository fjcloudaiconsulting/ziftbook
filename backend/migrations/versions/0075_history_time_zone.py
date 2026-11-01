"""A person's history names each business's time zone (ZIF-146, PR 6 review).

own_bookings (0058) gains one column, business_timezone, after all of 0058's: the business's saved
time zone setting, or NULL when it saved none (the app then uses the settings default, as the
booking page does). "Your bookings" shows each booking at its business's local time, as its booking
page, its emails and its manage page do. The zone is the business's own, shown publicly on its
booking page: nothing about the person is added.

Expand-safe for the release still running: its history route reads SELECT * by column name, and
every 0058 column keeps its name, type and place. A new return type needs DROP and CREATE (CREATE OR
REPLACE cannot change one), so both run in this migration's one transaction, and the grant is given
again. Calling a function takes no lock: a call made meanwhile does not wait, it runs the old
committed function until this transaction commits, and the new one after. No caller ever finds
the function missing.

OLD is 0058's own_bookings verbatim (tests/test_migrations.py), so the downgrade restores it byte
for byte. USABLE is 0057's, verbatim, as in 0058.

Revision ID: 0075
Revises: 0059
"""

from alembic import op

revision = "0075"
down_revision = "0059"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
SIGNATURE = "own_bookings(bytea,timestamptz,bytea,integer)"

# 0057's USABLE, verbatim (tests/test_migrations.py).
USABLE = """s.id_hash = p_id_hash AND s.expires_at > now()
        AND s.last_seen_at > now() - interval '7 days'
        AND NOT EXISTS (SELECT FROM platform_admins a WHERE a.user_id = s.user_id)
        AND (s.started_by IS NULL OR EXISTS (
          SELECT FROM sessions b WHERE b.id_hash = s.started_by AND b.expires_at > now()
            AND b.last_seen_at > now() - interval '7 days'))"""

# 0058's own_bookings, verbatim.
OLD = f"""
CREATE FUNCTION own_bookings(
  p_id_hash bytea, p_before_starts_at timestamptz, p_before_key bytea, p_limit integer)
RETURNS TABLE (business_name text, business_slug text, starts_at timestamptz,
               ends_at timestamptz, status text, service_name jsonb, price_amount_minor integer,
               price_currency text, worker_display_name text, sort_key bytea)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Up to p_limit of each business's bookings this account placed, newest first, older than the
-- cursor (p_before_starts_at, p_before_key) when one is given. The caller merges the businesses.
-- Every column is table-aliased: the OUT parameters would make bare names ambiguous (42702).
DECLARE
  v_user uuid;
  v_tenants uuid[];
  t uuid;
  v_tenant text := current_setting('app.tenant_id', true);
BEGIN
  SELECT s.user_id INTO v_user FROM account_sessions s
  WHERE {USABLE} AND s.started_at > now() - interval '24 hours';
  IF NOT FOUND THEN RETURN; END IF;
  SELECT array_agg(n.id ORDER BY n.id) INTO v_tenants FROM tenants n;
  FOREACH t IN ARRAY coalesce(v_tenants, '{{}}') LOOP
    PERFORM set_config('app.tenant_id', t::text, true);
    RETURN QUERY
      SELECT n.name, n.slug, b.starts_at, b.ends_at, b.status, b.service_name,
             b.price_amount_minor, b.price_currency, b.worker_display_name,
             sha256(convert_to(b.id::text, 'UTF8'))
      FROM bookings b JOIN tenants n ON n.id = b.tenant_id
      WHERE b.booked_as = v_user
        AND b.status <> 'awaiting_payment'
        AND NOT (b.status = 'expired' AND b.checkout_hash IS NOT NULL
                 AND NOT EXISTS (SELECT FROM booking_payments p
                                 WHERE p.booking_id = b.id AND p.status IN ('checked', 'paid')))
        AND (p_before_starts_at IS NULL
             OR (b.starts_at, sha256(convert_to(b.id::text, 'UTF8')))
                < (p_before_starts_at, p_before_key))
      ORDER BY b.starts_at DESC, 10 DESC
      LIMIT p_limit;
  END LOOP;
  PERFORM set_config('app.tenant_id', coalesce(v_tenant, ''), true);
END $$"""


def _with_time_zone(body: str) -> str:
    """OLD plus business_timezone: one more output column, read from the business's own settings
    row under its own tenant isolation (app.tenant_id is that business's while it is read)."""
    changes = [
        (
            "worker_display_name text, sort_key bytea)",
            "worker_display_name text, sort_key bytea,\n               business_timezone text)",
        ),
        (
            "             sha256(convert_to(b.id::text, 'UTF8'))\n",
            "             sha256(convert_to(b.id::text, 'UTF8')),\n"
            "             (SELECT z.value #>> '{}' FROM settings z\n"
            "              WHERE z.tenant_id = n.id AND z.key = 'timezone')\n",
        ),
    ]
    for old, new in changes:
        assert body.count(old) == 1, old
        body = body.replace(old, new)
    return body


NEW = _with_time_zone(OLD)


def _replace(definition: str) -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")
    op.execute(definition)
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO ziftbook_app")


def upgrade() -> None:
    _replace(NEW)


def downgrade() -> None:
    _replace(OLD)
