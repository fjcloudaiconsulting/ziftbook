"""bookings.original_starts_at: movable while a booking is pending (ZIF-157).

0029 made the refund anchor write-once by withholding the UPDATE grant. A client may now move a
pending request, and the new time becomes the anchor (nothing is refundable on a pending booking).
So the column is granted, and a trigger keeps the old guarantee: once the booking is no longer
pending, no change to original_starts_at is accepted. The trigger is the only thing standing
between the app role and a rewritten refund anchor.

Revision ID: 0042
Revises: 0041
"""

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE FUNCTION refuse_anchor_move() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, public, pg_temp AS $$
    BEGIN
      RAISE EXCEPTION 'the refund anchor moves only while a booking is pending'
        USING ERRCODE = '23514', CONSTRAINT = 'original_starts_at_while_pending';
    END $$""")
    op.execute("""
    CREATE TRIGGER original_starts_at_while_pending BEFORE UPDATE OF original_starts_at ON bookings
      FOR EACH ROW WHEN (OLD.status <> 'pending'
                         AND NEW.original_starts_at IS DISTINCT FROM OLD.original_starts_at)
      EXECUTE FUNCTION refuse_anchor_move()""")
    op.execute("GRANT UPDATE (original_starts_at) ON bookings TO ziftbook_app")


def downgrade() -> None:
    # A column grant, so the REVOKE removes exactly what the upgrade added.
    op.execute("REVOKE UPDATE (original_starts_at) ON bookings FROM ziftbook_app")
    op.execute("DROP TRIGGER original_starts_at_while_pending ON bookings")
    op.execute("DROP FUNCTION refuse_anchor_move()")
