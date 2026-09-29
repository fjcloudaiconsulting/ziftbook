"""bookings.decline_message: what a merchant tells the client when declining (ZIF-121).

Revision ID: 0031
Revises: 0030
"""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # DDL only, and every existing row is NULL. The explicit IS NULL matters: `NULL AND FALSE` is
    # FALSE, so without it the CHECK would refuse every row that is not declined.
    op.execute("""
    ALTER TABLE bookings
      ADD COLUMN decline_message text
        CONSTRAINT ck_bookings_decline_message CHECK (
          decline_message IS NULL
          OR (char_length(decline_message) BETWEEN 1 AND 1000 AND status = 'declined'))
    """)
    # Column grants (0027): the app role may only ever set (and, for erasure, clear) this one.
    op.execute("GRANT UPDATE (decline_message) ON bookings TO ziftbook_app")


def downgrade() -> None:
    # Loses every stored message, and there is no honest value to restore. No REVOKE: DROP COLUMN
    # takes the privilege with it (0029).
    op.execute("ALTER TABLE bookings DROP COLUMN decline_message")
