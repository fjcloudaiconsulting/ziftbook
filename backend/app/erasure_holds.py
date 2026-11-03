"""The purge of an erased client's contact hold (ZIF-58), the worker's alone.

client_erasure_holds (0064) keeps an erased client's email and locale so the client still hears of
a cancellation or a refund (mail.HOLD_TEMPLATES). The worker deletes a hold as soon as nothing is
left to tell through it, and at its purge date whatever is left: inside the one-month window an
erasure request is answered in.
"""

import logging
from datetime import datetime, timedelta
from uuid import UUID

from opentelemetry.trace import SpanKind
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import bookings, jobs, logs, tracing
from app.db import SessionLocal, tenant_context
from app.jobs import Job

# The core's purge date, computed as the core computes it (0064's INSERT: now() + 30 days, in the
# session's time zone), so a DST change in between cannot move the job an hour off the hold.
DUE = text("SELECT CAST(:at AS timestamptz) + interval '30 days'")

# The backstop job runs within a poll of the purge date and the sweep within five minutes of it: a
# hold found this long past it means the worker was down or the purge kept failing, and the 31-day
# bound (0064's CHECK) was at risk.
LATE = timedelta(hours=1)

# A hold goes at its purge date, or once client_hold_needed (0078, the rule the erase core writes
# it on) says nothing is left to tell: no money owed, no refund settled in the last 7 days, no
# email through the hold still to be sent.
PURGE = text("""
DELETE FROM client_erasure_holds h
WHERE h.tenant_id = current_setting('app.tenant_id')::uuid
  AND (h.purge_after <= now() OR NOT client_hold_needed(h.tenant_id, h.client_id))
RETURNING h.client_id, h.purge_after < now() - CAST(:late AS interval) AS late
""")

ANY_HOLD = text("SELECT EXISTS (SELECT 1 FROM client_erasure_holds)")

logger = logging.getLogger(__name__)


def purge(db: Session) -> int:
    """Delete this business's holds that are no longer needed or past their date, in the caller's
    tenant transaction after the booking lock. Every writer that can make a hold needed again (an
    erasure, a refund and its failure, each of which enqueues the client's email) holds that lock,
    so none can between the purge's look and its delete. Returns how many."""
    purged = db.execute(PURGE, {"late": LATE}).all()
    for row in purged:
        if row.late:
            logger.error("contact hold purged late", extra={"client_id": str(row.client_id)})
    if purged:
        logger.info("contact holds purged", extra={"count": len(purged)})
    return len(purged)


def sweep() -> int:
    """Every business's holds, on the worker's sweep timer. Returns how many were purged."""
    # The ids in their own short session, closed before the loop, as bookings.sweep does.
    with SessionLocal() as session:
        tenant_ids = session.scalars(text("SELECT id FROM tenants")).all()
    purged = failed = 0
    with tracing.span("erasure_holds.sweep", SpanKind.INTERNAL, {"tenants": len(tenant_ids)}):
        for tenant_id in tenant_ids:
            # One business that fails never holds back the others' purges.
            try:
                with tenant_context(tenant_id) as db:
                    # Most businesses have none: skipped without the booking lock. A hold written
                    # after this look is new, so not due before the next sweep.
                    if not db.scalar(ANY_HOLD):
                        continue
                    db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})  # first, as every writer
                    purged += purge(db)
            except Exception:
                failed += 1
                with logs.bound(tenant_id=str(tenant_id)):
                    logger.exception("business sweep failed", extra={"sweep": "holds"})
    if failed:
        raise RuntimeError(f"the hold sweep failed for {failed} businesses")
    return purged


def enqueue_purge(db: Session, tenant_id: UUID, client_id: UUID, erased_at: datetime) -> None:
    """The backstop job, due at the hold's purge date, for a caller of the erase core: in its
    transaction, with the time the core returned. The caller cannot read the hold (the API role
    may not), so it is enqueued whether or not one was written; with none, it purges nothing. The
    key carries the date: a re-applied erasure after a restore writes a new hold with a new one."""
    due_at: datetime = db.scalar(DUE, {"at": erased_at})
    jobs.enqueue(
        db,
        "erasure.hold_purge",
        f"erasure.hold_purge:{tenant_id}:{client_id}:{due_at.isoformat()}",
        {"client_id": str(client_id)},
        tenant_id=tenant_id,
        due_at=due_at,
    )


def purge_job(job: Job) -> None:
    """The erasure.hold_purge job: the business's purge, run at a hold's purge date whatever the
    sweep is doing (a business that keeps failing holds back the ones after it in the sweep)."""
    if job.tenant_id is None:
        raise ValueError("erasure.hold_purge needs a tenant")
    with tenant_context(job.tenant_id) as db:
        db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})
        purge(db)
