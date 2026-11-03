"""Addresses saved before confirmation existed wait for it (ZIF-232, the contract step).

Until 0087, an owner's PUT saved contact_email straight into settings, so a saved address was
never confirmed and was used at once (the mailer's Reply-To). This moves every saved one out of
settings into contact_email_requests, as a typed address waiting for its inbox to confirm it with
no link due (expires_at NULL, so Settings shows it as not confirmed). From this commit on, no
release, old or new, reads an address nobody confirmed: the readers read settings only, and
0087's guard, committed first (transaction_per_migration), keeps any older instance from saving a
new one meanwhile.

Only a JSON string of at most 254 characters moves; any other value, written around the API, is
deleted without moving (it was no usable address either way), so a value the table's CHECKs
refuse cannot stop the deploy. A business that already has a request keeps it.

No audit row: audit_events takes inserts from ziftbook_app only (0010), and widening that for an
append-only evidence table is not worth it. The owner's earlier setting_changed row already holds
the address, and Settings shows it as not confirmed from the next release.

Runs as one statement per business, under that business's app.tenant_id (forced row-level
security binds the migrate role too).

downgrade() does nothing, on purpose: putting the addresses back would use them unconfirmed
again. Downgrading 0086 then drops the only copy of them.

Revision ID: 0088
Revises: 0087
"""

from alembic import op
from sqlalchemy import text

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None

MOVE = text("""
WITH moved AS (DELETE FROM settings WHERE key = 'contact_email' RETURNING value)
INSERT INTO contact_email_requests (tenant_id, email)
SELECT current_setting('app.tenant_id')::uuid, lower(value #>> '{}') FROM moved
WHERE jsonb_typeof(value) = 'string' AND char_length(value #>> '{}') <= 254
ON CONFLICT (tenant_id) DO NOTHING
""")


def upgrade() -> None:
    conn = op.get_bind()
    for tenant_id in conn.scalars(text("SELECT id FROM tenants ORDER BY id")).all():
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(MOVE)


def downgrade() -> None:
    pass
