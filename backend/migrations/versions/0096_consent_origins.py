"""Consent addresses: none on a merchant-entered consent, and a retention period (ZIF-156).

A consent a member records for a client (source = 'merchant') stored the member's address and
browser, the receptionist's personal data, not the client's. The app now passes neither. Here:

- ck_consents_merchant_no_origin: a merchant row holds no ip and no user_agent. Added NOT VALID,
  so it scans nothing under its lock; 0097 clears the existing rows, and the next release's
  contract migration validates it.
- consents_merchant_no_origin, a BEFORE INSERT trigger, sets ip and user_agent to NULL on a
  merchant row, for the deploy window only: the release still running passes the member's address,
  and without the trigger the check would refuse its consent writes (a 500). It also means the check
  can only refuse an UPDATE until the trigger goes, so tests/test_clients_api.py fences the app's
  own write path. The next release's contract migration drops it.

sweep_consent_origins(p_from), SECURITY DEFINER, owned by ziftbook_migrate, executable by
ziftbook_worker only, as 0074's functions: sets ip and user_agent of a consent to NULL a year after
the client's answer for that purpose changed (granted to withdrawn, or back) after it. A later row
with the same answer, from the merchant or the booking page, changes nothing: the rows of the
answer that stands keep their proof however old they are (owner ruling D4, and the privacy notice:
"removed 1 year after you change your answer"). The row itself, its wording, version, purpose,
source and time, always stays: the Art. 7(1) evidence. Later is by id, as
app.clients.CURRENT_CONSENTS reads it. The walk is 0074's sweep_booking_origins's: business by
business under tenant_isolation, FOR UPDATE SKIP LOCKED, at most 1000 rows and 200 businesses per
call, next_tenant where the next call resumes (NULL once past the last business); its resume and
cap tests run against both functions.

Lock: ADD CONSTRAINT NOT VALID takes ACCESS EXCLUSIVE on consents and CREATE TRIGGER SHARE ROW
EXCLUSIVE, each for a catalog change only, until this migration commits; nothing here scans or
updates a row.

The downgrade drops the function, the trigger and the check.

Revision ID: 0096
Revises: 0095
"""

from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
SIGNATURE = "sweep_consent_origins(uuid)"

# shortcut: what each hourly call reads only grows: every withdrawal older than a year (0098's
# index, index-only), plus every row of those clients and purposes, the standing answer's
# included, which keeps its address by design and so is read again on every run. Fine at
# pre-launch scale; at about 100,000 withdrawals older than a year across businesses, keep a
# per-business high-water mark (the created_at of the newest answer change already swept) and read
# only the changes past it and the year, so a run costs the rows that just became due.
SWEEP = f"""
CREATE FUNCTION sweep_consent_origins(p_from uuid, OUT cleared integer, OUT next_tenant uuid)
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
    UPDATE consents c SET ip = NULL, user_agent = NULL
    WHERE c.tenant_id = v_t AND c.id = ANY(ARRAY(
      -- Driven by the answer changes (0098's index): a change a year old has a withdrawal a year
      -- old on one side or the other, so only the client-purposes that hold one are read. In a
      -- MATERIALIZED CTE: FOR UPDATE OF x row-marks every other relation of its own query, which
      -- rules out the index-only scan.
      WITH changed AS MATERIALIZED (
        SELECT DISTINCT w.client_id, w.purpose FROM consents w
        WHERE w.tenant_id = v_t AND NOT w.granted AND w.created_at < now() - interval '1 year')
      SELECT x.id FROM consents x
      WHERE x.tenant_id = v_t AND (x.ip IS NOT NULL OR x.user_agent IS NOT NULL)
        AND (x.client_id, x.purpose) IN (SELECT ch.client_id, ch.purpose FROM changed ch)
        AND EXISTS (
          SELECT 1 FROM consents y
          WHERE y.tenant_id = v_t AND y.client_id = x.client_id AND y.purpose = x.purpose
            AND y.id > x.id AND y.granted <> x.granted
            AND y.created_at < now() - interval '1 year')
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
END $$"""


def upgrade() -> None:
    # The strongest lock first, so the trigger's never has to be upgraded to it.
    op.execute("""
    ALTER TABLE consents ADD CONSTRAINT ck_consents_merchant_no_origin
      CHECK (source <> 'merchant' OR (ip IS NULL AND user_agent IS NULL)) NOT VALID""")
    op.execute(f"""
    CREATE FUNCTION consents_merchant_no_origin() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER {SEARCH_PATH} AS $$
      BEGIN
        NEW.ip := NULL;
        NEW.user_agent := NULL;
        RETURN NEW;
      END $$""")
    op.execute("""
    CREATE TRIGGER consents_merchant_no_origin BEFORE INSERT ON consents
      FOR EACH ROW WHEN (NEW.source = 'merchant')
      EXECUTE FUNCTION consents_merchant_no_origin()""")
    # Firing a trigger checks no EXECUTE privilege (0030), so no role needs it.
    op.execute("REVOKE EXECUTE ON FUNCTION consents_merchant_no_origin() FROM PUBLIC")
    op.execute(SWEEP)
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO ziftbook_worker")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")
    op.execute("DROP TRIGGER consents_merchant_no_origin ON consents")
    op.execute("DROP FUNCTION consents_merchant_no_origin()")
    op.execute("ALTER TABLE consents DROP CONSTRAINT ck_consents_merchant_no_origin")
