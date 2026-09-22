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

ROLLING THIS BACK IS A ONE-WAY DOOR ON REAL DATA. `downgrade()` DROPs both columns, so it destroys
the snapshot of every booking already sold -- silently, and with no way to reconstruct it -- and
the upgrade above then refuses forever, because the rows it comes back to are the rows it cannot
backfill. The only forward path after that is `TRUNCATE booking_events, bookings`, i.e. throwing
away every booking in the database. See downgrade() for why this is documented rather than
guarded. Roll back only on a database whose bookings you are willing to lose.
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

# Every bookings column the app role may UPDATE, which is every column it could UPDATE before this
# migration MINUS the two snapshots. Spelled out because a column-list GRANT is the only form that
# works: a bare `REVOKE UPDATE (col)` against a TABLE-level grant is accepted and changes nothing
# (measured -- has_column_privilege stays true), so the table-level privilege has to go and come
# back column by column. That makes bookings a column-grant table like booking_events
# (0026:44-49): A COLUMN ADDED BY A LATER MIGRATION GETS NO UPDATE PRIVILEGE UNLESS THAT MIGRATION
# GRANTS IT. tests/test_migrations.py's T7 asserts this list against information_schema, so a
# future column with no grant turns that test red rather than a route 500.
UPDATABLE = """
id, tenant_id, client_id, worker_id, service_id, starts_at, ends_at, status, expires_at, source,
service_name, price_amount_minor, price_currency, duration_minutes, cancellation_policy_text,
auto_confirm_at_booking, worker_display_name, created_at
"""


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
    # The snapshot is evidence in a money dispute, so its immutability is a DATABASE fact and not
    # a property of "no route writes it", exactly as booking_events' append-only shape is. The app
    # role keeps INSERT (the snapshot is written once, by create()) and SELECT, and loses UPDATE
    # on these two columns alone. Nothing needs it: the only two UPDATEs the app issues against
    # bookings are EXPIRE and TRANSITION, both `SET status`.
    #
    # The REVOKE runs before the GRANT, as CONTRIBUTING.md requires, and both run AFTER the ADD
    # COLUMN so the new columns exist to be left out. SELECT and INSERT are untouched: revoking
    # one privilege leaves the others alone.
    op.execute("REVOKE UPDATE ON bookings FROM ziftbook_app")
    op.execute(f"GRANT UPDATE ({UPDATABLE}) ON bookings TO ziftbook_app")


def downgrade() -> None:
    # DESTRUCTIVE, AND IT BRICKS THE WAY BACK UP. On a non-empty bookings table this SUCCEEDS and
    # throws both snapshots away for every booking sold; `alembic upgrade head` afterwards is
    # refused for good with 23502, because upgrade() has no backfill to offer. The remedy at that
    # point is not a smaller one: `TRUNCATE booking_events, bookings` as ziftbook_migrate, i.e.
    # every booking in the database. Run this only where losing them is acceptable.
    #
    # NOT guarded in code, and the guard cannot be written: bookings has FORCE ROW LEVEL SECURITY
    # and a migration sets no app.tenant_id, so `SELECT count(*)` and `EXISTS (SELECT 1 ...)`
    # against this table both fail 42704 here -- see upgrade() -- even on an empty table, and
    # `SET LOCAL row_security = off` is refused for the owner under FORCE. A destructive downgrade
    # is house convention besides: 0026's does `DROP TABLE bookings`.
    #
    # DROP COLUMN takes each column's CHECK with it, so no DROP CONSTRAINT.
    op.execute("""
    ALTER TABLE bookings
      DROP COLUMN free_cancellation_hours, DROP COLUMN reschedule_cutoff_hours
    """)
    # Put the table-level UPDATE back, exactly as 0026 left it: REVOKE first, or the column grants
    # upgrade() made would survive beside it (0026:249-250, CONTRIBUTING.md "the REVOKE runs
    # before the GRANTs"). SELECT and INSERT are not named and do not move.
    op.execute("REVOKE UPDATE ON bookings FROM ziftbook_app")
    op.execute("GRANT UPDATE ON bookings TO ziftbook_app")
