import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import logs, tracing
from app.db import SessionLocal

ENQUEUE = text("""
INSERT INTO jobs (kind, dedupe_key, tenant_id, payload, due_at, next_attempt_at)
VALUES (:kind, :dedupe_key, :tenant_id, CAST(:payload AS jsonb),
        coalesce(CAST(:due_at AS timestamptz), now()),
        coalesce(CAST(:due_at AS timestamptz), now()))
ON CONFLICT (dedupe_key) DO NOTHING
RETURNING id
""")


def enqueue(
    session: Session,
    kind: str,
    dedupe_key: str,
    payload: dict[str, Any],
    *,
    tenant_id: UUID | None = None,
    due_at: datetime | None = None,
) -> bool:
    """Add a job in the caller's transaction, unless a kept job has its dedupe_key (finished jobs
    are pruned after RETENTION_DAYS, see prune).

    Returns whether it was added. Build the key as "kind:tenant_id:natural id", adding the due time
    when the same thing can be rescheduled ("booking.reminder:<tenant>:<booking>:<starts_at>").
    The payload holds ids only.
    """
    payload_copy = dict(payload)
    # Writes traceparent only when the current span is valid; a no-op otherwise. Never baggage or
    # tracestate: jobs is global, and only W3C tracecontext belongs in it (R3).
    tracing.PROPAGATOR.inject(payload_copy)
    added = session.execute(
        ENQUEUE,
        {
            "kind": kind,
            "dedupe_key": dedupe_key,
            "tenant_id": tenant_id,
            "payload": json.dumps(payload_copy),
            "due_at": due_at,
        },
    ).first()
    return added is not None


@dataclass(frozen=True)
class Job:
    id: UUID
    kind: str
    tenant_id: UUID | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class JobKind:
    """How to run one kind of job.

    handler runs in a thread and opens tenant_context(job.tenant_id) itself when it touches tenant
    data. It may run more than once for the same job, so it must be safe to repeat. timeout
    (seconds) stays below the one-minute lease; grace must be longer than the retry backoff (about
    15 minutes), or retries turn into skips.
    """

    handler: Callable[[Job], None]
    timeout: float
    grace: timedelta


# Attempts per job. The ix_jobs_claimable index (migration 0003) repeats it as a literal.
MAX_ATTEMPTS = 5

# How long a finished job is kept, so a given-up job's last_error stays readable for a human.
RETENTION_DAYS = 30

# One statement claims a batch: SKIP LOCKED keeps concurrent workers apart while claiming, and the
# bumped next_attempt_at keeps them apart afterwards. It is both the lease and the backoff
# (1, 2, 4, 8, 16 minutes), so a crashed worker's jobs come back on their own. = ANY(ARRAY(...)),
# not IN (...): the planner may run an IN subquery with LIMIT more than once and claim extra rows.
# MAX_ATTEMPTS is a literal, not a parameter: a prepared statement's generic plan can only use the
# partial index when its condition is in the statement itself.
CLAIM = text(f"""
UPDATE jobs
SET attempts = attempts + 1,
    next_attempt_at = now() + interval '1 minute' * 2 ^ attempts
WHERE id = ANY(ARRAY(
    SELECT id FROM jobs
    WHERE completed_at IS NULL AND attempts < {MAX_ATTEMPTS}
      AND next_attempt_at <= now() AND kind = ANY(:kinds)
    ORDER BY next_attempt_at
    LIMIT 20
    FOR UPDATE SKIP LOCKED))
RETURNING id, kind, tenant_id, payload, attempts, now() - due_at AS overdue
""")

# The backlog the queue gauges report: claimable right now (due, not leased, not in backoff, not
# given up), on CLAIM's predicate so it is an index scan. Age runs from when a job last became
# claimable, so a job retrying normally does not look stuck.
QUEUE = text(f"""
SELECT kind, count(*) AS size, extract(epoch FROM now() - min(next_attempt_at)) AS oldest
FROM jobs
WHERE completed_at IS NULL AND attempts < {MAX_ATTEMPTS}
  AND next_attempt_at <= now() AND kind = ANY(:kinds)
GROUP BY kind
""")

# Finished jobs are deleted RETENTION_DAYS after they finish; email_outbox keeps the record of what
# was sent (no FK, 0004). A given-up job has no finished time: its last claim set next_attempt_at
# 16 minutes ahead (CLAIM), which is close enough, and still in the future while it runs.
# shortcut: one unbatched DELETE and a seq scan of jobs every sweep, and a prune that times out
# every poll also blocks bookings.sweep (the same withheld ping alerts); batch with LIMIT and add a
# partial index on coalesce(completed_at, next_attempt_at) if jobs passes ~1M rows or prune shows up
# in slow logs.
PRUNE = text(f"""
DELETE FROM jobs
WHERE completed_at < now() - interval '{RETENTION_DAYS} days'
   OR (completed_at IS NULL AND attempts >= {MAX_ATTEMPTS}
       AND next_attempt_at < now() - interval '{RETENTION_DAYS} days')
RETURNING id
""")

logger = logging.getLogger(__name__)


