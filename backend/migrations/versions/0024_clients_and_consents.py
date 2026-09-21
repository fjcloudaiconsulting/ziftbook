"""clients and consents: a business's own client records, and an append-only consent log.

A business keeps its own copy of a client's name and contact details, refreshed on each booking
(ZIF-99): never a live view of the platform account, and never a join to users. clients.user_id
points at that account when a signed-in person booked (ZIF-51 sets it, from their own session),
with no foreign key on purpose - see the comment on the column.

consents is append-only: one row per grant or withdrawal per purpose, with the exact text the
person was shown. The app role may SELECT, and INSERT a fixed column list; never UPDATE or DELETE,
and never set id or created_at itself.

Erasure is ZIF-58: it adds clients.erased_at as its own expand migration. Nothing here reads it.

Revision ID: 0024
Revises: 0023
"""

from alembic import op

from app.db import enable_tenant_isolation

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

# id and created_at are left out on purpose: with no column grant the app cannot backdate a row,
# which is what makes the log append-only (audit_events does the same, 0010:120-123).
# A column added to consents later needs its own GRANT INSERT (column) in its migration, or every
# consent write fails - not only writes of the new column. CONTRIBUTING.md documents the same trap
# for audit_events.
CONSENT_COLUMNS = (
    "tenant_id, client_id, purpose, granted, text_shown, policy_version, source, ip, user_agent"
)


def upgrade() -> None:
    op.execute("""
    CREATE TABLE clients (
      id uuid CONSTRAINT pk_clients PRIMARY KEY DEFAULT uuidv7(),
      tenant_id uuid NOT NULL
        CONSTRAINT fk_clients_tenant_id_tenants REFERENCES tenants (id),
      -- No foreign key and no index, on purpose. A foreign key check bypasses row-level security,
      -- so a constraint here would turn this column into an existence oracle over the global users
      -- table; nothing ever joins clients to users (ZIF-99); and a pointer left dangling by a
      -- platform erasure is the wanted behaviour, not a bug (audit_events, 0010:91). Written only
      -- by ZIF-51, from the booker's own session, never from a request body: in the find-or-create
      -- INSERT column list, and never in its DO UPDATE SET list - see app.clients.find_or_create.
      user_id uuid,
      name text NOT NULL
        CONSTRAINT ck_clients_name_length CHECK (char_length(name) BETWEEN 1 AND 100),
      -- Normalised in Python (app.passwords.normalise_email), never by a lower() index: Python's
      -- str.lower() and Postgres lower() disagree under some collations. NULL for a walk-in.
      email text
        CONSTRAINT ck_clients_email_lowercase CHECK (email = lower(email)),
      phone text
        CONSTRAINT ck_clients_phone_length CHECK (char_length(phone) BETWEEN 1 AND 40),
      -- The language this business writes to this client in; IN (...) rather than a length check,
      -- as on users.locale (0005:27).
      locale text CONSTRAINT ck_clients_locale CHECK (locale IN ('en', 'nl', 'pt')),
      -- client_note is what the client told the business; internal_note is the business's own and
      -- never appears in a client-facing schema (tests/test_openapi.py holds that fence).
      client_note text
        CONSTRAINT ck_clients_client_note_length
          CHECK (char_length(client_note) BETWEEN 1 AND 2000),
      internal_note text
        CONSTRAINT ck_clients_internal_note_length
          CHECK (char_length(internal_note) BETWEEN 1 AND 2000),
      -- Plain: not partial, not functional. NULLs are distinct, so any number of walk-ins with no
      -- email coexist, and the find-or-create upsert can name (tenant_id, email) as its conflict
      -- target. Do not make this partial or functional later: ON CONFLICT (tenant_id, email)
      -- infers a partial index only when the statement repeats its predicate, and a functional one
      -- only when it repeats the expression, so either change turns every booking into
      -- "there is no unique or exclusion constraint matching the ON CONFLICT specification".
      CONSTRAINT uq_clients_tenant_id_email UNIQUE (tenant_id, email)
    )""")
    enable_tenant_isolation("clients")
    # 0001's default privileges granted DELETE; a client is erased by ZIF-58, never by the app.
    op.execute("REVOKE DELETE ON clients FROM ziftbook_app")

    op.execute("""
    CREATE TABLE consents (
      id uuid CONSTRAINT pk_consents PRIMARY KEY DEFAULT uuidv7(),
      tenant_id uuid NOT NULL
        CONSTRAINT fk_consents_tenant_id_tenants REFERENCES tenants (id),
      client_id uuid NOT NULL,
      purpose text NOT NULL
        CONSTRAINT ck_consents_purpose
          CHECK (purpose IN ('marketing_email', 'sms', 'whatsapp')),
      -- One row per grant or withdrawal: false is a withdrawal. Deliberately not ZIF-4's
      -- granted_at/withdrawn_at pair, which invites the UPDATE that append-only forbids.
      granted boolean NOT NULL,
      -- Server-owned (app.clients.CONSENT_TEXTS): the exact wording shown, and the policy version
      -- that wording belongs to. A client-supplied text would make the Art. 7(1) evidence
      -- attacker-controlled.
      text_shown text NOT NULL
        CONSTRAINT ck_consents_text_shown_length
          CHECK (char_length(text_shown) BETWEEN 1 AND 2000),
      policy_version text NOT NULL
        CONSTRAINT ck_consents_policy_version_length
          CHECK (char_length(policy_version) BETWEEN 1 AND 40),
      source text NOT NULL
        CONSTRAINT ck_consents_source
          CHECK (source IN ('booking_page', 'merchant', 'import')),
      ip inet,
      user_agent text
        CONSTRAINT ck_consents_user_agent_length CHECK (char_length(user_agent) <= 512),
      -- clock_timestamp, not now(): a transaction held open would otherwise backdate its rows
      -- (0010:89-90).
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      -- Composite: a plain foreign key would be checked with row-level security bypassed and could
      -- point at another business's client.
      CONSTRAINT fk_consents_tenant_id_client_id_clients
        FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id)
    )""")
    # referenced=True (the default): the flag is about a table having an id, which consents does
    # (see app.db.enable_tenant_isolation). Nothing references consents today, so its
    # uq_consents_tenant_id_id is one spare index - cheaper than a variant call whose reason does
    # not apply here.
    enable_tenant_isolation("consents")
    # The current state per purpose: DISTINCT ON (client_id, purpose) ... ORDER BY ..., id DESC,
    # under app.clients.CURRENT_CONSENTS' tenant predicate.
    op.execute("""
    CREATE INDEX ix_consents_tenant_id_client_id_purpose_id
      ON consents (tenant_id, client_id, purpose, id DESC)""")
    # Append-only by privilege, not by a trigger: no UPDATE, no DELETE, and an INSERT that cannot
    # name id or created_at. The REVOKE runs before the GRANTs, or it would wipe them.
    op.execute("REVOKE ALL ON consents FROM ziftbook_app")
    op.execute("GRANT SELECT ON consents TO ziftbook_app")
    op.execute(f"GRANT INSERT ({CONSENT_COLUMNS}) ON consents TO ziftbook_app")


def downgrade() -> None:
    op.execute("DROP TABLE consents")  # first: its foreign key points at clients
    op.execute("DROP TABLE clients")
