"""time_off: whole-day and multi-day blocks, stored as local calendar dates (ZIF-101).

Revision ID: 0028
Revises: 0027
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # DDL only: time_off has FORCE RLS (0019:52) and a migration sets no app.tenant_id, so any DML
    # fails. None is needed: every existing row has both instants and NULL dates, a valid partial
    # block. ADD CONSTRAINT still validates every tenant's rows (a DDL scan ignores the policy;
    # measured with a probe CHECK).
    op.execute("""
    ALTER TABLE time_off
      ALTER COLUMN starts_at DROP NOT NULL,
      ALTER COLUMN ends_at DROP NOT NULL,
      ADD COLUMN first_day date,
      ADD COLUMN last_day date,  -- included
      ADD CONSTRAINT ck_time_off_kind CHECK (
        (starts_at, ends_at) IS NOT NULL AND (first_day, last_day) IS NULL
        OR (starts_at, ends_at) IS NULL AND (first_day, last_day) IS NOT NULL),
      -- A day count, not elapsed time: 366 whole days over two fall-backs is 366 d 1 h, which
      -- ck_time_off_at_most_366_days would refuse. That CHECK passes on NULL and never sees day
      -- rows.
      ADD CONSTRAINT ck_time_off_days CHECK (
        last_day >= first_day AND last_day - first_day < 366
        AND first_day >= DATE '2000-01-01' AND last_day <= DATE '2999-12-31')
    """)


def downgrade() -> None:
    # Refuses with 23502 while any whole-day block exists (SET NOT NULL scans every tenant's rows;
    # measured). Deliberate: converting needs the business zone and DML, both impossible here, and
    # dropping them would make days off bookable. To go down, delete the whole-day blocks first
    # (through the API, or per tenant with app.tenant_id set).
    op.execute("""
    ALTER TABLE time_off
      DROP CONSTRAINT ck_time_off_days, DROP CONSTRAINT ck_time_off_kind,
      ALTER COLUMN starts_at SET NOT NULL, ALTER COLUMN ends_at SET NOT NULL
    """)
    op.execute("ALTER TABLE time_off DROP COLUMN first_day, DROP COLUMN last_day")
