"""booking_links, and bookings' reschedule snapshots: the guest booking link (ZIF-54).

booking_links holds only sha256(token): the raw token is minted by the worker at send time
(app/mail.py send_booking, D1) and never stored. Insert-only by privilege, exactly as
booking_events is (0026): the app role keeps SELECT and a column-list INSERT, and gets no UPDATE or
DELETE at all.

bookings gains four columns:
  - original_starts_at: the start AT INSERT, write-once (no UPDATE grant).
  - earliest_starts_at: the refund anchor (R4). = starts_at at insert; a RESCHEDULE sets
    LEAST(earliest_starts_at, :new), so it only ever goes down. The CHECK
    `earliest_starts_at <= original_starts_at` is the other half of that guarantee: together with
    the LEAST in every writer, a column with UPDATE granted can still never be raised past the
    original start it was insert-derived from. It does not by itself prevent a bad UPDATE from
    raising earliest_starts_at above the CURRENT starts_at (RESCHEDULE's own :new qualifier does
    that); it only pins the ceiling to the one value that never changes.
  - max_reschedules: a per-booking snapshot of the setting in force at insert (ZIF-55 pattern),
    write-once.
  - reschedule_count: how many times RESCHEDULE has run; the database enforces
    0 <= reschedule_count <= max_reschedules.

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
      -- Composite: a plain FK is checked with RLS bypassed and could point at another business's
      -- row.
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

    # DDL only, as 0027/0028 before it: bookings has FORCE ROW LEVEL SECURITY and a migration sets
    # no app.tenant_id, so any DML against it 42704s here. ADD COLUMN's validation scan bypasses the
    # policy, so the CHECKs below are the guard on existing rows: a row sold before this release has
    # reschedule_count = 0 (the DEFAULT), original_starts_at NULL and earliest_starts_at NULL, and
    # every CHECK below passes on that combination.
    op.execute("""
    ALTER TABLE bookings
      ADD COLUMN original_starts_at timestamptz,
      ADD COLUMN earliest_starts_at timestamptz,
      -- DEFAULT only because this migration is expand-only: the old app version, still deployed
      -- during the rollout, inserts without naming this column.
      ADD COLUMN max_reschedules integer NOT NULL DEFAULT 2
        CONSTRAINT ck_bookings_max_reschedules CHECK (max_reschedules BETWEEN 0 AND 10),
      ADD COLUMN reschedule_count integer NOT NULL DEFAULT 0,
      ADD CONSTRAINT ck_bookings_reschedule_count
        CHECK (reschedule_count BETWEEN 0 AND max_reschedules),
      -- A row sold before this release (NULL original) can never be rescheduled (reschedule_count
      -- stays 0 for it); it can still be cancelled.
      ADD CONSTRAINT ck_bookings_original_starts_at
        CHECK (reschedule_count = 0 OR original_starts_at IS NOT NULL),
      -- Passes on NULL (a legacy row, or a row with no original yet within one statement).
      ADD CONSTRAINT ck_bookings_earliest_starts_at
        CHECK (earliest_starts_at <= original_starts_at),
      -- Sign-off addition: earliest_starts_at is also never later than the CURRENT starts_at, at
      -- insert or after any RESCHEDULE (LEAST(earliest_starts_at, new_start) and the insert both
      -- set earliest_starts_at = starts_at at the moment they run). Passes on NULL, same as above.
      ADD CONSTRAINT ck_bookings_earliest_starts_at_current
        CHECK (earliest_starts_at <= starts_at)
    """)
    # bookings is a column-grant table since 0027 (its own comment there): a new column gets no
    # UPDATE privilege unless THIS migration grants it. original_starts_at (write-once) and
    # max_reschedules (snapshot) get none; reschedule_count and earliest_starts_at do, because
    # RESCHEDULE writes both. INSERT is table-level (0001's default privilege) and already covers
    # every new column.
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
