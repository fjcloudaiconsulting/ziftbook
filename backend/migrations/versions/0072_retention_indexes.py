"""Indexes for the worker's retention sweep (ZIF-235).

The sweep deletes audit events past their retention (failed sign-ins after 30 days, the rest after
a year, on both audit tables) and clears the address and browser of old booking events, business
by business. Each of its statements is a range scan on one of these:

- audit_events (created_at), and (created_at) WHERE action = 'sign_in_failed': the existing BRIN
  index serves incident time windows, but deletes and page reuse wear its correlation away;
- booking_events (tenant_id, created_at) WHERE ip or user_agent is set: a cleared row leaves it;
- platform_audit_events (created_at), and (created_at) for the three failure actions.

The first autocommit_block in this repository: CREATE INDEX CONCURRENTLY cannot run in a
transaction (CONTRIBUTING, "Database migrations"). With transaction_per_migration, Alembic commits
0072's own (empty) per-migration transaction before the block, runs the block in autocommit, and
stamps 0072 only after it. A second migration run waiting its turn holds no snapshot (env.py polls
its lock), so it never deadlocks with these builds.
A run that fails part-way leaves the indexes built so far valid, any half-built one invalid, and
the version at 0056; the re-run drops each index first (DROP INDEX CONCURRENTLY IF EXISTS, so an
invalid leftover never survives) and builds it again. lock_timeout is 0 while building, so the
wait for older transactions blocks no app query, and is back to the 5 seconds of migrations/env.py
after the block.

The downgrade drops the five indexes, also concurrently. Nothing else changes.

Revision ID: 0072
Revises: 0056
"""

from alembic import op

revision = "0072"
down_revision = "0056"
branch_labels = None
depends_on = None

# The failure actions of platform_audit_events kept 30 days, as the audit_events rule (owner ruling
# D3, ZIF-235); app.retention.PLATFORM_FAILURES holds the same list, and a test compares them.
PLATFORM_FAILURES = "'admin_sign_in_failed', 'second_factor_failed', 'totp_enrolment_failed'"

INDEXES = {
    "ix_audit_events_created_at": "audit_events (created_at)",
    "ix_audit_events_sign_in_failed_created_at": (
        "audit_events (created_at) WHERE action = 'sign_in_failed'"
    ),
    "ix_booking_events_tenant_id_created_at_with_origin": (
        "booking_events (tenant_id, created_at) WHERE ip IS NOT NULL OR user_agent IS NOT NULL"
    ),
    "ix_platform_audit_events_created_at": "platform_audit_events (created_at)",
    "ix_platform_audit_events_failed_created_at": (
        f"platform_audit_events (created_at) WHERE action IN ({PLATFORM_FAILURES})"
    ),
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, definition in INDEXES.items():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute("SET lock_timeout = 0")
            op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {definition}")
            op.execute("SET lock_timeout = '5s'")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
