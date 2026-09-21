"""clients and consents: a business's own copy of its clients' names and contact details, refreshed
on each booking (ZIF-99), and the log of the marketing consent given to that business.
consents is append-only by privilege, not by a trigger.

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
      -- Deliberately not ZIF-4's granted_at/withdrawn_at pair, which invites the UPDATE that
      -- append-only forbids.
      granted boolean NOT NULL,
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
    enable_tenant_isolation("consents")
    op.execute("""
    CREATE INDEX ix_consents_tenant_id_client_id_purpose_id
      ON consents (tenant_id, client_id, purpose, id DESC)""")
    # The REVOKE runs before the GRANTs, or it would wipe them.
    op.execute("REVOKE ALL ON consents FROM ziftbook_app")
    op.execute("GRANT SELECT ON consents TO ziftbook_app")
    op.execute(f"GRANT INSERT ({CONSENT_COLUMNS}) ON consents TO ziftbook_app")


def downgrade() -> None:
    op.execute("DROP TABLE consents")  # first: its foreign key points at clients
    op.execute("DROP TABLE clients")
