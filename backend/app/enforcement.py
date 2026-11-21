"""The people limit's clock (ZIF-78): a business with more live members than its plan's places is
stamped over the limit, and cleared once back within them. The grace period runs from the stamp
(entitlements.GRACE). Runs as a job, as the worker: only it may write over_limit_since (0103).
"""

import uuid
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import jobs
from app.db import tenant_context
from app.entitlements import LIVE_MEMBERS, entitlements

# Stamps only an unstamped business and clears only a stamped one, so a repeat run keeps the
# first stamp: the 30 days run from when the business first went over.
STAMP = text("""
UPDATE tenant_billing SET over_limit_since = CASE WHEN :over THEN now() END
WHERE (over_limit_since IS NULL) = :over
""")


def enqueue(db: Session, tenant_id: UUID) -> None:
    """Check the business's places after this transaction's change to its plan or members."""
    # A unique tail: a finished job keeps its dedupe key for 30 days, and every later change needs
    # its own check. Repeats are harmless; the handler reads the current state.
    jobs.enqueue(
        db,
        "billing.enforce",
        f"billing.enforce:{tenant_id}:{uuid.uuid4().hex}",
        {},
        tenant_id=tenant_id,
    )


def run(job: jobs.Job) -> None:
    assert job.tenant_id is not None
    with tenant_context(job.tenant_id) as db:
        # Live members only, not pending invites: an invite never starts the grace clock (spec).
        over = db.scalar(LIVE_MEMBERS) > entitlements(db).count("places")
        db.execute(STAMP, {"over": over})
