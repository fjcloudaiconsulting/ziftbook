"""bookings: the evidence and the refund anchor are immutable to the app role (ZB-09).

0027 withheld UPDATE from its two new snapshot columns only, so the rest of the evidence (the price,
the service, the source, the client, created_at, id) stayed UPDATE-able, and INSERT stayed
table-level, so a row could be written with any created_at or id. The app role now holds UPDATE on
exactly the columns app/bookings.py's UPDATEs set, and INSERT on exactly the columns its
INSERT_BOOKING names.

0042 lets original_starts_at move only while a booking is pending. A trigger now keeps a settled
booking from becoming pending again: an UPDATE makes a booking pending only from awaiting_payment
(its card check landed, PAID) or keeps it pending, and never makes one awaiting_payment, which
only INSERT does (a checkout is born in it).

MAINTENANCE TRAP, as booking_events' (0026:44-49): bookings is now a column-grant table for INSERT
as well as UPDATE. A column added later that place() / INSERT_BOOKING writes (the one insert, for
public and merchant bookings alike) needs its own
`GRANT INSERT (column) ON bookings TO ziftbook_app` in ITS migration, applied before the app that
writes it, or EVERY booking insert fails, not only writes of the new column. UPDATE likewise, and
only for a column a route updates.

Revision ID: 0047
Revises: 0046
"""

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

# What app/bookings.py's UPDATEs set (EXPIRE, EXPIRE_ONE, PAID, TRANSITION, CLIENT_CANCEL,
# RESCHEDULE, PENDING_MOVE, MOVE), and nothing else.
UPDATABLE = """
status, expires_at, decline_message, starts_at, ends_at, earliest_starts_at, reschedule_count,
original_starts_at, worker_id, worker_display_name
"""
# What app/bookings.py's INSERT_BOOKING names, and nothing else: not id or created_at (their
# defaults), not reschedule_count (0 at birth) or decline_message (only a decline sets it).
INSERTABLE = """
tenant_id, client_id, worker_id, service_id, starts_at, ends_at, status, expires_at, source,
service_name, price_amount_minor, price_currency, duration_minutes, cancellation_policy_text,
auto_confirm_at_booking, worker_display_name, free_cancellation_hours, reschedule_cutoff_hours,
original_starts_at, earliest_starts_at, max_reschedules, client_name, client_phone,
client_message, checkout_hash
"""
# What the app role could UPDATE before this migration: 0027's UPDATABLE plus the column grants of
# 0029 (reschedule_count, earliest_starts_at), 0031 (decline_message) and 0042
# (original_starts_at). The downgrade puts back exactly these.
UPDATABLE_AT_0046 = """
id, tenant_id, client_id, worker_id, service_id, starts_at, ends_at, status, expires_at, source,
service_name, price_amount_minor, price_currency, duration_minutes, cancellation_policy_text,
auto_confirm_at_booking, worker_display_name, created_at, reschedule_count, earliest_starts_at,
decline_message, original_starts_at
"""


def upgrade() -> None:
    # Each REVOKE runs before its GRANT (CONTRIBUTING.md): revoking a table-level privilege also
    # takes every column grant of it, so the other order would leave nothing. SELECT, and DELETE
    # (revoked in 0026), do not move.
    op.execute("REVOKE UPDATE ON bookings FROM ziftbook_app")
    op.execute(f"GRANT UPDATE ({UPDATABLE}) ON bookings TO ziftbook_app")
    op.execute("REVOKE INSERT ON bookings FROM ziftbook_app")
    op.execute(f"GRANT INSERT ({INSERTABLE}) ON bookings TO ziftbook_app")

    # 23514 by name, as 0042's trigger raises. UPDATE OF status: a statement that does not set the
    # status cannot change it. One trigger, two arms, because the two statuses are one path: a
    # booking is pending only after awaiting_payment, so letting an UPDATE make it awaiting_payment
    # again would undo the first arm.
    op.execute(f"""
    CREATE FUNCTION bookings_status_to_pending() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER {SEARCH_PATH} AS $$
      BEGIN
        RAISE EXCEPTION 'awaiting_payment is set only at placement, and pending only from it'
          USING ERRCODE = '23514', CONSTRAINT = 'bookings_status_to_pending';
      END $$""")
    op.execute("""
    CREATE TRIGGER bookings_status_to_pending BEFORE UPDATE OF status ON bookings
      FOR EACH ROW WHEN ((NEW.status = 'pending'
                          AND OLD.status NOT IN ('awaiting_payment', 'pending'))
                         OR (NEW.status = 'awaiting_payment'
                             AND OLD.status <> 'awaiting_payment'))
      EXECUTE FUNCTION bookings_status_to_pending()""")
    # Firing a trigger checks no EXECUTE privilege (0030), so no role needs it.
    op.execute("REVOKE EXECUTE ON FUNCTION bookings_status_to_pending() FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TRIGGER bookings_status_to_pending ON bookings")
    op.execute("DROP FUNCTION bookings_status_to_pending()")
    # Back to what 0046 left: table-level INSERT (0001's default privileges) and UPDATE on
    # UPDATABLE_AT_0046. REVOKE first, as above.
    op.execute("REVOKE INSERT ON bookings FROM ziftbook_app")
    op.execute("GRANT INSERT ON bookings TO ziftbook_app")
    op.execute("REVOKE UPDATE ON bookings FROM ziftbook_app")
    op.execute(f"GRANT UPDATE ({UPDATABLE_AT_0046}) ON bookings TO ziftbook_app")
