"""bookings and booking_events: a client holds one worker for one interval, and the database - not
the application - is what makes that exclusive.

The app role can add and change a booking but never delete one: a booking is evidence (ZIF-5), and
ZIF-58 pseudonymises rather than deletes. booking_events is append-only by privilege, exactly as
audit_events (0010:118-123) and consents (0024:106-109) are.

The exclusion constraint's predicate is ZIF-5's four occupying statuses. `no_show` is deliberately
NOT among them: see the module comment on OCCUPYING below and docs/specs ZIF-51 SS3.3.

Revision ID: 0026
Revises: 0025
"""

from alembic import op

from app.db import enable_tenant_isolation

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

STATUSES = (
    "awaiting_payment",
    "pending",
    "confirmed",
    "completed",
    "declined",
    "expired",
    "cancelled_by_client",
    "cancelled_by_merchant",
    "no_show",
)
# app.availability.OCCUPYING must equal this tuple, exactly and in this order. The keystone fence
# (tests/test_bookings_db.py, test 12) reads pg_get_constraintdef and asserts it.
OCCUPYING = ("awaiting_payment", "pending", "confirmed", "completed")

OVERLAP = "ex_bookings_worker_overlap"  # app/bookings.py answers it with 409 slot_taken

# id and created_at are left out on purpose: with no column grant the app can neither backdate a
# row nor choose its id, which is what makes the log append-only (audit_events 0010:118-123,
# consents 0024:106-109).
#
# MAINTENANCE TRAP, repeated here because it has bitten twice: a column added to booking_events
# later needs its own `GRANT INSERT (column) ON booking_events TO ziftbook_app` in ITS migration, or
# EVERY event write fails - not only writes of the new column - because this grant is a column list.
# That migration must also be applied BEFORE the app that writes the column.
EVENT_COLUMNS = "tenant_id, booking_id, event, ip, user_agent, policy_version, consent_purposes"


def languages(column: str) -> str:
    # Copied verbatim from 0016, on purpose: migrations never import each other.
    return (
        f"CASE WHEN jsonb_typeof({column}) = 'object' "
        f"THEN {column} - ARRAY['en', 'nl', 'pt'] = '{{}}'::jsonb ELSE false END"
    )


def quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE bookings (
      id uuid CONSTRAINT pk_bookings PRIMARY KEY DEFAULT uuidv7(),
      tenant_id uuid NOT NULL
        CONSTRAINT fk_bookings_tenant_id_tenants REFERENCES tenants (id),

      -- Who, by whom, of what. Composite every time: a plain foreign key is checked with
      -- row-level security bypassed and could point at another business's row.
      -- No ON DELETE anywhere: a booking outlives nothing. Removing a member who holds one is a
      -- refusal the API turns into 409 has_bookings (app/members.py), never a cascade that
      -- silently destroys a business record.
      client_id uuid NOT NULL,
      worker_id uuid NOT NULL,
      service_id uuid NOT NULL,

      starts_at timestamptz NOT NULL,
      ends_at timestamptz NOT NULL
        CONSTRAINT ck_bookings_ends_after_start CHECK (ends_at > starts_at),
      -- The bound app.availability.booked()'s look-back derives from (N7). 12 hours is exactly the
      -- longest service (ck_services_duration_minutes caps duration at 720 minutes), so it costs a
      -- legitimate booking nothing.
      --
      -- Deliberately NOT `ends_at = starts_at + make_interval(mins => duration_minutes)`. That
      -- would forbid ruling 2's own deciding case: a 30-minute service recorded 10:15-11:00 for a
      -- walk-in by the merchant, where the interval and the snapshotted duration legitimately
      -- differ because the snapshot is what was SOLD and the interval is what HAPPENED.
      CONSTRAINT ck_bookings_at_most_12_hours CHECK (ends_at - starts_at <= interval '12 hours'),

      status text NOT NULL
        CONSTRAINT ck_bookings_status CHECK (status IN ({quoted(STATUSES)})),
      -- The TTL of a status that holds a slot without being settled. One-way, never a
      -- biconditional: `expired` and `cancelled_*` keep the expiry they were given, and a
      -- biconditional would make the expiry UPDATE itself a check violation.
      expires_at timestamptz
        CONSTRAINT ck_bookings_expires_at
          CHECK (status <> ALL (ARRAY['awaiting_payment', 'pending']) OR expires_at IS NOT NULL),

      -- Drives the platform fee (ZIF-77) and cannot be reconstructed afterwards (ZIF-51 AC).
      source text NOT NULL
        CONSTRAINT ck_bookings_source
          CHECK (source IN ('booking_page', 'merchant', 'marketplace')),

      -- The evidence snapshot (ZIF-5). NO CLIENT PERSONAL DATA: name, email, phone and locale stay
      -- in clients, reached by client_id, so ZIF-58 keeps exactly one tombstone per person.
      -- All three locales, copied verbatim from services.name: no locale decision is taken at
      -- booking time, and a later rename never rewrites history.
      service_name jsonb NOT NULL
        CONSTRAINT ck_bookings_service_name
          CHECK ({languages("service_name")} AND service_name <> '{{}}'::jsonb),
      price_amount_minor integer NOT NULL
        CONSTRAINT ck_bookings_price_amount_minor
          CHECK (price_amount_minor BETWEEN 0 AND 1000000),
      price_currency text NOT NULL,
      duration_minutes integer NOT NULL
        CONSTRAINT ck_bookings_duration_minutes CHECK (duration_minutes BETWEEN 5 AND 720),
      -- The text the client was shown, never a settings key: ZIF-55 interprets THIS column, because
      -- a key would be re-read at cancellation time and ZIF-5 rejects that outright.
      --
      -- TEXT ONLY, and deliberately no cancellation_policy_version beside it (spec C6). A version
      -- number is only ever useful for RE-READING a policy at cancellation time, which is exactly
      -- what ZIF-5 rejects as the thing that destroys the dispute evidence. The consent wording
      -- version is a different key for a different purpose and lives on booking_events; the two
      -- must never be unified.
      cancellation_policy_text text
        CONSTRAINT ck_bookings_cancellation_policy_text_length
          CHECK (char_length(cancellation_policy_text) <= 2000),
      auto_confirm_at_booking boolean NOT NULL,
      -- Nullable forever. Migration 0023 forbids NOT NULL, a default or uniqueness on
      -- memberships.display_name, and this is its snapshot, so the same three are forbidden here.
      -- Snapshotting it is also what lets booked() never join memberships
      -- (tests/test_availability_api.py:502-504).
      worker_display_name text
        CONSTRAINT ck_bookings_worker_display_name_length
          CHECK (char_length(worker_display_name) BETWEEN 1 AND 60),

      -- clock_timestamp, not now(): a transaction held open would otherwise backdate its rows
      -- (0010:89-90, 0024:94-96).
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),

      CONSTRAINT fk_bookings_tenant_id_client_id_clients
        FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id),
      CONSTRAINT fk_bookings_tenant_id_worker_id_memberships
        FOREIGN KEY (tenant_id, worker_id) REFERENCES memberships (tenant_id, id),
      CONSTRAINT fk_bookings_tenant_id_service_id_services
        FOREIGN KEY (tenant_id, service_id) REFERENCES services (tenant_id, id),
      -- The money pair, exactly as 0016:52-54: a business's currency, never the client's.
      CONSTRAINT fk_bookings_tenant_id_price_currency_tenants
        FOREIGN KEY (tenant_id, price_currency) REFERENCES tenants (id, currency)
    )""")

    # THE constraint. Half-open [) is tstzrange's default and exactly right: 10:00-11:00 and
    # 11:00-12:00 do not conflict. No stored range column - tstzrange(timestamptz, timestamptz) is
    # IMMUTABLE and so legal in an index expression, and the app never learns what a range type is.
    #
    # FOUR statuses, ZIF-5's enumeration. `no_show` is OUT, decided in this ticket and not later:
    #   * The deciding case: a client no-shows 10:00-11:00; a walk-in arrives at 10:15; the merchant
    #     records them 10:15-11:00 from the console (source='merchant', bypassing the availability
    #     engine). With no_show in the predicate that insert is a 23P01 and the merchant cannot
    #     record work that physically happened.
    #   * Accepted cost, recorded deliberately: marking a booking no_show retroactively frees its
    #     slot, so a backdated insert can overlap history. Availability is unaffected (a past
    #     slot is never offered); only the merchant's own console can produce the overlap, and it
    #     is the merchant's own record.
    #   * Why now and not later: there is no ADD CONSTRAINT ... USING INDEX for EXCLUDE constraints.
    #     Widening the predicate on live data therefore takes ACCESS EXCLUSIVE on bookings for a
    #     full gist build, with every booking write blocked for the duration.
    #
    # The TTL carve-out (expires_at > now()) CANNOT appear here: an index predicate must be
    # IMMUTABLE and now() is STABLE. It lives in booked() alone (app/availability.py).
    #
    # ############################################################################################
    # EVERY WRITER OF THIS TABLE MUST FIRST TAKE
    #     SELECT pg_advisory_xact_lock(51, hashtext(current_setting('app.tenant_id')))
    # as the FIRST statement of its transaction. That includes ZIF-50/ZIF-52's console path and any
    # future importer or migration that inserts bookings.
    #
    # This is not tidiness. An EXCLUDE constraint has no speculative-insertion path: each inserter
    # writes its heap tuple and THEN scans, so two concurrent inserters of an identical interval can
    # each find the other's tuple and both enter XactLockTableWait. Measured on real Postgres, 80
    # rounds of two threads: {'ok': 80, '23P01': 73, '40P01': 7} - about 9% of losers DEADLOCK
    # rather than conflict. A 40P01 arrives as OperationalError, not IntegrityError, and aborts the
    # whole transaction, so ROLLBACK TO SAVEPOINT cannot recover it.
    #
    # The constraint stays anyway: it is the control that holds when a writer forgets the lock.
    # ############################################################################################
    op.execute(f"""
    ALTER TABLE bookings ADD CONSTRAINT {OVERLAP} EXCLUDE USING gist (
      tenant_id WITH =, worker_id WITH =, tstzrange(starts_at, ends_at) WITH &&)
      WHERE (status IN ({quoted(OCCUPYING)}))
    """)
    # booked()'s window read: one worker's bookings by start. Also the pending count's leading
    # columns are covered by row-level security's tenant predicate plus a scan of a handful of
    # rows; no second index until a business has thousands of live pendings.
    op.execute("""
    CREATE INDEX ix_bookings_tenant_id_worker_id_starts_at
      ON bookings (tenant_id, worker_id, starts_at)""")
    enable_tenant_isolation("bookings")
    # 0001's default privileges granted DELETE; a booking is evidence and is never deleted.
    op.execute("REVOKE DELETE ON bookings FROM ziftbook_app")

    op.execute("""
    CREATE TABLE booking_events (
      id uuid CONSTRAINT pk_booking_events PRIMARY KEY DEFAULT uuidv7(),
      tenant_id uuid NOT NULL
        CONSTRAINT fk_booking_events_tenant_id_tenants REFERENCES tenants (id),
      booking_id uuid NOT NULL,
      -- No CHECK on the value set, deliberately, and the reason is the lock and nothing else:
      -- ZIF-53, ZIF-55 and ZIF-7 each add an event, and a value list here would make every one of
      -- them an ALTER TABLE ... ADD CONSTRAINT under ACCESS EXCLUSIVE. The value set is held at the
      -- single write site instead - app/bookings.py's INSERT_EVENT, which hardcodes 'created' in
      -- the SQL. (An earlier version of this comment cited an `app.bookings.Event` Literal as the
      -- protection. It was a one-value Literal referenced nowhere, it protected nothing, and it has
      -- been deleted; ZIF-53 adds the parameter and a real value set together.) Length only.
      event text NOT NULL
        CONSTRAINT ck_booking_events_event_length CHECK (char_length(event) BETWEEN 1 AND 40),
      -- From auth.origin(request), the same source an audit event's comes from. On a
      -- source='merchant' event these are the employee's personal data (ZIF-109), so a subject
      -- access request for the CLIENT must withhold them.
      ip inet,
      user_agent text
        CONSTRAINT ck_booking_events_user_agent_length CHECK (char_length(user_agent) <= 512),
      -- The ZIF-109 consent seam. The booking page's marketing GRANTS are not written to consents
      -- at booking time; the version and the purposes the booker ticked ride here, on the
      -- append-only creation row, and ZIF-53's confirmation CLICK calls clients.record_consents
      -- with them. Withdrawals go straight to consents at booking time. See docs/specs ZIF-51 SS8.
      --
      -- This is the CONSENT WORDING version - a key into app.clients.CONSENT_TEXTS, resolved by
      -- texts_for(). It is NOT a cancellation-policy version: bookings snapshots the cancellation
      -- TEXT and carries no version at all (spec C6). Two different keys, two different purposes;
      -- do not unify them.
      policy_version text
        CONSTRAINT ck_booking_events_policy_version_length
          CHECK (char_length(policy_version) BETWEEN 1 AND 40),
      consent_purposes jsonb
        CONSTRAINT ck_booking_events_consent_purposes
          CHECK (jsonb_typeof(consent_purposes) = 'object'),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      CONSTRAINT fk_booking_events_tenant_id_booking_id_bookings
        FOREIGN KEY (tenant_id, booking_id) REFERENCES bookings (tenant_id, id)
    )""")
    op.execute("""
    CREATE INDEX ix_booking_events_tenant_id_booking_id_id
      ON booking_events (tenant_id, booking_id, id)""")
    enable_tenant_isolation("booking_events")
    # The REVOKE runs before the GRANTs, or it would wipe them (CONTRIBUTING.md:97-98).
    op.execute("REVOKE ALL ON booking_events FROM ziftbook_app")
    op.execute("GRANT SELECT ON booking_events TO ziftbook_app")
    op.execute(f"GRANT INSERT ({EVENT_COLUMNS}) ON booking_events TO ziftbook_app")


def downgrade() -> None:
    op.execute("DROP TABLE booking_events")  # first: its foreign key points at bookings
    op.execute("DROP TABLE bookings")
