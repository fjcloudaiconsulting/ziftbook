"""The erase core blanks a client of this business only (ZIF-58).

erase_client_core(p_client, p_announce) erases one client of the current business (app.tenant_id),
in one place: it marks the clients row erased and blanks every personal field of it (0063's CHECK),
the client's personal data on its bookings (the name, phone and message the booker typed, the
merchant's decline message, the checkout cookie's hash), the client's address and browser on the
booking events the client made (the team's own events keep theirs), and deletes the client's
consent rows and its bookings' manage links. Booking evidence (dates, prices, services, status,
payments) is kept. It changes no booking's status: the caller ends live bookings first, in the same
transaction. It returns when the client was erased, the first erasure's time on a repeat (which
writes nothing), or NULL for a client this business does not have.

Every statement names the business explicitly, besides row-level security, so the scope holds even
if a policy for ziftbook_migrate (the function's owner) is ever widened. A SECURITY DEFINER rather
than column grants: booking_events and consents stay append-only for the app role, and the booking
snapshot stays write-once (0047).

client_erasure_holds keeps an erased client's email and locale, its purge date at most 31 days
after it is written: the only way to tell the client that a booking was cancelled or a refund
failed. The core writes it when the client has an address and either p_announce is true and this
erasure ended one of its bookings (the cancellation mails), or money is still owed an answer on
one of its bookings, whatever p_announce says. An owner's erasure and an account deletion pass
true; re-applying an erasure after a backup restore passes false, because the cancellations were
announced the first time, while a refund that fails later must still reach the client. Only the
core writes a hold; the app role can neither read nor write it; the worker reads and deletes it.

erase_client(p_session_hash, p_client) is the app role's way in: it refuses unless the hash is a
live owner session of the current business (not past its absolute expiry, seen within the idle
limit, auth.IDLE), so a route that forgets its owner check cannot erase. The core is executable by
no role but its owner.

Revision ID: 0064
Revises: 0063
"""

from alembic import op

from app.db import enable_tenant_isolation

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

# The booking endings that send a paid booking's money back: app.bookings.REFUNDING, written out
# because a migration must not change when the code does. With payments.UNSETTLED's predicate
# below, "money still owed an answer".
REFUNDING = "'declined', 'expired', 'cancelled_by_merchant', 'cancelled_by_client'"
# The reasons an erasure records on the bookings it ends (ZIF-58): an owner's erasure, an account
# deletion, and a re-applied erasure. Whether the client hears of them is p_announce's.
ERASURE_REASONS = "'client_erased', 'account_erased', 'erasure_reapplied'"

ERASE_CLIENT_CORE = f"""
CREATE FUNCTION erase_client_core(p_client uuid, p_announce boolean) RETURNS timestamptz
  LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
DECLARE
  v_tenant uuid;
  v_at timestamptz;
  v_email text;
  v_locale text;
BEGIN
  IF coalesce(current_setting('app.tenant_id', true), '') = '' THEN
    RAISE EXCEPTION 'erase_client needs a business' USING ERRCODE = 'insufficient_privilege';
  END IF;
  v_tenant := current_setting('app.tenant_id')::uuid;
  SELECT email, locale INTO v_email, v_locale FROM clients
   WHERE tenant_id = v_tenant AND id = p_client AND erased_at IS NULL
   FOR NO KEY UPDATE;
  UPDATE clients SET erased_at = now(), user_id = NULL, name = '', email = NULL, phone = NULL,
                     locale = NULL, client_note = NULL, internal_note = NULL
   WHERE tenant_id = v_tenant AND id = p_client AND erased_at IS NULL
   RETURNING erased_at INTO v_at;
  IF v_at IS NULL THEN
    SELECT erased_at INTO v_at FROM clients WHERE tenant_id = v_tenant AND id = p_client;
    IF v_at IS NULL THEN
      RETURN NULL;  -- not a client of this business
    END IF;
  END IF;
  -- The contact hold: when this erasure ended a booking the client is to be told about (p_announce,
  -- and its event, with an erasure's reason, written in this transaction), or when money is still
  -- owed an answer on one of the client's bookings, announced or not. On a repeat v_email is NULL:
  -- nothing is written.
  INSERT INTO client_erasure_holds (tenant_id, client_id, email, locale, purge_after)
  SELECT v_tenant, p_client, v_email, v_locale, now() + interval '30 days'
   WHERE v_email IS NOT NULL
     AND ((p_announce
          AND EXISTS (SELECT 1 FROM booking_events e
                  JOIN bookings b ON b.tenant_id = e.tenant_id AND b.id = e.booking_id
                  WHERE e.tenant_id = v_tenant AND b.tenant_id = v_tenant
                    AND b.client_id = p_client AND e.created_at >= now()
                    AND e.details ->> 'reason' IN ({ERASURE_REASONS})))
          OR EXISTS (SELECT 1 FROM booking_payments p
                     JOIN bookings b ON b.tenant_id = p.tenant_id AND b.id = p.booking_id
                     WHERE p.tenant_id = v_tenant AND b.tenant_id = v_tenant
                       AND b.client_id = p_client AND p.settled_at IS NULL
                       AND ((p.status = 'open'
                             AND NOT (b.status = 'awaiting_payment' AND b.expires_at > now()))
                            OR p.status = 'checked'
                            OR (p.status = 'paid' AND b.status IN ({REFUNDING})))))
  ON CONFLICT DO NOTHING;
  -- ZIF-146's bookings.booked_as joins this list: whichever of its migration and this one merges
  -- second re-creates the function with it (tests/test_client_erasure_db.py fails until then).
  UPDATE bookings SET client_name = NULL, client_phone = NULL, client_message = NULL,
                      decline_message = NULL, checkout_hash = NULL
   WHERE tenant_id = v_tenant AND client_id = p_client
     AND num_nonnulls(client_name, client_phone, client_message, decline_message,
                      checkout_hash) > 0;
  -- The client's own events: an event with no actor is the client's (or the clock's, which
  -- records no address). The team's keep the member's address and browser.
  UPDATE booking_events e SET ip = NULL, user_agent = NULL FROM bookings b
   WHERE e.tenant_id = v_tenant AND b.tenant_id = v_tenant AND b.id = e.booking_id
     AND b.client_id = p_client AND e.actor_user_id IS NULL
     AND (e.ip IS NOT NULL OR e.user_agent IS NOT NULL);
  DELETE FROM consents WHERE tenant_id = v_tenant AND client_id = p_client;
  DELETE FROM booking_links l USING bookings b
   WHERE l.tenant_id = v_tenant AND b.tenant_id = v_tenant AND b.id = l.booking_id
     AND b.client_id = p_client;
  RETURN v_at;
END $$"""

