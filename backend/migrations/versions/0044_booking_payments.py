"""booking_payments: a booking's card check or payment at Stripe (ZIF-158).

One row per Stripe intent, never a card number, brand, PaymentMethod id or client_secret. The amount
and currency are the booking's price snapshot exactly (a composite foreign key), the fee policy that
applied is write-once by grant, and a booking has at most one live payment. Payment rows are
evidence, like bookings: no DELETE.

Revision ID: 0044
Revises: 0043
"""

from alembic import op

from app.db import enable_tenant_isolation

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The target of the payment's link to its booking's price snapshot (one more index on bookings).
    op.execute("""
    ALTER TABLE bookings ADD CONSTRAINT uq_bookings_tenant_id_id_price
      UNIQUE (tenant_id, id, price_amount_minor, price_currency)""")
    op.execute("""
    CREATE TABLE booking_payments (
      id uuid CONSTRAINT pk_booking_payments PRIMARY KEY DEFAULT uuidv7(),
      tenant_id uuid NOT NULL
        CONSTRAINT fk_booking_payments_tenant_id_tenants REFERENCES tenants (id),
      booking_id uuid NOT NULL,
      kind text NOT NULL CONSTRAINT ck_booking_payments_kind CHECK (kind IN ('check', 'pay')),
      intent_id text NOT NULL CONSTRAINT uq_booking_payments_intent_id UNIQUE
        CONSTRAINT ck_booking_payments_intent_id
          CHECK (intent_id ~ '^(pi|seti)_[A-Za-z0-9]{1,255}$'),
      amount_minor integer NOT NULL
        CONSTRAINT ck_booking_payments_amount_minor CHECK (amount_minor BETWEEN 1 AND 1000000),
      currency text NOT NULL,
      -- D6: the fee policy that applied to this payment, copied from payment_accounts at intent
      -- creation
      fee_mode text NOT NULL CONSTRAINT ck_booking_payments_fee_mode
        CHECK (fee_mode IN ('business_pays', 'business_pays_plus_fee', 'platform_pays')),
      application_fee_minor integer NOT NULL DEFAULT 0
        CONSTRAINT ck_booking_payments_application_fee_minor
          CHECK (application_fee_minor BETWEEN 0 AND amount_minor),
      -- D7: Stripe's processing fee on this payment's charge, read from its balance transaction;
      -- NULL until known
      stripe_fee_minor integer
        CONSTRAINT ck_booking_payments_stripe_fee_minor
          CHECK (stripe_fee_minor BETWEEN 0 AND amount_minor),
      CONSTRAINT ck_booking_payments_fee_needs_mode_2
        CHECK (application_fee_minor = 0 OR (kind = 'pay' AND fee_mode = 'business_pays_plus_fee')),
      status text NOT NULL DEFAULT 'open'
        CONSTRAINT ck_booking_payments_status
          CHECK (status IN ('open', 'checked', 'paid', 'canceled')),
      refunded_minor integer NOT NULL DEFAULT 0
        CONSTRAINT ck_booking_payments_refunded_minor
          CHECK (refunded_minor BETWEEN 0 AND amount_minor),
      -- any Stripe object prefix: refunds for some methods may not be re_ (sandbox V6); never fail
      -- after paying out
      refund_id text
        CONSTRAINT ck_booking_payments_refund_id
          CHECK (refund_id ~ '^[a-z]{2,8}_[A-Za-z0-9]{1,255}$'),
      -- B3: set when Stripe reports the refund failed; the console shows it and nothing reads it as
      -- refunded
      refund_failed_at timestamptz,
      settled_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      CONSTRAINT ck_booking_payments_kind_status
        CHECK ((kind = 'check' AND status <> 'paid') OR (kind = 'pay' AND status <> 'checked')),
      CONSTRAINT ck_booking_payments_setup_is_a_check
        CHECK (intent_id LIKE 'pi\\_%' OR kind = 'check'),
      CONSTRAINT ck_booking_payments_refund_needs_paid
        CHECK (refunded_minor = 0 OR status = 'paid'),
      -- the price snapshot, exactly: never a 0, another amount or another currency (ZIF-62
      -- deposits revisit it)
      CONSTRAINT fk_booking_payments_booking_price
        FOREIGN KEY (tenant_id, booking_id, amount_minor, currency)
        REFERENCES bookings (tenant_id, id, price_amount_minor, price_currency))""")
    op.execute(
        "CREATE UNIQUE INDEX uq_booking_payments_live ON booking_payments (booking_id) "
        "WHERE status <> 'canceled'"
    )
    op.execute(
        "CREATE INDEX ix_booking_payments_unsettled ON booking_payments (tenant_id) "
        "WHERE settled_at IS NULL"
    )
    enable_tenant_isolation("booking_payments")
    # The REVOKE runs before the GRANTs (CONTRIBUTING.md). No DELETE: payment rows are evidence.
    op.execute("REVOKE ALL ON booking_payments FROM ziftbook_app")
    op.execute("GRANT SELECT ON booking_payments TO ziftbook_app")
    op.execute(
        "GRANT INSERT (tenant_id, booking_id, kind, intent_id, amount_minor, currency, fee_mode, "
        "application_fee_minor) ON booking_payments TO ziftbook_app"
    )
    # fee_mode and application_fee_minor are write-once by grant: the record of what applied.
    op.execute(
        "GRANT UPDATE (status, refunded_minor, refund_id, settled_at, stripe_fee_minor, "
        "refund_failed_at) ON booking_payments TO ziftbook_app"
    )


def downgrade() -> None:
    # Loses every payment record (the intents live on at Stripe).
    op.execute("DROP TABLE booking_payments")
    op.execute("ALTER TABLE bookings DROP CONSTRAINT uq_bookings_tenant_id_id_price")
