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

from app import bookings, jobs, tracing
from app.db import SessionLocal, tenant_context
from app.jobs import MAX_ATTEMPTS, Job
from app.mail import HOLD_TEMPLATES

# The core's purge date: when it erased the client plus 30 days (0064's INSERT).
HOLD_LIFETIME = timedelta(days=30)

# The backstop job runs within a poll of the purge date and the sweep within five minutes of it: a
# hold found this long past it means the worker was down or the purge kept failing, and the 31-day
# bound (0064's CHECK) was at risk.
LATE = timedelta(hours=1)

# Nothing is left to tell when no money is owed an answer (client_money_owed, the rule the erase
# core wrote the hold on), no refund settled in the last 7 days (one can still fail at the bank
# days later), and none of the client's emails through the hold is still to be sent (an email
# that gave up after its last attempt is out).
PURGE = text(f"""
DELETE FROM client_erasure_holds h
WHERE h.tenant_id = current_setting('app.tenant_id')::uuid
  AND (h.purge_after <= now()
       OR NOT (client_money_owed(h.tenant_id, h.client_id)
               OR EXISTS (SELECT 1 FROM booking_payments p
                          JOIN bookings b ON b.tenant_id = p.tenant_id AND b.id = p.booking_id
                          WHERE p.tenant_id = h.tenant_id AND b.client_id = h.client_id
                            AND p.refunded_minor > 0
                            AND p.settled_at > now() - interval '7 days')
               OR EXISTS (SELECT 1 FROM jobs j
                          JOIN bookings b ON b.id::text = j.payload ->> 'booking_id'
                          WHERE j.tenant_id = h.tenant_id AND b.tenant_id = h.tenant_id
                            AND b.client_id = h.client_id
                            AND j.kind = 'email.booking' AND j.completed_at IS NULL
                            AND j.attempts < {MAX_ATTEMPTS}
                            AND j.payload ->> 'template' IN
                              ({", ".join(f"'{t}'" for t in sorted(HOLD_TEMPLATES))}))))
RETURNING h.client_id, h.purge_after < now() - CAST(:late AS interval) AS late
""")

logger = logging.getLogger(__name__)


def purge(db: Session) -> int:
    """Delete this business's holds that are no longer needed or past their date, in the caller's
    tenant transaction after the booking lock (every writer of a refund or an erasure holds it, so
    none changes what the purge decides on while it runs). Returns how many."""
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
    purged = 0
    with tracing.span("erasure_holds.sweep", SpanKind.INTERNAL, {"tenants": len(tenant_ids)}):
        for tenant_id in tenant_ids:
            with tenant_context(tenant_id) as db:
                db.execute(bookings.LOCK, {"key": bookings.LOCK_KEY})  # first, as every writer
                purged += purge(db)
    return purged


def enqueue_purge(db: Session, tenant_id: UUID, client_id: UUID, erased_at: datetime) -> None:
    """The backstop job, due at the hold's purge date, for a caller of the erase core: in its
    transaction, with the time the core returned. The caller cannot read the hold (the API role
    may not), so it is enqueued whether or not one was written; with none, it purges nothing. The
    key carries the date: a re-applied erasure after a restore writes a new hold with a new one."""
    due_at = erased_at + HOLD_LIFETIME
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
