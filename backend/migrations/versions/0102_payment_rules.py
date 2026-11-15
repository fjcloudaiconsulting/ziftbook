"""How clients pay, per service, fixed on the booking when it is made (ZIF-62).

- services.payment_rule: in_person, choice (a card check or paying in full: what every service did
  until now, so the default), deposit or prepaid. services.deposit_pct, 1 to 99, set exactly when
  the rule is deposit.
- bookings.payment_rule and bookings.payment_due_minor: the snapshot of a booking placed with a
  payment step, both NULL for any other. What a `pay` charges is payment_due_minor: the price,
  except for a deposit, where it is a share of it (never more than the price). INSERT only for
  the app role: nothing changes a snapshot afterwards.
- A payment's amount now matches the booking's payment_due_minor, not its price (0044 tied it to
  the price exactly, with "ZIF-62 deposits revisit it"). A check is offered only under `choice`,
  where the two are equal, so a check still holds the full price. The existing bookings with a
  payment get the `choice` snapshot first, business by business under tenant_isolation, so every
  existing payment row meets the new key.
- bookings_payment_snapshot_default, a BEFORE INSERT trigger, gives a booking that starts
  awaiting_payment with no snapshot the `choice` one, for the deploy window only: the release still
  running places such bookings and records their payment at the price, and without the trigger
  the new key would refuse that payment (every checkout would fail). This release always writes the
  snapshot itself. The next release's contract migration drops it.

Lock: the backfill is a plain UPDATE (ROW EXCLUSIVE). Adding the unique key builds its index
under ACCESS EXCLUSIVE on bookings, as 0044's did, for the time one index build over bookings
takes; the key swap takes ACCESS EXCLUSIVE on booking_payments and checks the existing payment
rows. Both are held until this migration commits. That check reads both tables, which have FORCE
row-level security, so FORCE is lifted on both around it (app.db.lift_force's way, two tables
rather than CONTRIBUTING's one: lifting takes no lock the swap does not already hold). The column
grants touch no pg_class row (ZIF-287).

The downgrade drops the trigger, puts 0044's key back and drops the columns. It refuses while any
payment is not for its booking's full price (a deposit): that money record cannot meet the old key.

Revision ID: 0102
Revises: 0101
"""

from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"


def lift_both(statement: str) -> None:
    """app.db.lift_force, for a key between two FORCE tables: its check reads both, so FORCE is
    lifted on both. No extra lock: the key swap already holds ACCESS EXCLUSIVE on both."""
    op.execute("SET LOCAL row_security = off")
    op.execute("ALTER TABLE bookings NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE booking_payments NO FORCE ROW LEVEL SECURITY")
    op.execute(statement)
    op.execute("ALTER TABLE booking_payments FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE bookings FORCE ROW LEVEL SECURITY")
    op.execute("SET LOCAL row_security = on")


