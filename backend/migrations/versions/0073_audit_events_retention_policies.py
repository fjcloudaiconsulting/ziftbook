"""Audit events past their retention can be deleted by the worker's sweep only (ZIF-235).

audit_events has FORCE row-level security and no DELETE policy, so not even its owner deletes an
event. Two policies, TO ziftbook_migrate, open the events past their retention while
app.retention_sweep is on: failed sign-ins after 30 days, every other event after a year
(CONTRIBUTING, "Audit log"; ZIF-58 decision 6). The SELECT one lets the DELETE find them. The
worker reaches them only through sweep_audit_events (0074), a SECURITY DEFINER function owned by
ziftbook_migrate that sets and restores the flag.

The cutoff is in the policies, not only in the function: an event inside its window stays out of
reach of any migrate session, flag or not, whatever a function body does. The app role can set the
flag too, but these policies are TO ziftbook_migrate: it gains nothing directly, and a
migrate-owned definer that reads audit_events must clear the flag (CONTRIBUTING; a test lists
them). Only audit_events: migrate has no tenant path there (events of no business). Booking events
are cleared under tenant_isolation instead, so no policy widens what anyone reads across
businesses.

CREATE POLICY takes ACCESS EXCLUSIVE on audit_events for the migration's moment, bounded by the
5-second lock_timeout; a run that times out changes nothing and is run again.

The downgrade drops both policies.

Revision ID: 0073
Revises: 0072
"""

from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None

# Copied, not imported (migrations import nothing from the app): app.retention states the same
# periods, and tests pin both.
EXPIRED = (
    "current_setting('app.retention_sweep', true) = 'on'"
    " AND (created_at < now() - interval '1 year'"
    " OR (action = 'sign_in_failed' AND created_at < now() - interval '30 days'))"
)


def upgrade() -> None:
    op.execute(
        f"CREATE POLICY retention_sweep_read ON audit_events FOR SELECT TO ziftbook_migrate"
        f" USING ({EXPIRED})"
    )
    op.execute(
        f"CREATE POLICY retention_sweep ON audit_events FOR DELETE TO ziftbook_migrate"
        f" USING ({EXPIRED})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY retention_sweep ON audit_events")
    op.execute("DROP POLICY retention_sweep_read ON audit_events")
