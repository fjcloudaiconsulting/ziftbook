"""platform_admins and platform_audit_events: who runs the platform, and what they did (ZIF-70).

platform_admins: the app role has no grant at all, so no app-role bug or injection can make an
admin. Rows come from the CLI (python -m app.admin, as ziftbook_migrate) and, later, first-run
setup's definer function.

platform_audit_events: global, not per business. The app role may only append, naming its
columns: never id or created_at (no backdating), never read, change or delete. A write needs a
non-blank reason. Operators read it as ziftbook_migrate. No foreign keys: rows outlive users and
businesses. No RLS: the app cannot read it at all.

The downgrade DROPS every platform admin grant and the whole platform audit history.

Revision ID: 0036
Revises: 0035
"""

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE platform_admins (
      user_id uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
      granted_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""")
    # Default privileges from 0001 gave the app role DML on every new table; none comes back.
    op.execute("REVOKE ALL ON platform_admins FROM ziftbook_app")

    op.execute("""
    CREATE TABLE platform_audit_events (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      -- clock_timestamp, not now(): a transaction held open would otherwise backdate its events.
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      actor_user_id uuid,  -- NULL only for CLI rows (details.via = 'cli')
      action text NOT NULL,
      kind text NOT NULL CHECK (kind IN ('auth', 'read', 'write')),
      tenant_id uuid,  -- the business acted on; NULL for platform-level events
      target text,
      reason text CHECK (char_length(reason) <= 500),
      details jsonb,
      ip inet,
      user_agent text CHECK (char_length(user_agent) <= 512),
      -- A regex, not btrim(): btrim strips only spaces, so a tab/newline-only reason would pass.
      CONSTRAINT reason_on_write CHECK (kind <> 'write' OR coalesce(reason, '') ~ '[^[:space:]]')
    )""")
    op.execute("CREATE INDEX ON platform_audit_events USING brin (created_at)")
    op.execute("CREATE INDEX ON platform_audit_events (tenant_id, id)")
    op.execute("REVOKE ALL ON platform_audit_events FROM ziftbook_app")
    op.execute(
        "GRANT INSERT (actor_user_id, action, kind, tenant_id, target, reason, details, ip, "
        "user_agent) ON platform_audit_events TO ziftbook_app"
    )


def downgrade() -> None:
    op.execute("DROP TABLE platform_audit_events")
    op.execute("DROP TABLE platform_admins")
