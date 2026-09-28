"""booking_links (sha256 only, insert-only) and bookings' reschedule snapshots (ZIF-54).

Revision ID: 0029
Revises: 0028
"""

from alembic import op

from app.db import enable_tenant_isolation

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

LINK_COLUMNS = "token_hash, tenant_id, booking_id"


def upgrade() -> None:
    op.execute("""
    CREATE TABLE booking_links (
      token_hash bytea NOT NULL CONSTRAINT pk_booking_links PRIMARY KEY
        CONSTRAINT ck_booking_links_token_hash_length CHECK (octet_length(token_hash) = 32),
      tenant_id uuid NOT NULL CONSTRAINT fk_booking_links_tenant_id_tenants REFERENCES tenants (id),
      booking_id uuid NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      -- Composite: a plain FK is checked with RLS bypassed and could name another tenant's row.
      CONSTRAINT fk_booking_links_tenant_id_booking_id_bookings
        FOREIGN KEY (tenant_id, booking_id) REFERENCES bookings (tenant_id, id)
    )""")
    op.execute("""
    CREATE INDEX ix_booking_links_tenant_id_booking_id ON booking_links (tenant_id, booking_id)
    """)
    # referenced=False: nothing else has a foreign key to booking_links.
    enable_tenant_isolation("booking_links", referenced=False)
    # The REVOKE runs before the GRANTs (CONTRIBUTING.md). Insert-only: no UPDATE, no DELETE.
    op.execute("REVOKE ALL ON booking_links FROM ziftbook_app")
    op.execute("GRANT SELECT ON booking_links TO ziftbook_app")
    op.execute(f"GRANT INSERT ({LINK_COLUMNS}) ON booking_links TO ziftbook_app")

    # DDL only (FORCE RLS, no app.tenant_id); every CHECK passes on a legacy row (count 0, NULLs).
    op.execute("""
    ALTER TABLE bookings
      ADD COLUMN original_starts_at timestamptz,
      ADD COLUMN earliest_starts_at timestamptz,
      -- DEFAULT only for expand-only: the old app version inserts without naming this column.
      ADD COLUMN max_reschedules integer NOT NULL DEFAULT 2
        CONSTRAINT ck_bookings_max_reschedules CHECK (max_reschedules BETWEEN 0 AND 10),
      ADD COLUMN reschedule_count integer NOT NULL DEFAULT 0,
      ADD CONSTRAINT ck_bookings_reschedule_count
        CHECK (reschedule_count BETWEEN 0 AND max_reschedules),
      -- A legacy row (NULL original) can never be rescheduled.
      ADD CONSTRAINT ck_bookings_original_starts_at
        CHECK (reschedule_count = 0 OR original_starts_at IS NOT NULL),
      -- The refund anchor never rises past the write-once original start.
      ADD CONSTRAINT ck_bookings_earliest_starts_at
        CHECK (earliest_starts_at <= original_starts_at),
      -- Nor past the CURRENT start.
      ADD CONSTRAINT ck_bookings_earliest_starts_at_current
        CHECK (earliest_starts_at <= starts_at)
    """)
    # Column grants (0027): no UPDATE on original_starts_at or max_reschedules: write-once.
    op.execute("GRANT UPDATE (reschedule_count, earliest_starts_at) ON bookings TO ziftbook_app")


def downgrade() -> None:
    # One-way on real data, like 0027's: a booking already rescheduled loses reschedule_count and
    # earliest_starts_at, and there is no honest value to backfill them with afterwards. Unlike
    # 0027 this DOES succeed unconditionally (every new column here is nullable-or-defaulted), so no
    # finally-block workaround is needed by callers -- but the history it drops is still gone.
    op.execute("DROP TABLE booking_links")
    op.execute("""
    ALTER TABLE bookings
      DROP COLUMN original_starts_at, DROP COLUMN earliest_starts_at,
      DROP COLUMN max_reschedules, DROP COLUMN reschedule_count
    """)
    # No REVOKE here: DROP COLUMN already took the privilege with it, and naming a dropped column
    # in a REVOKE fails (42703).