def prune() -> int:
    """Delete jobs finished (done, skipped or given up) more than RETENTION_DAYS ago, on the
    worker's sweep timer. Returns how many."""
    with tracing.span("jobs.prune", SpanKind.INTERNAL, {}) as span:
        with SessionLocal.begin() as session:
            # RETURNING, not rowcount: Session.execute gives a Result, which has none (mypy).
            deleted = len(session.execute(PRUNE).all())
        span.set_attribute("deleted", deleted)
    if deleted:
        logger.info("jobs pruned", extra={"count": deleted})
    return deleted


def queue(kinds: list[str]) -> dict[str, tuple[int, float]]:
    """Per kind with a backlog: its size and the age in seconds of its oldest waiting job."""
    with SessionLocal.begin() as session:
        rows = session.execute(QUEUE, {"kinds": kinds}).all()
    return {row.kind: (row.size, float(row.oldest)) for row in rows}


def _claim(kinds: list[str]) -> list[tuple[Job, int, timedelta]]:
    with SessionLocal.begin() as session:
        rows = session.execute(CLAIM, {"kinds": kinds}).all()
    return [
        (Job(row.id, row.kind, row.tenant_id, row.payload), row.attempts, row.overdue)
        for row in rows
    ]


def _record(job_id: UUID, statement: str, **params: Any) -> None:
    with SessionLocal.begin() as session:
        session.execute(text(statement), {"id": job_id, **params})


async def _run(kind: JobKind, job: Job, attempts: int, overdue: timedelta) -> None:
    context = {"job_id": str(job.id), "job_kind": job.kind}
    if job.tenant_id is not None:
        context["tenant_id"] = str(job.tenant_id)
    # attempts rides on the job events only, not on everything the handler logs.
    attempt = {"attempts": attempts}
    # job.kind is closed: CLAIM only returns the registered kinds.
    labels = {"job.kind": job.kind}
    # A non-string value (an older or odd row) makes extract raise TypeError, which would escape
    # gather and fail run_once's whole batch; isinstance keeps a malformed value as no context
    # instead, and the job still runs as a root span.
    tp = job.payload.get("traceparent")
    parent = tracing.PROPAGATOR.extract({"traceparent": tp}) if isinstance(tp, str) else None
    with (
        logs.bound(**context),
        tracing.span(
            f"job {job.kind}",
            SpanKind.CONSUMER,
            {"job.kind": job.kind, "job.id": str(job.id), "job.attempts": attempts},
            context=parent,
        ) as job_span,
    ):
        logger.debug("job claimed", extra=attempt)
        if overdue > kind.grace:
            await asyncio.to_thread(
                _record,
                job.id,
                "UPDATE jobs SET completed_at = now(), skipped = true WHERE id = :id",
            )
            logger.info("job skipped", extra=attempt)
            tracing.JOB_RUNS.add(1, labels | {"job.outcome": "skipped"})
            return
        tracing.JOB_RUNNING.add(1, labels)
        try:
            # A timed-out handler thread cannot be stopped and keeps running; its own I/O timeouts
            # bound it. The job was already claimed, so it is retried after its backoff.
            async with asyncio.timeout(kind.timeout):
                await asyncio.to_thread(kind.handler, job)
        except Exception as error:
            # The class only: an error's text can quote an address or a row (ZIF-93: last_error).
            last = attempts >= MAX_ATTEMPTS  # never retried: that needs a human
            logger.log(
                logging.ERROR if last else logging.WARNING,
                "job gave up" if last else "job failed",
                extra={"error": type(error).__name__, **attempt},
                exc_info=error,
            )
            # Before the _record below: a database error there must not lose the count.
            tracing.JOB_RUNS.add(1, labels | {"job.outcome": "gave_up" if last else "failed"})
            # The handler's error never escapes _run, so tracing.span never sees it; set on the
            # job span explicitly instead.
            job_span.set_status(trace.Status(trace.StatusCode.ERROR, logs.error_summary(error)))
            job_span.set_attribute("error.type", type(error).__name__)
            await asyncio.to_thread(
                _record,
                job.id,
                "UPDATE jobs SET last_error = :error WHERE id = :id",
                error=logs.error_summary(error),
            )
        else:
            await asyncio.to_thread(
                _record, job.id, "UPDATE jobs SET completed_at = now() WHERE id = :id"
            )
            logger.info("job done", extra=attempt)
            tracing.JOB_RUNS.add(1, labels | {"job.outcome": "done"})
            tracing.JOB_ATTEMPTS.record(attempts, labels)
        finally:
            # A timed-out handler's thread may still run; it leaves the count here regardless.
            tracing.JOB_RUNNING.add(-1, labels)


async def run_once(kinds: dict[str, JobKind]) -> int:
    """Claim up to 20 due jobs of the given kinds (MAX_ATTEMPTS at most) and run them concurrently.

    Returns how many were claimed.
    """
    claimed = await asyncio.to_thread(_claim, list(kinds))
    await asyncio.gather(
        *(_run(kinds[job.kind], job, attempts, overdue) for job, attempts, overdue in claimed)
    )
    return len(claimed)