# The idle limit is app.auth.IDLE (tests/test_client_erasure_db.py pins the two together).
ERASE_CLIENT = f"""
CREATE FUNCTION erase_client(p_session_hash bytea, p_client uuid) RETURNS timestamptz
  LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
BEGIN
  IF coalesce(current_setting('app.tenant_id', true), '') = '' THEN
    RAISE EXCEPTION 'erase_client needs a business' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM sessions
                 WHERE id_hash = p_session_hash
                   AND tenant_id = current_setting('app.tenant_id')::uuid
                   AND role = 'owner' AND expires_at > now()
                   AND last_seen_at > now() - interval '7 days') THEN
    RAISE EXCEPTION 'erase_client needs a live owner session of this business'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN erase_client_core(p_client, true);  -- an owner's erasure: the client is told
END $$"""


def upgrade() -> None:
    op.execute("""
    CREATE TABLE client_erasure_holds (
      tenant_id uuid NOT NULL
        CONSTRAINT fk_client_erasure_holds_tenant_id_tenants REFERENCES tenants (id),
      client_id uuid NOT NULL,
      email text NOT NULL,
      locale text,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      purge_after timestamptz NOT NULL,
      CONSTRAINT pk_client_erasure_holds PRIMARY KEY (tenant_id, client_id),
      CONSTRAINT fk_client_erasure_holds_tenant_id_client_id_clients
        FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id),
      -- Inside the one-month answer window of an erasure request, whatever writes it.
      CONSTRAINT ck_client_erasure_holds_purge_after
        CHECK (purge_after <= created_at + interval '31 days')
    )""")
    enable_tenant_isolation("client_erasure_holds", referenced=False)
    # The REVOKE runs before the GRANT. The API never reads or writes a hold; the worker reads it
    # to send and deletes it to purge (its role is a member of ziftbook_app, so the REVOKE takes
    # what it inherited too); only the core inserts.
    op.execute("REVOKE ALL ON client_erasure_holds FROM ziftbook_app")
    op.execute("GRANT SELECT, DELETE ON client_erasure_holds TO ziftbook_worker")
    op.execute(ERASE_CLIENT_CORE)
    op.execute(ERASE_CLIENT)
    # 0048's default privileges give EXECUTE to no other role; revoked from PUBLIC all the same,
    # as every definer is. Only erase_client is the app role's.
    op.execute("REVOKE EXECUTE ON FUNCTION erase_client_core(uuid, boolean) FROM PUBLIC")
    op.execute("REVOKE EXECUTE ON FUNCTION erase_client(bytea, uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION erase_client(bytea, uuid) TO ziftbook_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION erase_client(bytea, uuid)")
    op.execute("DROP FUNCTION erase_client_core(uuid, boolean)")
    op.execute("DROP TABLE client_erasure_holds")
