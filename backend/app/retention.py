"""The worker's retention sweep (ZIF-235): personal data is kept only as long as it is needed.

Run hourly by the worker (app.worker), as ziftbook_worker, in batches of short transactions:

- rate_limits rows (counters, the failed-sign-in ones included; their keys can hold an address):
  deleted once their window started longer ago than limits.MAX_WINDOW plus MARGIN;
- sessions: deleted MARGIN after they stop being live (expired, or idle past auth.IDLE);
- contact_email_senders (ZIF-232): a business's place in an inbox's day, deleted MARGIN after the
  day of its last claim and link (contact_email.PURGE_SENDERS, the statement claims purge with);
- admin sessions, account sessions (ZIF-146), audit events, platform audit events and
  booking-event addresses: through the migrate-owned functions of migration 0074, which fix the
  periods themselves (failed sign-ins 30 days, other audit events a year, booking-event addresses
  18 months after both the event and the appointment) and which only ziftbook_worker may call;
- consent addresses (ZIF-156): through migration 0096's sweep_consent_origins, a year after a newer
  consent for the same client and purpose replaced it; the consent that stands keeps its proof.

MARGIN: a reader whose transaction began before the sweep's reads with an earlier now(); an hour
is longer than any runtime transaction lives (30 s statement timeout, 60 s idle in transaction),
so no reader can still see a swept row as live.

During a hold (ZIF_RETENTION_HOLD_UNTIL, an incident's evidence) audit events, booking-event
addresses and consent addresses are kept; sessions, rate limits, contact email senders and admin
and account sessions, which are live state, are still swept.

A run stops between batches when the budget is spent, after MAX_BATCHES, or when the worker is
stopping; whatever is left goes on the next run. Every statement is time-based, so a repeated,
interrupted or concurrent run (two replicas) converges, and a restored backup is swept again.
"""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from opentelemetry.trace import SpanKind
from sqlalchemy import text

from app import auth, contact_email, limits, tracing
from app.db import SessionLocal

logger = logging.getLogger("app.retention")

BATCH = 1000  # rows per statement; the 0074 functions use the same
MAX_BATCHES = 50
BUDGET_SECONDS = 20.0  # wall clock per run: jobs (reset links, payments) wait while it runs
MARGIN = limits.MARGIN  # one value with limits' own purge
MAX_HOLD = timedelta(
    days=90
)  # the furthest ahead a retention hold may end (ZIF_RETENTION_HOLD_UNTIL)

# platform_audit_events' failure actions, kept 30 days like failed sign-ins (owner ruling D3);
# migrations 0072 and 0074 repeat the list, and a test compares them.
PLATFORM_FAILURES = ("admin_sign_in_failed", "second_factor_failed", "totp_enrolment_failed")

