"""The people limit (ZIF-78): a business with more live members than its plan's places is stamped
over the limit, and cleared once back within them. For the grace period (entitlements.GRACE) from
the stamp everyone still signs in; after it only the members it keeps do, the others' memberships
paused until the business is back within its places. Runs as a job, as the worker: only it may
write over_limit_since (0103) and access_paused_at (0104). The hourly sweep also sends the trial's
and the grace's emails.
"""

import logging
import uuid
from datetime import UTC, datetime
from uuid import UUID

from opentelemetry.trace import SpanKind
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import jobs, logs, tracing
from app.db import SessionLocal, tenant_context
from app.entitlements import GRACE, LIVE_MEMBERS, entitlements

logger = logging.getLogger(__name__)

# Stamps only an unstamped business and clears only a stamped one, so a repeat run keeps the
# first stamp: the 30 days run from when the business first went over. A row back: it changed.
STAMP = text("""
UPDATE tenant_billing SET over_limit_since = CASE WHEN :over THEN now() END
WHERE (over_limit_since IS NULL) = :over
RETURNING over_limit_since
""")
GRACE_OVER = text("SELECT over_limit_since + :grace <= now() FROM tenant_billing")
# Every row this run may change, locked in id order first: members.target locks two memberships in
# id order, and an UPDATE locking in scan order could deadlock with it. Paused rows whatever their
# role, as UNPAUSE clears them all.
LOCK = text("""
SELECT FROM memberships
WHERE (:all AND role <> 'removed') OR access_paused_at IS NOT NULL
ORDER BY id FOR NO KEY UPDATE
""")
UNPAUSE = text("UPDATE memberships SET access_paused_at = NULL WHERE access_paused_at IS NOT NULL")
# Who keeps a place: the members an owner picked, then owners, then workers, the longest-standing
# first (memberships.id is time-ordered). One owner is always kept, the picked or the senior one,
# even past a pick of workers filling the places: else nobody would be left to upgrade.
PAUSE = text("""
WITH live AS (
  SELECT id, keeps_access, role = 'owner' AS owner FROM memberships WHERE role <> 'removed'
), lead AS (
  SELECT id FROM live WHERE owner ORDER BY keeps_access DESC, id LIMIT 1
), ranked AS (
  SELECT id, row_number() OVER (ORDER BY coalesce(id = (SELECT id FROM lead), false) DESC,
                                         keeps_access DESC, owner DESC, id) AS n
  FROM live
)
UPDATE memberships m SET access_paused_at = CASE WHEN r.n > :places THEN now() END
FROM ranked r
WHERE m.id = r.id AND (m.access_paused_at IS NULL) = (r.n > :places)
RETURNING m.user_id, m.access_paused_at IS NOT NULL AS paused
""")
# A paused member's business sessions end, as a removal ends them (members.remove): their bound
# account sessions go with them (0057's cascade). sessions has no row-level security, so tenant_id
# is mandatory. After the memberships, as every writer locks memberships, then sessions.
END_SESSIONS = text("DELETE FROM sessions WHERE tenant_id = :t AND user_id = ANY(:users)")
# A business that pays or was given a plan gets no trial email: a live grant, or a subscription in
# any state but the two Stripe ends (Checkout's already_subscribed rule).
SUBSCRIBED = text("""
SELECT EXISTS (SELECT FROM plan_grants WHERE ends_at IS NULL OR ends_at > now())
    OR EXISTS (SELECT FROM subscriptions WHERE status NOT IN ('canceled', 'incomplete_expired'))
""")
# The live owners an owner notice goes to.
OWNERS = text(
    "SELECT user_id FROM memberships WHERE role = 'owner' AND access_paused_at IS NULL ORDER BY id"
)


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


def tell_owners(db: Session, tenant_id: UUID, template: str, stamp: datetime) -> None:
    """One email.send per live owner, in the caller's transaction, keyed by the stamp the mail
    tells of (a trial's end, an over-the-limit mark): once per stamp, and again for a new one.
    mail.send skips it once the business has moved past that stamp."""
    at = stamp.isoformat()
    for user_id in db.scalars(OWNERS):
        jobs.enqueue(
            db,
            "email.send",
            f"email.send:{tenant_id}:{template}:{at}:{user_id}",
            {"recipient_id": str(user_id), "template": template, "stamp": at},
            tenant_id=tenant_id,
        )


