"""bookings: the cancellation thresholds, snapshotted beside the text that already is (ZIF-55).

Revision ID: 0027
Revises: 0026

This migration REFUSES on a non-empty bookings table, with
  23502 column "free_cancellation_hours" of relation "bookings" contains null values
and that refusal is the feature: ZIF-55 is cannot-backfill, and there is no honest value for a
booking sold before the policy existed. On a developer database (a serial pytest run points at the
shared `ziftbook` -- tests/conftest.py:105-108), the fix is to throw the developer data away:

    psql "$ZIF_MIGRATE_DATABASE_URL" -c 'TRUNCATE booking_events, bookings;'

as ziftbook_migrate; DELETE is revoked from the app role (0026:203). CI is unaffected: every test
database is dropped, recreated and migrated per run (tests/conftest.py:118-127).
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOT NULL with no default and no backfill. Nothing guards this in SQL because nothing CAN:
    # bookings has FORCE ROW LEVEL SECURITY (app/db.py:73-79) and a migration sets no
    # app.tenant_id, so SELECT count(*), EXISTS(SELECT 1 ...) and UPDATE against this table all
    # fail 42704 `unrecognized configuration parameter "app.tenant_id"` -- even when it is empty --
    # and `SET LOCAL row_security = off` is refused for the owner under FORCE. ADD COLUMN's own
    # validation scan is DDL and bypasses the policy, so THIS statement is the guard.
    op.execute("""
    ALTER TABLE bookings
      ADD COLUMN free_cancellation_hours integer NOT NULL
        CONSTRAINT ck_bookings_free_cancellation_hours
          CHECK (free_cancellation_hours BETWEEN 0 AND 720),
      ADD COLUMN reschedule_cutoff_hours integer NOT NULL
        CONSTRAINT ck_bookings_reschedule_cutoff_hours
          CHECK (reschedule_cutoff_hours BETWEEN 0 AND 720)
    """)
    # No GRANT: bookings' privileges are table-wide (0001's default privileges), so ziftbook_app
    # holds INSERT/SELECT/UPDATE on both new columns the moment they exist -- checked, because
    # booking_events' column-list INSERT grant (0026:44-49) is the documented trap and this is not
    # that table.


def downgrade() -> None:
    # DROP COLUMN takes the column's CHECK with it, so no DROP CONSTRAINT. No REVOKE/GRANT pair
    # anywhere in 0027: re-issuing 0026's REVOKE-then-GRANT sequence is the one way to break
    # existing privileges (0026:249-250, CONTRIBUTING.md "the REVOKE runs before the GRANTs").
    op.execute("""
    ALTER TABLE bookings
      DROP COLUMN free_cancellation_hours, DROP COLUMN reschedule_cutoff_hours
    """)
