"""ziftbook's fee under business_pays (ZIF-80).

The platform fee now comes from the business's plan (entitlements), by the booking's source, and is
charged while the business pays Stripe's fees itself. Two CHECKs allowed a fee only in the retired
business_pays_plus_fee mode; both go:

- ck_payment_accounts_fee_only_in_mode_2: payment_accounts' fee columns are no longer read (they
  stay until a later contract).
- ck_booking_payments_fee_needs_mode_2: replaced by ck_booking_payments_fee_only_on_pay, the half
  of it that still holds (a card check is never charged, so never carries a fee). Added NOT VALID:
  the dropped check already held every existing row to it, and a validating scan would need FORCE
  lifted on booking_payments (CONTRIBUTING, "Database migrations"). New rows are checked either way.

Lock: ACCESS EXCLUSIVE on each table for the catalog change only; nothing is scanned. No grant
changes.

The downgrade drops the new check and re-adds the two old ones NOT VALID: rows written meanwhile
may carry a fee under business_pays, which the old checks would refuse to validate.

Revision ID: 0107
Revises: 0106
"""

from alembic import op

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE payment_accounts DROP CONSTRAINT ck_payment_accounts_fee_only_in_mode_2"
    )
    op.execute("""
    ALTER TABLE booking_payments
      DROP CONSTRAINT ck_booking_payments_fee_needs_mode_2,
      ADD CONSTRAINT ck_booking_payments_fee_only_on_pay
        CHECK (application_fee_minor = 0 OR kind = 'pay') NOT VALID""")


def downgrade() -> None:
    op.execute("""
    ALTER TABLE booking_payments
      DROP CONSTRAINT ck_booking_payments_fee_only_on_pay,
      ADD CONSTRAINT ck_booking_payments_fee_needs_mode_2
        CHECK (application_fee_minor = 0 OR (kind = 'pay' AND fee_mode = 'business_pays_plus_fee'))
        NOT VALID""")
    op.execute("""
    ALTER TABLE payment_accounts
      ADD CONSTRAINT ck_payment_accounts_fee_only_in_mode_2
        CHECK (fee_mode = 'business_pays_plus_fee'
               OR (application_fee_bp = 0 AND application_fee_fixed_minor = 0)) NOT VALID""")
