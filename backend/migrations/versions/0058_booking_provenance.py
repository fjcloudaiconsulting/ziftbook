"""Bookings record the account that placed them; a person reads their own bookings (ZIF-146).

bookings.booked_as is the account whose live session proved the booking's email, written only by
the booking page's insert (place()), from that session, never from a request body. A guest booking
leaves it NULL, so nobody can put a booking into another person's history by typing their address.
Like clients.user_id it has no foreign key (a foreign key check bypasses row-level security, so it
would be an existence oracle over users, and a pointer left by a platform erasure is correct until
ZIF-58 clears it). It is personal data: it maps a booking to a person. No merchant route selects
it and no response schema carries it. The app role may INSERT it and never UPDATE it (0047's
column-grant rule: a column INSERT_BOOKING writes needs its own GRANT INSERT, applied before the app
that writes it). A partial index on it serves the person's history and ZIF-58's erasure; the
column is new and all NULL, so the index builds from an empty set while ADD COLUMN already holds
the table's lock. If this migration ever times out on bookings, split the index into a migration of
its own with CREATE INDEX CONCURRENTLY.

hold_account_session re-confirms, inside the booking transaction, the account session that proved
the booking, FOR SHARE: a sign-out, password reset or erasure that deletes it either waits for the
booking to commit (and then clears what it linked) or has committed already (and the booking is
refused). Lock order: account_sessions before clients.

own_bookings is the person's history: every booking their account placed, at every business. It is
keyed by an account session hash (live, started within 24 hours), never a user id; a business
session's hash finds nothing. It walks the businesses (tenants has no row-level security) and reads
each one's bookings under its own tenant isolation, so no policy widens what anyone can read. It
returns no booking id (a capability on older payment routes): the page key is sha256 of the id.
Hidden: a checkout still open, and an expired checkout that never completed.

Revision ID: 0058
Revises: 0057
"""

from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

LIVE = """s.id_hash = p_id_hash AND s.expires_at > now()
    AND s.last_seen_at > now() - interval '7 days'"""

FUNCTIONS = {
    "hold_account_session(bytea)": f"""
CREATE FUNCTION hold_account_session(p_id_hash bytea)
RETURNS uuid LANGUAGE sql SECURITY DEFINER {SEARCH_PATH} AS $$
  -- The live account session's holder, its row held FOR SHARE until the caller's transaction ends.
  SELECT s.user_id FROM account_sessions s WHERE {LIVE} FOR SHARE
$$""",
    "own_bookings(bytea,timestamptz,bytea,integer)": f"""
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
  WHERE {LIVE} AND s.started_at > now() - interval '24 hours';
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
END $$""",
}


def upgrade() -> None:
    op.execute("ALTER TABLE bookings ADD COLUMN booked_as uuid")
    op.execute("GRANT INSERT (booked_as) ON bookings TO ziftbook_app")
    op.execute(
        "CREATE INDEX ix_bookings_booked_as ON bookings (booked_as) WHERE booked_as IS NOT NULL"
    )
    for signature, definition in FUNCTIONS.items():
        op.execute(definition)
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")


def downgrade() -> None:
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("DROP INDEX ix_bookings_booked_as")
    op.execute("REVOKE INSERT (booked_as) ON bookings FROM ziftbook_app")
    op.execute("ALTER TABLE bookings DROP COLUMN booked_as")
