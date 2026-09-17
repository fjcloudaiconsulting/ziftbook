import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import logs
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
    """Add a job in the caller's transaction, unless its dedupe_key was ever used.

    Returns whether it was added. Build the key as "kind:tenant_id:natural id", adding the due time
    when the same thing can be rescheduled ("booking.reminder:<tenant>:<booking>:<starts_at>").
    The payload holds ids only.
    """
    added = session.execute(
        ENQUEUE,
        {
            "kind": kind,
            "dedupe_key": dedupe_key,
            "tenant_id": tenant_id,
            "payload": json.dumps(payload),
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


# One statement claims a batch: SKIP LOCKED keeps concurrent workers apart while claiming, and the
# bumped next_attempt_at keeps them apart afterwards. It is both the lease and the backoff
# (1, 2, 4, 8, 16 minutes), so a crashed worker's jobs come back on their own. = ANY(ARRAY(...)),
# not IN (...): the planner may run an IN subquery with LIMIT more than once and claim extra rows.
CLAIM = text("""
UPDATE jobs
SET attempts = attempts + 1,
    next_attempt_at = now() + interval '1 minute' * 2 ^ attempts
WHERE id = ANY(ARRAY(
    SELECT id FROM jobs
    WHERE completed_at IS NULL AND attempts < 5
      AND next_attempt_at <= now() AND kind = ANY(:kinds)
    ORDER BY next_attempt_at
    LIMIT 20
    FOR UPDATE SKIP LOCKED))
RETURNING id, kind, tenant_id, payload, now() - due_at AS overdue
""")

logger = logging.getLogger(__name__)


def _claim(kinds: list[str]) -> list[tuple[Job, timedelta]]:
    with SessionLocal.begin() as session:
        rows = session.execute(CLAIM, {"kinds": kinds}).all()
    return [(Job(row.id, row.kind, row.tenant_id, row.payload), row.overdue) for row in rows]


def _record(job_id: UUID, statement: str, **params: Any) -> None:
    with SessionLocal.begin() as session:
        session.execute(text(statement), {"id": job_id, **params})


async def _run(kind: JobKind, job: Job, overdue: timedelta) -> None:
    context = {"job_id": str(job.id), "job_kind": job.kind}
    if job.tenant_id is not None:
        context["tenant_id"] = str(job.tenant_id)
    with logs.bound(**context):
        if overdue > kind.grace:
            await asyncio.to_thread(
                _record,
                job.id,
                "UPDATE jobs SET completed_at = now(), skipped = true WHERE id = :id",
            )
            return
        try:
            # A timed-out handler thread cannot be stopped and keeps running; its own I/O timeouts
            # bound it. The job was already claimed, so it is retried after its backoff.
            async with asyncio.timeout(kind.timeout):
                await asyncio.to_thread(kind.handler, job)
        except Exception as error:
            # The class only: an error's text can quote an address or a row (ZIF-93: last_error).
            logger.warning("job failed", extra={"error": type(error).__name__}, exc_info=error)
            await asyncio.to_thread(
                _record,
                job.id,
                "UPDATE jobs SET last_error = :error WHERE id = :id",
                error=repr(error),
            )
        else:
            await asyncio.to_thread(
                _record, job.id, "UPDATE jobs SET completed_at = now() WHERE id = :id"
            )


async def run_once(kinds: dict[str, JobKind]) -> int:
    """Claim up to 20 due jobs of the given kinds (5 attempts at most) and run them concurrently.

    Returns how many were claimed.
    """
    claimed = await asyncio.to_thread(_claim, list(kinds))
    await asyncio.gather(*(_run(kinds[job.kind], job, overdue) for job, overdue in claimed))
    return len(claimed)