# Every table that stores an address or a browser, and the rule that bounds it. A test compares
# this with the catalog, so a new one is classified before it ships.
COVERED = {
    "sessions": "deleted an hour after it stops being live (SESSIONS below)",
    "admin_sessions": "deleted an hour after it stops being live (sweep_admin_sessions, 0074)",
    "account_sessions": "deleted an hour after it stops being live (sweep_account_sessions, 0074)",
    "audit_events": "failed sign-ins deleted after 30 days, the rest after a year (0073, 0074)",
    "platform_audit_events": "failures deleted after 30 days, the rest after a year (0074)",
    "booking_events": (
        "ip and user_agent cleared 18 months after both the event and the booking's end; the row"
        " stays as evidence (sweep_booking_origins, 0074)"
    ),
    "consents": (
        "ip and user_agent cleared a year after a newer consent for the same client and purpose"
        " replaced it; the one that stands keeps them (owner ruling D4), the row always stays as"
        " evidence, and a merchant-entered row holds neither (ZIF-156, sweep_consent_origins, 0096)"
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
ACCOUNT_SESSIONS = text("SELECT deleted FROM sweep_account_sessions()")
AUDIT_EVENTS = text("SELECT * FROM sweep_audit_events()")
BOOKING_ORIGINS = text("SELECT cleared, next_tenant FROM sweep_booking_origins(:start)")
CONSENT_ORIGINS = text("SELECT cleared, next_tenant FROM sweep_consent_origins(:start)")


@dataclass
class Swept:
    """One run's counts. Never a key, an address or an id."""

    rate_limits: int = 0
    sessions: int = 0
    admin_sessions: int = 0
    account_sessions: int = 0
    contact_email_senders: int = 0
    failed_sign_ins: int = 0
    older_events: int = 0
    platform_failures: int = 0
    platform_events: int = 0
    booking_origins: int = 0
    consent_origins: int = 0
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


def _contact_email_senders(swept: Swept) -> bool:
    count = _deleted(contact_email.PURGE_SENDERS)
    swept.contact_email_senders += count
    return count < BATCH


def _admin_sessions(swept: Swept) -> bool:
    with SessionLocal.begin() as session:
        count: int = session.scalar(ADMIN_SESSIONS)
    swept.admin_sessions += count
    return count < BATCH


def _account_sessions(swept: Swept) -> bool:
    with SessionLocal.begin() as session:
        count: int = session.scalar(ACCOUNT_SESSIONS)
    swept.account_sessions += count
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
    """The pass over the businesses for the booking-event (or consent) addresses, resumed call by
    call and run by run: a run that stops before the pass ends (budget, MAX_BATCHES) leaves its
    place here, and the next run goes on from it, so no business is starved. Back to the first
    business only once a call says the pass is complete. Per worker process: a restart starts a new
    pass (a full pass fits in one run up to MAX_BATCHES x 200 = 10,000 businesses), and each replica
    keeps its own place (SKIP LOCKED splits the rows)."""

    def __init__(self, statement: Any, count: str) -> None:
        self.statement = statement
        self.count = count  # the Swept field it adds to
        self.start: UUID | None = None

    def __call__(self, swept: Swept) -> bool:
        with SessionLocal.begin() as session:
            row = session.execute(self.statement, {"start": self.start}).one()
        setattr(swept, self.count, getattr(swept, self.count) + row.cleared)
        self.start = row.next_tenant
        return self.start is None


WALK = _Walk(BOOKING_ORIGINS, "booking_origins")
CONSENT_WALK = _Walk(CONSENT_ORIGINS, "consent_origins")


@dataclass(frozen=True)
class Hold:
    """ZIF_RETENTION_HOLD_UNTIL, parsed (owner ruling D6, the architect's ruling on the review).

    on: a plain YYYY-MM-DD from tomorrow to MAX_HOLD ahead; exclusive, held until 00:00 UTC on
    that date, then over by itself (today's date has therefore passed). past: a date that parsed
    and has come; not held (the worker logs an ERROR at startup: a mistyped year must show).
    invalid: anything else (`0`, a datetime, a number, more
    than MAX_HOLD ahead); held until someone fixes it, with an ERROR every run and no healthcheck
    ping, so it can neither turn the protection off nor keep data quietly for years."""

    raw: str | None
    state: Literal["off", "on", "past", "invalid"]
    until: date | None = None

    def held(self, today: date) -> bool:
        if self.state == "invalid":
            return True
        return self.state == "on" and self.until is not None and today < self.until


PLAIN_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_hold(raw: str | None, today: date) -> Hold:
    if raw is None:
        return Hold(raw, "off")
    if not PLAIN_DATE.fullmatch(raw):
        return Hold(raw, "invalid")
    try:
        until = date.fromisoformat(raw)
    except ValueError:
        return Hold(raw, "invalid")
    if until <= today:
        return Hold(raw, "past", until)
    if until > today + MAX_HOLD:
        return Hold(raw, "invalid")
    return Hold(raw, "on", until)


def sweep(should_stop: Callable[[], bool] = lambda: False, hold: Hold | None = None) -> Swept:
    """One run: batches until nothing is left, the budget is spent or the worker stops. While the
    hold holds, the evidence is kept."""
    held = hold is not None and hold.held(datetime.now(UTC).date())
    swept = Swept(held=held)
    steps: list[Callable[[Swept], bool]] = [
        _rate_limits,
        _sessions,
        _contact_email_senders,
        _admin_sessions,
        _account_sessions,
    ]
    if not held:
        steps += [_audit_events, WALK, CONSENT_WALK]
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
    # WARNING when anything was left: a backlog, a hold, or a busy audit sweep (one worker runs in
    # production, so a lasting busy means something else holds the sweep's lock).
    quiet = swept.complete and not swept.held and not swept.busy
    level = logging.INFO if quiet else logging.WARNING
    if held:
        assert hold is not None
        fields["hold_until"] = (hold.raw or "")[:40]  # as set, so an invalid value shows
    logger.log(level, "retention swept", extra=fields)
    return swept
