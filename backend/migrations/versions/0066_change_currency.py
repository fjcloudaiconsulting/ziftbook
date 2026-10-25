"""The owner changes the business's currency until its first booking (ZIF-102).

Owner's ruling, 2026-10-25: the currency is the merchant's choice, independent of the country and of
anybody's language, and it changes only while the business has no booking and no Stripe account.
Saved prices keep their numbers and take the new currency, once the owner has confirmed how many.

What the database enforces, and how:
- Only the four currencies a business can hold: ck_tenants_currency_supported. Adding one needs a
  migration widening it (CONTRIBUTING.md, "Money").
- No booking: the bookings foreign key to tenants (id, currency) stays NO ACTION (0026).
- No Stripe account: the payment_accounts foreign key (0062).
- Prices follow: the services foreign key becomes ON UPDATE CASCADE, so changing tenants.currency
  relabels every saved price, archived ones too, and changes no amount.
- The count the owner confirmed: change_currency's p_confirmed_prices must equal the number of
  saved prices it finds under the lock.
- Owner only: the API (app/business_currency.py). app.tenant_id is trusted from the caller, as in
  every definer here.

change_currency is SECURITY DEFINER because tenants has no row-level security: a column grant would
let the app role change any business's currency. It changes only the business in app.tenant_id.
It locks that business's row first, so a booking or a Stripe account inserted while it waits (their
foreign key checks hold the row) is seen by the checks after it; that needs a fresh snapshot per
statement, so it refuses to run outside READ COMMITTED, as keep_an_owner does (0014).

Accepted, at most once per business and only before its first booking:
- A booking or service insert racing a change can fail once on its foreign key, or deadlock against
  the cascade (booking creation reads the service FOR SHARE, then takes the business's row).
- A client who opened the booking page before the change, and books after it, is booked at the
  number they saw in the new currency: the booking snapshots the service as it is at insert, and
  the booking request carries no price or currency to compare with. Closing it needs the page to
  send the currency it showed (a booking-page change, left to the settings-screen work, PR G).

Preflight: the upgrade refuses, naming the problem, while any business holds a currency outside the
four (possible before this release, whose check was only three capital letters): the NOT VALID check
would pass, and 0067's validation would then stop the deploy with a bare constraint error.

Expand: both constraints are added NOT VALID, so neither scans under its lock; 0067 validates them.
The services key is swapped first: that statement locks services, then tenants, so any lock wait
happens while tenants is still free, and the check then reuses the tenants lock already held. A NOT
VALID key still checks new rows and still cascades. Executable by ziftbook_app (and so by
ziftbook_worker, its member).

Revision ID: 0066
Revises: 0062
"""

from alembic import op

revision = "0066"
down_revision = "0062"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
SERVICES_FK = "fk_services_tenant_id_price_currency_tenants"


def upgrade() -> None:
    op.execute("""
    DO $$
    DECLARE v_found text;
    BEGIN
      SELECT string_agg(DISTINCT currency, ', ' ORDER BY currency) INTO v_found FROM tenants
      WHERE currency NOT IN ('BRL', 'EUR', 'GBP', 'USD');
      IF v_found IS NOT NULL THEN
        RAISE EXCEPTION 'businesses hold a currency outside BRL, EUR, GBP and USD: %', v_found
          USING HINT = 'Move each to one of the four, then run the migrations again.';
      END IF;
    END $$""")
    op.execute(f"""
    ALTER TABLE services
      DROP CONSTRAINT {SERVICES_FK},
      ADD CONSTRAINT {SERVICES_FK}
        FOREIGN KEY (tenant_id, price_currency) REFERENCES tenants (id, currency)
        ON UPDATE CASCADE NOT VALID""")
    op.execute("""
    ALTER TABLE tenants ADD CONSTRAINT ck_tenants_currency_supported
      CHECK (currency IN ('BRL', 'EUR', 'GBP', 'USD')) NOT VALID""")
    op.execute(f"""
CREATE FUNCTION change_currency(p_currency text, p_confirmed_prices integer)
RETURNS TABLE (outcome text, prices integer, previous text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER {SEARCH_PATH} AS $$
-- Changes the caller's business's currency: 'changed' (with the prices relabelled), 'unchanged',
-- 'locked_bookings', 'locked_payments', or 'prices_exist' (with how many) when the count confirmed
-- is not the count saved. previous: the currency it found.
DECLARE
  v_tenant uuid := current_setting('app.tenant_id')::uuid;
  v_old text;
  v_prices integer;
BEGIN
  IF current_setting('transaction_isolation') <> 'read committed' THEN
    -- A snapshot taken before the lock wait would miss a booking committed meanwhile.
    RAISE EXCEPTION 'change_currency needs READ COMMITTED' USING ERRCODE = '0A000';
  END IF;
  -- Its own statement, first: every check below runs after the lock is granted.
  SELECT t.currency INTO STRICT v_old FROM tenants t WHERE t.id = v_tenant FOR UPDATE;
  IF v_old = p_currency THEN
    RETURN QUERY SELECT 'unchanged'::text, 0, v_old;
    RETURN;
  END IF;
  IF EXISTS (SELECT FROM bookings b WHERE b.tenant_id = v_tenant) THEN
    RETURN QUERY SELECT 'locked_bookings'::text, 0, v_old;
    RETURN;
  END IF;
  IF EXISTS (SELECT FROM payment_accounts p WHERE p.tenant_id = v_tenant) THEN
    RETURN QUERY SELECT 'locked_payments'::text, 0, v_old;
    RETURN;
  END IF;
  SELECT count(*) INTO v_prices FROM services s WHERE s.tenant_id = v_tenant;
  IF v_prices > 0 AND p_confirmed_prices IS DISTINCT FROM v_prices THEN
    RETURN QUERY SELECT 'prices_exist'::text, v_prices, v_old;
    RETURN;
  END IF;
  UPDATE tenants SET currency = p_currency WHERE id = v_tenant;  -- the services key cascades
  RETURN QUERY SELECT 'changed'::text, v_prices, v_old;
END $$""")
    op.execute("REVOKE EXECUTE ON FUNCTION change_currency(text, integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION change_currency(text, integer) TO ziftbook_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION change_currency(text, integer)")
    # Back to 0016's validated NO ACTION key. Its check scans services as the owner, which FORCE
    # row-level security would subject to the tenant policy (no app.tenant_id here: 42704), so FORCE
    # is lifted for this one statement, under the ACCESS EXCLUSIVE lock it already takes.
    op.execute("ALTER TABLE services NO FORCE ROW LEVEL SECURITY")
    op.execute(f"""
    ALTER TABLE services
      DROP CONSTRAINT {SERVICES_FK},
      ADD CONSTRAINT {SERVICES_FK}
        FOREIGN KEY (tenant_id, price_currency) REFERENCES tenants (id, currency)""")
    op.execute("ALTER TABLE services FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants DROP CONSTRAINT ck_tenants_currency_supported")
