"""A person's history in two sections: what is still to come, soonest first, and the rest (ZIF-146).

own_bookings_in is own_bookings (0075) for one section at a time, so "Your bookings" shows the next
booking first from its first page, however many lie ahead (PR 6 fold). The API pages
newest first otherwise, and with more than a page of bookings ahead the next one sat on a later
page. Upcoming: still standing (pending, awaiting payment, confirmed) and not ended yet, one in
progress included, soonest first, after the cursor. Past: everything else, newest first, before
the cursor. The server decides the sections; the page only shows them.

Same key, same walk, same columns and hiding rules as 0075's own_bookings, which stays as it is for
the release still running (its history route calls it); this is a second function, so nothing the
running release reads changes. USABLE is 0057's, verbatim (tests/test_migrations.py). The downgrade
drops it, leaving 0075 as it was.

Revision ID: 0076
Revises: 0075
"""

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
SIGNATURE = "own_bookings_in(bytea,boolean,timestamptz,bytea,integer)"

# 0057's USABLE, verbatim (tests/test_migrations.py).
USABLE = """s.id_hash = p_id_hash AND s.expires_at > now()
        AND s.last_seen_at > now() - interval '7 days'
        AND NOT EXISTS (SELECT FROM platform_admins a WHERE a.user_id = s.user_id)
        AND (s.started_by IS NULL OR EXISTS (
          SELECT FROM sessions b WHERE b.id_hash = s.started_by AND b.expires_at > now()
            AND b.last_seen_at > now() - interval '7 days'))"""

# One business's bookings this account placed, as 0075's own_bookings selects and hides them.
ROWS = """
      SELECT n.name, n.slug, b.starts_at, b.ends_at, b.status, b.service_name,
             b.price_amount_minor, b.price_currency, b.worker_display_name,
             sha256(convert_to(b.id::text, 'UTF8')),
             (SELECT z.value #>> '{}' FROM settings z
              WHERE z.tenant_id = n.id AND z.key = 'timezone')
      FROM bookings b JOIN tenants n ON n.id = b.tenant_id
      WHERE b.booked_as = v_user
        AND b.status <> 'awaiting_payment'
        AND NOT (b.status = 'expired' AND b.checkout_hash IS NOT NULL
                 AND NOT EXISTS (SELECT FROM booking_payments p
                                 WHERE p.booking_id = b.id AND p.status IN ('checked', 'paid')))"""

# Still to come: still standing, and not ended (a booking in progress is still to come).
UPCOMING = "b.status IN ('pending', 'awaiting_payment', 'confirmed') AND b.ends_at > now()"

FUNCTION = f"""
CREATE FUNCTION own_bookings_in(
  p_id_hash bytea, p_upcoming boolean, p_after_starts_at timestamptz, p_after_key bytea,
  p_limit integer)
RETURNS TABLE (business_name text, business_slug text, starts_at timestamptz,
               ends_at timestamptz, status text, service_name jsonb, price_amount_minor integer,
               price_currency text, worker_display_name text, sort_key bytea,
               business_timezone text)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Up to p_limit of each business's bookings this account placed in one section: upcoming
-- (p_upcoming), soonest first and after the cursor; or past, newest first and before it. The
-- caller merges the businesses. Every column is table-aliased (the OUT parameters, 42702).
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
    IF p_upcoming THEN
      RETURN QUERY{ROWS}
        AND {UPCOMING}
        AND (p_after_starts_at IS NULL
             OR (b.starts_at, sha256(convert_to(b.id::text, 'UTF8')))
                > (p_after_starts_at, p_after_key))
      ORDER BY b.starts_at, 10
      LIMIT p_limit;
    ELSE
      RETURN QUERY{ROWS}
        AND NOT ({UPCOMING})
        AND (p_after_starts_at IS NULL
             OR (b.starts_at, sha256(convert_to(b.id::text, 'UTF8')))
                < (p_after_starts_at, p_after_key))
      ORDER BY b.starts_at DESC, 10 DESC
      LIMIT p_limit;
    END IF;
  END LOOP;
  PERFORM set_config('app.tenant_id', coalesce(v_tenant, ''), true);
END $$"""


def upgrade() -> None:
    op.execute(FUNCTION)
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO ziftbook_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")
