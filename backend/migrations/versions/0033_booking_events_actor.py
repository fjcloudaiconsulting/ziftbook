"""booking_events.actor_user_id and details: who did it, and what changed (ZIF-57).

The booking panel's history reads "Accepted by Joana" and "Moved from 10:00 to 10:30"; neither was
recorded. actor_user_id is the signed-in person behind a merchant-side event, NULL for the client,
the system and every row written before this migration (no backfill: there is no honest value to
invent, and the API derives team/client/system from the event and the booking's source instead).
details is a small object for events that carry data, today only `rescheduled` ({from, to}).

No foreign key on actor_user_id, as audit_events.actor_user_id (0010): members.remove hard-deletes
memberships, and an event must outlive the person who caused it (the API then reads the actor's
name as null). The CHECK is validated in-transaction: transaction_per_migration holds the table
lock for the whole migration anyway (env.py), and booking_events is small.

Deploy order: this migration must run BEFORE the new app. A new app on a pre-0033 database fails
every INSERT_EVENT with 42703 (public create, PATCH, link actions). An old app on the new database
is fine: both columns are nullable.

Revision ID: 0033
Revises: 0032
"""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

# Column-list grants (0026:44-48): a new column is NOT insertable by the app role until it is named
# in a GRANT INSERT of its own. No UPDATE, no DELETE: booking_events stays append-only (0026).
ADDED_COLUMNS = "actor_user_id, details"


def upgrade() -> None:
    # NULL passes the CHECK by itself (a NULL condition is not a violation).
    op.execute("""
    ALTER TABLE booking_events
      ADD COLUMN actor_user_id uuid,
      ADD COLUMN details jsonb
        CONSTRAINT ck_booking_events_details CHECK (jsonb_typeof(details) = 'object')
    """)
    op.execute(f"GRANT INSERT ({ADDED_COLUMNS}) ON booking_events TO ziftbook_app")


def downgrade() -> None:
    # Loses who acted and every reschedule's from/to. No REVOKE: DROP COLUMN takes the privilege
    # with it (0029), and the CHECK goes with its column.
    op.execute("ALTER TABLE booking_events DROP COLUMN actor_user_id, DROP COLUMN details")
