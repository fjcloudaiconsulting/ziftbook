"""contact_email_senders: which businesses mailed one inbox a contact email link today (ZIF-232).

At most 3 businesses a day may mail one inbox, so a ring of them cannot flood it. The count is of
rows, one per inbox and business, never inferred from a counter a concurrent request could give
back: a business that already mailed the inbox today is in the set and counts once, whatever its
requests do. A claim (claimed_at) holds a place for a few minutes while its request runs; a link
queued from it (linked_at, set in that request's transaction) keeps the place for a day.

address_key is limits.email_key of the inbox (peppered, never the address). Global on purpose, like
rate_limits: the count spans businesses, so there is no row-level security, and a business id is
all it holds. Rows older than a day are purged as claims go.

A fourth migration in ZIF-232's range: rate_limits keys cannot be counted by inbox, as the
database collation keeps a prefix match off the key's index.

Revision ID: 0094
Revises: 0088
"""

from alembic import op

revision = "0094"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE contact_email_senders (
      address_key text NOT NULL,
      tenant_id uuid NOT NULL CONSTRAINT fk_contact_email_senders_tenant_id_tenants
        REFERENCES tenants (id) ON DELETE CASCADE,
      claimed_at timestamptz NOT NULL,
      linked_at timestamptz,
      CONSTRAINT pk_contact_email_senders PRIMARY KEY (address_key, tenant_id)
    )""")
    op.execute(
        "CREATE INDEX ix_contact_email_senders_claimed_at ON contact_email_senders (claimed_at)"
    )
    # 0001's default privileges give the app role every privilege; it needs only these four.
    op.execute("REVOKE ALL ON contact_email_senders FROM ziftbook_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON contact_email_senders TO ziftbook_app")


def downgrade() -> None:
    op.execute("DROP TABLE contact_email_senders")
