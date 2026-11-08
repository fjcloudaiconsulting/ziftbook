"""Consent addresses: none on a merchant-entered consent, and a retention period (ZIF-156).

A consent a member records for a client (source = 'merchant') stored the member's address and
browser, the receptionist's personal data, not the client's. From here:

- consents_merchant_no_origin, a BEFORE INSERT trigger, sets ip and user_agent to NULL on a
  merchant row. The new app passes neither; the trigger is what keeps the release still running
  during the deploy (which passes both) recording merchant consents rather than failing them.
- ck_consents_merchant_no_origin states the rule for every row, existing and future, whatever
  writes it (an UPDATE, a data-only restore with triggers disabled).
- Existing merchant rows lose their ip and user_agent here, before the check validates.

sweep_consent_origins(p_from), SECURITY DEFINER, owned by ziftbook_migrate, executable by
ziftbook_worker only, as 0074's functions: sets ip and user_agent of a consent to NULL a year after
a newer row for the same client and purpose replaced it (the period of 0074's audit events, the
nearest analogue; ZIF-285's legal review may change it). The consent that stands, the newest per
client and purpose, keeps its proof however old it is (owner ruling D4, and the privacy notice:
"kept for as long as your choice stands"). The row itself, its wording, version, purpose, source and
time, always stays: the Art. 7(1) evidence. Newest is by id, as app.clients.CURRENT_CONSENTS reads
it. The walk is 0074's sweep_booking_origins's: business by business under tenant_isolation, FOR
UPDATE SKIP LOCKED, at most 1000 rows and 200 businesses per call, next_tenant where the next call
resumes (NULL once past the last business).

Lock: the update and the check's validation run with FORCE lifted (app.db.lift_force), so NO FORCE
holds ACCESS EXCLUSIVE on consents until commit: one small table, for one scan of it and the
update of its merchant rows.

The downgrade drops the function, the check and the trigger. Cleared addresses do not come back.

Revision ID: 0096
Revises: 0095
"""

from alembic import op

from app.db import lift_force

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
SIGNATURE = "sweep_consent_origins(uuid)"

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
      SELECT x.id FROM consents x
      WHERE x.tenant_id = v_t AND (x.ip IS NOT NULL OR x.user_agent IS NOT NULL)
        AND EXISTS (
          SELECT 1 FROM consents y
          WHERE y.tenant_id = x.tenant_id AND y.client_id = x.client_id
            AND y.purpose = x.purpose AND y.id > x.id
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
    lift_force(
        op.execute,
        "consents",
        "UPDATE consents SET ip = NULL, user_agent = NULL"
        " WHERE source = 'merchant' AND (ip IS NOT NULL OR user_agent IS NOT NULL)",
    )
    lift_force(
        op.execute,
        "consents",
        "ALTER TABLE consents ADD CONSTRAINT ck_consents_merchant_no_origin"
        " CHECK (source <> 'merchant' OR (ip IS NULL AND user_agent IS NULL))",
    )
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