def upgrade() -> None:
    op.execute("""
    ALTER TABLE services
      ADD COLUMN payment_rule text NOT NULL DEFAULT 'choice'
        CONSTRAINT ck_services_payment_rule
          CHECK (payment_rule IN ('in_person', 'choice', 'deposit', 'prepaid')),
      ADD COLUMN deposit_pct smallint
        CONSTRAINT ck_services_deposit_pct CHECK (deposit_pct BETWEEN 1 AND 99),
      ADD CONSTRAINT ck_services_deposit_pct_with_deposit
        CHECK ((deposit_pct IS NOT NULL) = (payment_rule = 'deposit'))
    """)
    # A rule with no step (in_person) is never snapshotted: such a booking has none. Only a deposit
    # charges less than the price.
    op.execute("""
    ALTER TABLE bookings
      ADD COLUMN payment_rule text
        CONSTRAINT ck_bookings_payment_rule
          CHECK (payment_rule IN ('choice', 'deposit', 'prepaid')),
      ADD COLUMN payment_due_minor integer,
      ADD CONSTRAINT ck_bookings_payment_due CHECK (
        (payment_rule IS NULL) = (payment_due_minor IS NULL)
        AND payment_due_minor BETWEEN 1 AND price_amount_minor
        AND (payment_rule = 'deposit' OR payment_due_minor = price_amount_minor))
    """)
    # bookings is a column-grant table for INSERT (0047): INSERT_BOOKING writes both.
    op.execute("GRANT INSERT (payment_rule, payment_due_minor) ON bookings TO ziftbook_app")
    op.execute(f"""
    CREATE FUNCTION bookings_payment_snapshot_default() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER {SEARCH_PATH} AS $$
      BEGIN
        NEW.payment_rule := 'choice';
        NEW.payment_due_minor := NEW.price_amount_minor;
        RETURN NEW;
      END $$""")
    op.execute("""
    CREATE TRIGGER bookings_payment_snapshot_default BEFORE INSERT ON bookings
      FOR EACH ROW WHEN (NEW.status = 'awaiting_payment' AND NEW.payment_rule IS NULL)
      EXECUTE FUNCTION bookings_payment_snapshot_default()""")
    # Firing a trigger checks no EXECUTE privilege (0030), so no role needs it.
    op.execute("REVOKE EXECUTE ON FUNCTION bookings_payment_snapshot_default() FROM PUBLIC")
    op.execute("""
    DO $$
    DECLARE v_t uuid;
    BEGIN
      FOR v_t IN SELECT t.id FROM tenants t ORDER BY t.id LOOP
        PERFORM set_config('app.tenant_id', v_t::text, true);
        UPDATE bookings b SET payment_rule = 'choice', payment_due_minor = b.price_amount_minor
        WHERE b.tenant_id = v_t AND b.payment_rule IS NULL
          AND EXISTS (SELECT 1 FROM booking_payments p
                      WHERE p.tenant_id = b.tenant_id AND p.booking_id = b.id);
      END LOOP;
      PERFORM set_config('app.tenant_id', '', true);
    END $$""")
    op.execute("""
    ALTER TABLE bookings ADD CONSTRAINT uq_bookings_tenant_id_id_due
      UNIQUE (tenant_id, id, payment_due_minor, price_currency)""")
    lift_both("""
    ALTER TABLE booking_payments
      DROP CONSTRAINT fk_booking_payments_booking_price,
      ADD CONSTRAINT fk_booking_payments_booking_due
        FOREIGN KEY (tenant_id, booking_id, amount_minor, currency)
        REFERENCES bookings (tenant_id, id, payment_due_minor, price_currency)""")
    op.execute("ALTER TABLE bookings DROP CONSTRAINT uq_bookings_tenant_id_id_price")


def downgrade() -> None:
    op.execute("DROP TRIGGER bookings_payment_snapshot_default ON bookings")
    op.execute("DROP FUNCTION bookings_payment_snapshot_default()")
    op.execute("""
    ALTER TABLE bookings ADD CONSTRAINT uq_bookings_tenant_id_id_price
      UNIQUE (tenant_id, id, price_amount_minor, price_currency)""")
    lift_both("""
    ALTER TABLE booking_payments
      DROP CONSTRAINT fk_booking_payments_booking_due,
      ADD CONSTRAINT fk_booking_payments_booking_price
        FOREIGN KEY (tenant_id, booking_id, amount_minor, currency)
        REFERENCES bookings (tenant_id, id, price_amount_minor, price_currency)""")
    # No REVOKE: DROP COLUMN takes the privilege with it (0029's downgrade comment).
    op.execute("""
    ALTER TABLE bookings DROP CONSTRAINT uq_bookings_tenant_id_id_due,
      DROP COLUMN payment_rule, DROP COLUMN payment_due_minor
    """)
    op.execute("ALTER TABLE services DROP COLUMN payment_rule, DROP COLUMN deposit_pct")
