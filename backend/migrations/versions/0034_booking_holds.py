"""booking_holds: a guest's chosen time, held while they confirm their emailed link (ZIF-117).

A booking made without signing in exists only after its booker opens the emailed link and confirms
(owner ruling 2026-10-08). Until then this row holds the slot for 15 minutes (app.holds.HOLD_TTL,
`expires_at`) and keeps the link alive for 24 hours from `created_at` (app.holds.LINK_TTL), after
which the worker's sweep deletes it. It carries no name, phone, IP, user agent or consent: the
address is the only thing stored about a person who has not proven it.

No EXCLUDE constraint, unlike bookings (0026): the only overlap worth refusing is with a LIVE hold,
and "live" needs now(), which an index predicate cannot use (it must be IMMUTABLE). What keeps two
holds, or a hold and a booking, apart is the tenant advisory lock plus re-derivation through
app.availability, whose BOOKED reads live holds beside bookings.

############################################################################################
EVERY WRITER OF THIS TABLE MUST FIRST TAKE
    SELECT pg_advisory_xact_lock(51, hashtext(current_setting('app.tenant_id')))
as the FIRST statement of its transaction: the hold POST (its `replaces` delete included), the
confirm, the sweep and the verify email's token mint. Without it two holds for one worker and
time can both pass re-derivation and both commit, and nothing in the database refuses them.
The one exception is the ON DELETE CASCADE from memberships below: it only ever frees capacity.
############################################################################################

Revision ID: 0034
Revises: 0033
"""

from alembic import op

from app.db import enable_tenant_isolation

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

# id and created_at are left out: the app can neither choose an id nor backdate a hold, and the
# link's 24 hours run from created_at. token_hash is set by its own UPDATE grant, at send time.
INSERT_COLUMNS = (
    "tenant_id, service_id, worker_id, anyone, starts_at, ends_at, email, locale, secret_hash, "
    "expires_at"
)


def upgrade() -> None:
    op.execute("""
    CREATE TABLE booking_holds (
      id uuid CONSTRAINT pk_booking_holds PRIMARY KEY DEFAULT uuidv7(),
      tenant_id uuid NOT NULL
        CONSTRAINT fk_booking_holds_tenant_id_tenants REFERENCES tenants (id),
      service_id uuid NOT NULL,
      -- The worker the hold blocks. For "anyone" (anyone = true) the confirm may still land on
      -- another worker who is free then; a named worker never changes.
      worker_id uuid NOT NULL,
      anyone boolean NOT NULL,
      starts_at timestamptz NOT NULL,
      ends_at timestamptz NOT NULL
        CONSTRAINT ck_booking_holds_ends_after_start CHECK (ends_at > starts_at),
      -- The bound app.availability.BOOKED's one-day look-back relies on, as bookings' (0026).
      CONSTRAINT ck_booking_holds_at_most_12_hours
        CHECK (ends_at - starts_at <= interval '12 hours'),
      -- Normalised in Python (app.passwords.normalise_email), as clients.email (0024).
      email text NOT NULL CONSTRAINT ck_booking_holds_email_lowercase CHECK (email = lower(email)),
      locale text CONSTRAINT ck_booking_holds_locale CHECK (locale IN ('en', 'nl', 'pt')),
      -- sha256 of the secret the hold POST answered with, kept only in the booker's page: only that
      -- page can replace this hold (send again, wrong address, another time).
      secret_hash bytea NOT NULL CONSTRAINT uq_booking_holds_secret_hash UNIQUE
        CONSTRAINT ck_booking_holds_secret_hash_length CHECK (octet_length(secret_hash) = 32),
      -- sha256 of the emailed link's token; NULL until the email is sent.
      token_hash bytea CONSTRAINT uq_booking_holds_token_hash UNIQUE
        CONSTRAINT ck_booking_holds_token_hash_length CHECK (octet_length(token_hash) = 32),
      expires_at timestamptz NOT NULL,
      -- clock_timestamp, not now(), as every evidence table (0026).
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      -- Composite every time: a plain foreign key is checked with row-level security bypassed.
      CONSTRAINT fk_booking_holds_tenant_id_service_id_services
        FOREIGN KEY (tenant_id, service_id) REFERENCES services (tenant_id, id),
      -- CASCADE, unlike bookings: a hold is not evidence, and removing a member drops their holds.
      CONSTRAINT fk_booking_holds_tenant_id_worker_id_memberships
        FOREIGN KEY (tenant_id, worker_id) REFERENCES memberships (tenant_id, id) ON DELETE CASCADE
    )""")
    # BOOKED's read, shaped like ix_bookings_tenant_id_worker_id_starts_at.
    op.execute("""
    CREATE INDEX ix_booking_holds_tenant_id_worker_id_starts_at
      ON booking_holds (tenant_id, worker_id, starts_at)""")
    # referenced=False: nothing has a foreign key to booking_holds.
    enable_tenant_isolation("booking_holds", referenced=False)
    # The REVOKE runs before the GRANTs (CONTRIBUTING.md).
    op.execute("REVOKE ALL ON booking_holds FROM ziftbook_app")
    op.execute("GRANT SELECT, DELETE ON booking_holds TO ziftbook_app")
    op.execute(f"GRANT INSERT ({INSERT_COLUMNS}) ON booking_holds TO ziftbook_app")
    op.execute("GRANT UPDATE (token_hash) ON booking_holds TO ziftbook_app")


def downgrade() -> None:
    # Loses only live holds and links, none older than 24 hours: their bookers book again.
    op.execute("DROP TABLE booking_holds")
