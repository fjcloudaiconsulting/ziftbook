"""Contract step for ZIF-156: validate the merchant-consent check and drop 0096's trigger (ZIF-290).

0096 added ck_consents_merchant_no_origin NOT VALID and a BEFORE INSERT trigger that cleared ip and
user_agent on a merchant row, for the deploy window in which the release before #421 still passed
the member's address. Every environment now runs a release whose record_consents drops them itself,
so:

- VALIDATE the check. 0097 cleared the existing rows, and since 0096 the trigger cleared every new
  one and the check refused every update, so no row can fail it.
- Drop the trigger and its function: a writer that passes a member's address now fails loudly (the
  check, a 500) instead of being silently fixed.

Lock: VALIDATE first, under SHARE UPDATE EXCLUSIVE, so its scan blocks no read or write. DROP
TRIGGER takes ACCESS EXCLUSIVE (checked on Postgres 18), last, for a catalog change only, held until
this migration commits a moment later. Going from the weaker lock to the stronger one can deadlock
only with a session that holds a lock on consents and then asks for one SHARE UPDATE EXCLUSIVE
conflicts with (a VACUUM, ANALYZE or DDL), and env.py retries a deadlock once (ZIF-287).

The downgrade puts the trigger and its function back, as 0096 made them. The check stays validated:
that is stronger than 0096's NOT VALID, every row meets it, and 0096's downgrade drops it.

Revision ID: 0101
Revises: 0100
"""

from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"


def upgrade() -> None:
    op.execute("ALTER TABLE consents VALIDATE CONSTRAINT ck_consents_merchant_no_origin")
    op.execute("DROP TRIGGER consents_merchant_no_origin ON consents")
    op.execute("DROP FUNCTION consents_merchant_no_origin()")


def downgrade() -> None:
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
    op.execute("REVOKE EXECUTE ON FUNCTION consents_merchant_no_origin() FROM PUBLIC")
