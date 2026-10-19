"""bookings: what the booker typed, kept on the booking itself (ZIF-157).

A client row is one per address and is rewritten whenever its owner books again, so reading the
name and phone from it shows the LATEST booking's values on every earlier booking. These columns
snapshot the name, the E.164 phone and the optional message a booking page booking was made with.

Nullable with no backfill: old rows and merchant bookings read the client row through coalesce.
There is NO UPDATE grant: bookings has been a column-grant table since 0027, so a new column is
write-once for ziftbook_app by default (INSERT is table-level). Erasure (ZIF-58) must GRANT
UPDATE on these three columns and NULL them.

Revision ID: 0041
Revises: 0040
"""

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE bookings
      ADD COLUMN client_name text
        CONSTRAINT ck_bookings_client_name CHECK (char_length(client_name) BETWEEN 1 AND 100),
      ADD COLUMN client_phone text
        CONSTRAINT ck_bookings_client_phone CHECK (client_phone ~ '^\\+[1-9][0-9]{1,14}$'),
      ADD COLUMN client_message text
        CONSTRAINT ck_bookings_client_message CHECK (char_length(client_message) BETWEEN 1 AND 1000)
    """)


def downgrade() -> None:
    # No REVOKE: DROP COLUMN takes the privilege with it (0029's downgrade comment).
    op.execute("""
    ALTER TABLE bookings
      DROP COLUMN client_name, DROP COLUMN client_phone, DROP COLUMN client_message
    """)
