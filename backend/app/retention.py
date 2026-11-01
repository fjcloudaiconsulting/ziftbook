"""The worker's retention sweep (ZIF-235): personal data is kept only as long as it is needed.

Run hourly by the worker (app.worker), as ziftbook_worker, in batches of short transactions:

- rate_limits rows (counters, the failed-sign-in ones included; their keys can hold an address):
  deleted once their window started longer ago than limits.MAX_WINDOW plus MARGIN;
- sessions: deleted MARGIN after they stop being live (expired, or idle past auth.IDLE);
- admin sessions, audit events, platform audit events and booking-event addresses: through the
  migrate-owned functions of migration 0074, which fix the periods themselves (failed sign-ins 30
  days, other audit events a year, booking-event addresses 18 months after both the event and the
  appointment) and which only ziftbook_worker may call.

MARGIN: a reader whose transaction began before the sweep's reads with an earlier now(); an hour
is longer than any runtime transaction lives (30 s statement timeout, 60 s idle in transaction),
so no reader can still see a swept row as live.

A run stops between batches when the budget is spent, after MAX_BATCHES, or when the worker is
stopping; whatever is left goes on the next run. Every statement is time-based, so a repeated,
interrupted or concurrent run (two replicas) converges, and a restored backup is swept again.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from opentelemetry.trace import SpanKind
from sqlalchemy import text

from app import auth, limits, tracing
from app.db import SessionLocal

logger = logging.getLogger("app.retention")

BATCH = 1000  # rows per statement; the 0074 functions use the same
MAX_BATCHES = 50
BUDGET_SECONDS = 20.0  # wall clock per run: jobs (reset links, payments) wait while it runs
MARGIN = timedelta(hours=1)

# platform_audit_events' failure actions, kept 30 days like failed sign-ins (owner ruling D3);
# migrations 0072 and 0074 repeat the list, and a test compares them.
PLATFORM_FAILURES = ("admin_sign_in_failed", "second_factor_failed", "totp_enrolment_failed")

# Every table that stores an address or a browser, and the rule that bounds it. A test compares
# this with the catalog, so a new one is classified before it ships.
COVERED = {
    "sessions": "deleted an hour after it stops being live (SESSIONS below)",
    "admin_sessions": "deleted an hour after it stops being live (sweep_admin_sessions, 0074)",
    "audit_events": "failed sign-ins deleted after 30 days, the rest after a year (0073, 0074)",
    "platform_audit_events": "failures deleted after 30 days, the rest after a year (0074)",
    "booking_events": (
        "ip and user_agent cleared 18 months after both the event and the booking's end; the row"
        " stays as evidence (sweep_booking_origins, 0074)"
    ),
    "consents": (
        "exempt until ZIF-156 decides (owner ruling D4): the proof of a consent is needed while it"
        " stands, so its address needs a rule tied to the consent's life, not to its age"
    ),
}

RATE_LIMITS = text("""
DELETE FROM rate_limits WHERE key = ANY(ARRAY(
  SELECT key FROM rate_limits WHERE window_start < now() - CAST(:keep AS interval)
  LIMIT :batch FOR UPDATE SKIP LOCKED))
RETURNING 1
""")

# The negation of auth.RESOLVE's liveness, each side an hour later.
SESSIONS = text("""
DELETE FROM sessions WHERE id_hash = ANY(ARRAY(
  SELECT id_hash FROM sessions
  WHERE expires_at <= now() - CAST(:margin AS interval)
     OR last_seen_at <= now() - CAST(:idle AS interval)
  LIMIT :batch FOR UPDATE SKIP LOCKED))
RETURNING 1
""")

ADMIN_SESSIONS = text("SELECT deleted FROM sweep_admin_sessions()")
AUDIT_EVENTS = text("SELECT * FROM sweep_audit_events()")
BOOKING_ORIGINS = text("SELECT cleared, next_tenant FROM sweep_booking_origins(:start)")


@dataclass
class Swept:
    """One run's counts. Never a key, an address or an id."""

    rate_limits: int = 0
    sessions: int = 0
    admin_sessions: int = 0
    failed_sign_ins: int = 0
    older_events: int = 0
    platform_failures: int = 0
    platform_events: int = 0
    booking_origins: int = 0
    batches: int = 0
    complete: bool = False  # nothing left past its period (as far as this run could see)
    busy: bool = False  # another worker held the audit batch; it sweeps them
    held: bool = False  # a retention hold kept the evidence (ZIF_RETENTION_HOLD_UNTIL)


def _deleted(statement: Any, **params: Any) -> int:
    # One short transaction per statement, one pooled connection at a time.
    with SessionLocal.begin() as session:
        return len(session.execute(statement, {"batch": BATCH, **params}).all())


def _rate_limits(swept: Swept) -> bool:
    count = _deleted(RATE_LIMITS, keep=limits.MAX_WINDOW + MARGIN)
    swept.rate_limits += count
    return count < BATCH


def _sessions(swept: Swept) -> bool:
    count = _deleted(SESSIONS, margin=MARGIN, idle=auth.IDLE + MARGIN)
    swept.sessions += count
    return count < BATCH


def _admin_sessions(swept: Swept) -> bool:
    with SessionLocal.begin() as session:
        count: int = session.scalar(ADMIN_SESSIONS)
    swept.admin_sessions += count
    return count < BATCH


def _audit_events(swept: Swept) -> bool:
    with SessionLocal.begin() as session:
        row = session.execute(AUDIT_EVENTS).one()
    if row.busy:
        swept.busy = True
        return True  # the worker holding the lock sweeps them
    counts = (row.failed_sign_ins, row.older_events, row.platform_failures, row.platform_events)
    swept.failed_sign_ins += row.failed_sign_ins
    swept.older_events += row.older_events
    swept.platform_failures += row.platform_failures
    swept.platform_events += row.platform_events
    return all(count < BATCH for count in counts)


class _Walk:
    """One pass over the businesses for the booking-event addresses, resumed call by call."""

    def __init__(self) -> None:
        self.start: UUID | None = None

    def __call__(self, swept: Swept) -> bool:
        with SessionLocal.begin() as session:
            row = session.execute(BOOKING_ORIGINS, {"start": self.start}).one()
        swept.booking_origins += row.cleared
        self.start = row.next_tenant
        return self.start is None


def sweep(should_stop: Callable[[], bool] = lambda: False) -> Swept:
    """One run: batches until nothing is left, the budget is spent or the worker stops."""
    swept = Swept()
    steps: list[Callable[[Swept], bool]] = [
        _rate_limits,
        _sessions,
        _admin_sessions,
        _audit_events,
        _Walk(),
    ]
    started = time.monotonic()
    with tracing.span("retention.sweep", SpanKind.INTERNAL, {}) as span:
        while steps:
            if (
                should_stop()
                or swept.batches >= MAX_BATCHES
                or time.monotonic() - started >= BUDGET_SECONDS
            ):
                break
            swept.batches += 1
            steps = [step for step in steps if not step(swept)]
        swept.complete = not steps
        fields = asdict(swept)
        for name, value in fields.items():
            span.set_attribute(name, value)
    level = logging.INFO if swept.complete and not swept.held else logging.WARNING
    logger.log(level, "retention swept", extra=fields)
    return swept
