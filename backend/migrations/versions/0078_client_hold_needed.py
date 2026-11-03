"""One rule for when an erased client's contact hold is needed (ZIF-58).

client_hold_needed(p_tenant, p_client) is true while the client may still have to be told
something through a hold (0064): money is still owed an answer on one of its bookings
(client_money_owed), a refund settled in the last 7 days (one can still fail at the bank days
later), or one of its emails that go through a hold is still to be sent. An email has given up only
once its last lease has run out: CLAIM counts an attempt when it starts.

Both sides of the hold use it, so they cannot drift: erase_client_core writes a hold when it is
true (besides the cancellations this erasure announces), and the worker's purge keeps a hold while
it is true. Before this, the core wrote one on money owed alone, so a client erased days after a
refund settled had no channel if that refund then failed, and an email queued just before the
erasure found no hold.

The core changes in its stored definition only (pg_get_functiondef): two anchored fragments, the
hold condition and a comment above the hold's INSERT, each found exactly once or the migration
refuses, so another migration's edit to the core (0058's booked_as) stays, and the downgrade
reverses exactly those two on the stored definition (CONTRIBUTING, "Database migrations"). CREATE
OR REPLACE keeps the core's owner, grants, SECURITY DEFINER and search_path. client_hold_needed is
SECURITY INVOKER: the core runs it as its owner; the worker, which is granted it, with its own
rights under row-level security. The two lists below are the code's (app.mail.HOLD_TEMPLATES,
app.jobs.MAX_ATTEMPTS), written out because a migration must not change when the code does;
tests/test_erasure_holds.py runs the code's against them.

Revision ID: 0078
Revises: 0077
"""

import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
HOLD_TEMPLATES = "'booking_cancelled', 'booking_expired', 'booking_refund_failed'"
MAX_ATTEMPTS = 5

# shortcut: the jobs arm seq-scans jobs (global, every kind, 30 days of finished jobs: no index
# fits an unfinished email.booking job by its booking), once per hold per sweep and once per erasure
# under that business's booking lock; when jobs passes ~100k rows or this shows in slow logs, add in
# its own migration CREATE INDEX CONCURRENTLY ON jobs ((payload ->> 'booking_id')) WHERE
# kind = 'email.booking' AND completed_at IS NULL.
CLIENT_HOLD_NEEDED = f"""
CREATE FUNCTION client_hold_needed(p_tenant uuid, p_client uuid) RETURNS boolean
  LANGUAGE sql STABLE SECURITY INVOKER {SEARCH_PATH} AS $$
  SELECT client_money_owed(p_tenant, p_client)
      OR EXISTS (SELECT 1 FROM booking_payments p
                 JOIN bookings b ON b.tenant_id = p.tenant_id AND b.id = p.booking_id
                 WHERE p.tenant_id = p_tenant AND b.tenant_id = p_tenant AND b.client_id = p_client
                   AND p.refunded_minor > 0 AND p.settled_at > now() - interval '7 days')
      OR EXISTS (SELECT 1 FROM jobs j
                 JOIN bookings b ON b.id::text = j.payload ->> 'booking_id'
                 WHERE j.tenant_id = p_tenant AND b.tenant_id = p_tenant AND b.client_id = p_client
                   AND j.kind = 'email.booking' AND j.completed_at IS NULL
                   AND (j.attempts < {MAX_ATTEMPTS} OR j.next_attempt_at > now())
                   AND j.payload ->> 'template' IN ({HOLD_TEMPLATES}))
$$"""


# 0078's anchored fragments in erase_client_core, as (before, after).
CORE_EDITS = (
    ("OR client_money_owed(v_tenant, p_client))", "OR client_hold_needed(v_tenant, p_client))"),
    (
        "  INSERT INTO client_erasure_holds",
        "  -- 0078: or whenever client_hold_needed says the client may still have to be told.\n"
        "  INSERT INTO client_erasure_holds",
    ),
)
CORE_DEFINITION = sa.text(
    "SELECT pg_get_functiondef(to_regprocedure('erase_client_core(uuid,boolean)'))"
)


def edit_core(edits: tuple[tuple[str, str], ...]) -> None:
    """Applies each (old, new) to the stored erase_client_core; refuses unless old is there once."""
    bind = op.get_bind()
    definition = bind.scalar(CORE_DEFINITION)
    if definition is None:
        raise RuntimeError("0078: erase_client_core is missing")
    for old, new in edits:
        if definition.count(old) != 1:
            raise RuntimeError(f"0078: expected {old!r} once in erase_client_core")
        definition = definition.replace(old, new)
    # SQL read back from the catalog, run as it is: no bind-parameter parsing of its text.
    bind.exec_driver_sql(definition)


def upgrade() -> None:
    op.execute(CLIENT_HOLD_NEEDED)
    op.execute("REVOKE EXECUTE ON FUNCTION client_hold_needed(uuid, uuid) FROM PUBLIC")
    # The worker's purge calls it (and, through it, client_money_owed, which 0077 granted).
    op.execute("GRANT EXECUTE ON FUNCTION client_hold_needed(uuid, uuid) TO ziftbook_worker")
    edit_core(CORE_EDITS)


def downgrade() -> None:
    edit_core(tuple((new, old) for old, new in reversed(CORE_EDITS)))
    op.execute("DROP FUNCTION client_hold_needed(uuid, uuid)")