def run(job: jobs.Job) -> None:
    assert job.tenant_id is not None
    with tenant_context(job.tenant_id) as db:
        places = entitlements(db).count("places")
        # Live members only, not pending invites: an invite never starts the grace clock (spec).
        over = db.scalar(LIVE_MEMBERS) > places
        stamped = db.scalar(STAMP, {"over": over})
        if stamped is not None:
            tell_owners(db, job.tenant_id, "over_limit_started", stamped)
        # NULL (not over, or no tenant_billing row) pauses nobody.
        ended = over and db.scalar(GRACE_OVER, {"grace": GRACE}) is True
        db.execute(LOCK, {"all": ended})
        if ended:
            changed = db.execute(PAUSE, {"places": max(places, 1)}).all()
            users = [row.user_id for row in changed if row.paused]
            if users:
                db.execute(END_SESSIONS, {"t": job.tenant_id, "users": users})
        else:
            db.execute(UNPAUSE)


# What the sweep needs to know, in one read. The windows bound each email, so none goes out again
# once the jobs table's 30 days have dropped its dedupe key: the trial's 3 days before its end, the
# day after it ended, and the grace's last 7 days. A trial counts only once an operator has set a
# trial plan (until then it resolves to the default plan, and was never a trial).
SWEEP_READ = text("""
SELECT tb.trial_ends_at, tb.over_limit_since,
       b.trial_plan_id IS NOT NULL
         AND now() >= tb.trial_ends_at - interval '3 days' AND now() < tb.trial_ends_at
         AS trial_ending,
       b.trial_plan_id IS NOT NULL
         AND now() >= tb.trial_ends_at AND now() < tb.trial_ends_at + interval '1 day'
         AS trial_ended,
       now() >= tb.over_limit_since + :grace - interval '7 days'
         AND now() < tb.over_limit_since + :grace AS over_limit_ending,
       EXISTS (SELECT FROM memberships WHERE access_paused_at IS NOT NULL) AS paused,
       (SELECT count(*) FROM memberships WHERE role <> 'removed') AS live
FROM platform_billing b LEFT JOIN tenant_billing tb ON true
""")


def check(db: Session, tenant_id: UUID) -> None:
    """One business's hourly look: its trial's and grace's emails, and an enforce job when its
    places may need one (a trial, grant or override that ran out changes the plan with no write)."""
    found = entitlements(db)
    row = db.execute(SWEEP_READ, {"grace": GRACE}).one()
    if (row.trial_ending or row.trial_ended) and not db.scalar(SUBSCRIBED):
        template = "trial_ending" if row.trial_ending else "trial_ended"
        tell_owners(db, tenant_id, template, row.trial_ends_at)
    if row.over_limit_ending:
        tell_owners(db, tenant_id, "over_limit_ending", row.over_limit_since)
    # Over, stamped or paused: anything else is within its places with nothing to undo. Keyed by
    # the hour, not enqueue()'s unique tail: a sweep retried after a failure adds no second job.
    if row.live > found.count("places") or row.over_limit_since is not None or row.paused:
        hour = datetime.now(UTC).strftime("%Y-%m-%dT%H")
        jobs.enqueue(
            db, "billing.enforce", f"billing.enforce:{tenant_id}:{hour}", {}, tenant_id=tenant_id
        )


def sweep() -> None:
    """Every business's check, on the worker's hourly timer."""
    # The ids in their own short session, closed before the loop, as bookings.sweep does.
    with SessionLocal() as session:
        tenant_ids = session.scalars(text("SELECT id FROM tenants")).all()
    failed = 0
    with tracing.span("enforcement.sweep", SpanKind.INTERNAL, {"tenants": len(tenant_ids)}):
        for tenant_id in tenant_ids:
            # One business that fails never holds back the others' emails.
            try:
                with tenant_context(tenant_id) as db:
                    check(db, tenant_id)
            except Exception:
                failed += 1
                with logs.bound(tenant_id=str(tenant_id)):
                    logger.exception("business sweep failed", extra={"sweep": "enforcement"})
    if failed:
        raise RuntimeError(f"the enforcement sweep failed for {failed} businesses")
