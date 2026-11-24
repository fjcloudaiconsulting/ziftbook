"""Require payment snapshots from the writer, after 0102's deploy window (ZIF-292).

Ship after 0.44.0, only once every running writer is at least 0.43.0. The current
writer supplies the snapshot itself; a pre-0.43.0 checkout would now be refused.
The CHECK is NOT VALID because pre-0102 awaiting rows without payments may have
no snapshot. Audit those rows before validating in a later migration; do not
invent payment terms for them. No scan or FORCE RLS lift here. bookings takes
ACCESS EXCLUSIVE until commit, bounded by env.py's five-second lock_timeout.

Revision ID: 0112
Revises: 0108
"""

from alembic import op

revision = "0112"
down_revision = "0108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER bookings_payment_snapshot_default ON bookings")
    op.execute("DROP FUNCTION bookings_payment_snapshot_default()")
    op.execute("""
    ALTER TABLE bookings ADD CONSTRAINT ck_bookings_awaiting_payment_snapshot
      CHECK (status <> 'awaiting_payment'
             OR (payment_rule IS NOT NULL AND payment_due_minor IS NOT NULL)) NOT VALID
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE bookings DROP CONSTRAINT ck_bookings_awaiting_payment_snapshot")
    # Restore 0102's removed function and trigger, including its body whitespace and privileges.
    op.execute("""
    CREATE FUNCTION bookings_payment_snapshot_default() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, public, pg_temp AS $$
      BEGIN
        NEW.payment_rule := 'choice';
        NEW.payment_due_minor := NEW.price_amount_minor;
        RETURN NEW;
      END $$""")
    op.execute("""
    CREATE TRIGGER bookings_payment_snapshot_default BEFORE INSERT ON bookings
      FOR EACH ROW WHEN (NEW.status = 'awaiting_payment' AND NEW.payment_rule IS NULL)
      EXECUTE FUNCTION bookings_payment_snapshot_default()""")
    op.execute("REVOKE EXECUTE ON FUNCTION bookings_payment_snapshot_default() FROM PUBLIC")
