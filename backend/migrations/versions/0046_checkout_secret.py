"""bookings.checkout_hash: the checkout secret's SHA-256 (ZB-15).

A booking page booking placed with a payment step hands its browser a random secret in a cookie and
keeps only its hash here; sync, resume and abandon compare the two. Expand only: nullable with no
backfill. A NULL is a booking placed by the release before this one (or one with no payment step),
and the payment routes still answer it during the deploy overlap.

There is NO UPDATE grant: bookings has been a column-grant table since 0027, so the column is
write-once for ziftbook_app (INSERT is table-level, and create() writes it in the INSERT).

Revision ID: 0046
Revises: 0045
"""

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE bookings ADD COLUMN checkout_hash bytea")


def downgrade() -> None:
    # No REVOKE: DROP COLUMN takes the privilege with it (0029's downgrade comment).
    op.execute("ALTER TABLE bookings DROP COLUMN checkout_hash")
