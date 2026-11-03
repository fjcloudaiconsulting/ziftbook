"""The worker's retention sweep (ZIF-235), app.retention.sweep(), run as ziftbook_worker.

The xdist database keeps other tests' rows, so every assertion is on rows this test seeded."""

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app import auth, limits, retention
from app.config import WorkerSettings
from app.db import SessionLocal
from tests.conftest import People, engine_as_worker, jobs_as_the_worker
from tests.test_retention_db import audit_targets, event_rows, seed_audit, seed_booking, seed_event
from tests.test_sessions import start

Lines = Callable[[], list[dict[str, Any]]]


@pytest.fixture(autouse=True)
def _walk_from_the_start() -> Iterator[None]:
    """The booking-origin walk keeps its place across runs (module state): each test starts a new
    pass, and leaves none behind."""
    retention.WALK.start = None
    yield
    retention.WALK.start = None


@pytest.fixture
def action(app_engine: Engine) -> Iterator[str]:
    """A fresh rate-limit key prefix; SessionLocal bound to the worker role, as the worker runs."""
    SessionLocal.configure(bind=app_engine)
    prefix = f"test-retention-{uuid.uuid4()}"
    with jobs_as_the_worker():
        yield prefix
    with app_engine.begin() as conn:
        conn.execute(text("DELETE FROM rate_limits WHERE key LIKE :p"), {"p": f"{prefix}%"})
    SessionLocal.configure(bind=None)


def seed_key(app_engine: Engine, key: str, hits: int, age: str) -> None:
    with app_engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO rate_limits (key, window_start, hits)
            VALUES (:k, now() - CAST(:a AS interval), :h)
            """),
            {"k": key, "a": age, "h": hits},
        )


def keys(app_engine: Engine, prefix: str) -> set[str]:
    with app_engine.connect() as conn:
        return set(
            conn.scalars(text("SELECT key FROM rate_limits WHERE key LIKE :p"), {"p": f"{prefix}%"})
        )


# 18: FENCE (review #3). Wrong impl: a cutoff at MAX_WINDOW with no margin, or shorter; rows past
# the longest window plus the margin kept forever.
def test_rate_limit_rows_go_an_hour_after_the_longest_window(
    action: str, app_engine: Engine
) -> None:
    seed_key(app_engine, f"{action}:gone", 3, "1 day 1 hour 1 minute")
    seed_key(app_engine, f"{action}:margin", 3, "1 day 59 minutes")
    seed_key(app_engine, f"{action}:live", 3, "23 hours")

    swept = retention.sweep()

    assert keys(app_engine, action) == {f"{action}:margin", f"{action}:live"}
    assert swept.rate_limits >= 1


# 19: FENCE. Wrong impl: any cutoff that reaches into a live window of any length a limit uses.
@pytest.mark.parametrize(
    "window",
    [timedelta(minutes=1), timedelta(minutes=15), timedelta(hours=1), limits.MAX_WINDOW],
)
def test_a_live_rate_limit_window_is_never_shortened(action: str, window: timedelta) -> None:
    key = f"{action}:k"
    for _ in range(3):
        limits.hit({key: 10}, window)

    retention.sweep()

    assert limits.count(key, window) == 3
    assert limits.charge(key, window) == 4


# 19b: FENCE (review #3). Wrong impl: no margin. A transaction that began before the sweep reads
# with its own, earlier now(): a row the sweep's clock calls dead (by two seconds here) is still
# live to it, and its hit must count on, not start again at 1.
def test_a_reader_older_than_the_sweep_keeps_its_window(action: str, app_engine: Engine) -> None:
    key = f"{action}:older"
    with Session(app_engine) as reader, reader.begin():
        began = reader.scalar(text("SELECT now()"))
        with app_engine.begin() as conn:
            conn.execute(
                text("""
                INSERT INTO rate_limits (key, window_start, hits)
                VALUES (:k, CAST(:t AS timestamptz) - CAST(:w AS interval) + interval '1 second', 5)
                """),
                {"k": key, "t": began, "w": limits.MAX_WINDOW},
            )
        time.sleep(2)  # the sweep's now() is now past this window's end
        retention.sweep()
        limits.hit({key: 100}, limits.MAX_WINDOW, db=reader)
        hits = reader.scalar(text("SELECT hits FROM rate_limits WHERE key = :k"), {"k": key})
    assert hits == 6


# 20: GUARD. A swept key's next hit starts a new window at 1, as a stale window does.
def test_a_swept_key_starts_again_at_one(action: str, app_engine: Engine) -> None:
    key = f"{action}:again"
    seed_key(app_engine, key, 7, "2 days")

    retention.sweep()

    assert limits.charge(key, limits.MAX_WINDOW) == 1


# 21: FENCE. Wrong impl: no SKIP LOCKED (the sweep would queue behind a request's row lock).
def test_the_rate_limit_sweep_skips_a_row_another_transaction_holds(
    action: str, app_engine: Engine
) -> None:
    seed_key(app_engine, f"{action}:held", 1, "3 days")
    seed_key(app_engine, f"{action}:free", 1, "3 days")
    with app_engine.connect() as holder, holder.begin():
        holder.execute(
            text("SELECT FROM rate_limits WHERE key = :k FOR UPDATE"), {"k": f"{action}:held"}
        )
        started = time.monotonic()
        retention.sweep()
        assert time.monotonic() - started < 10
    assert keys(app_engine, action) == {f"{action}:held"}


def session_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def age_session(app_engine: Engine, token: str, expires: str, seen: str) -> None:
    with app_engine.begin() as conn:
        conn.execute(
            text("""
            UPDATE sessions SET expires_at = now() + CAST(:e AS interval),
                                last_seen_at = now() - CAST(:s AS interval)
            WHERE id_hash = :h
            """),
            {"h": session_hash(token), "e": expires, "s": seen},
        )


def session_exists(app_engine: Engine, token: str) -> bool:
    with app_engine.connect() as conn:
        found = conn.scalar(
            text("SELECT EXISTS (SELECT FROM sessions WHERE id_hash = :h)"),
            {"h": session_hash(token)},
        )
    return bool(found)


# 22: FENCE (review #3). Wrong impl: a live session deleted; a cutoff with no margin; dead
# sessions (and their addresses) kept until someone signs in.
def test_sessions_go_an_hour_after_they_die_and_a_live_one_still_resolves(
    people: People, app_engine: Engine
) -> None:
    ages = {
        "live": ("1 day", "1 minute"),
        "expired_in_margin": ("-59 minutes", "1 minute"),
        "expired": ("-61 minutes", "1 minute"),
        "idle_in_margin": ("1 day", "7 days 59 minutes"),
        "idle": ("1 day", "7 days 61 minutes"),
    }
    tokens = {name: start(people.a, people.only_a) for name in ages}
    for name, (expires, seen) in ages.items():
        age_session(app_engine, tokens[name], expires, seen)

    with jobs_as_the_worker():
        swept = retention.sweep()

    left = {name for name, token in tokens.items() if session_exists(app_engine, token)}
    assert left == {"live", "expired_in_margin", "idle_in_margin"}
    assert swept.sessions >= 2
    with app_engine.begin() as conn:
        resolved = conn.execute(
            auth.RESOLVE,
            {
                "id_hash": session_hash(tokens["live"]),
                "idle": auth.IDLE,
                "touch_every": auth.TOUCH_EVERY,
            },
        ).all()
    assert [row.user_id for row in resolved] == [people.only_a]


# 23: FENCE. Wrong impl: no SKIP LOCKED on sessions (a membership being removed holds them).
def test_the_session_sweep_skips_a_row_another_transaction_holds(
    people: People, app_engine: Engine
) -> None:
    held, free = start(people.a, people.only_a), start(people.a, people.only_a)
    for token in (held, free):
        age_session(app_engine, token, "-2 hours", "1 minute")
    with app_engine.connect() as holder, holder.begin():
        holder.execute(
            text("SELECT FROM sessions WHERE id_hash = :h FOR UPDATE"), {"h": session_hash(held)}
        )
        with jobs_as_the_worker():
            retention.sweep()
    assert session_exists(app_engine, held) and not session_exists(app_engine, free)


# 24: FENCE. Wrong impl: a sweep that swallows a privilege error (as ziftbook_app the retention
# functions refuse): a misconfigured worker must fail loudly, so its ping is withheld.
def test_the_sweep_fails_loudly_as_the_app_role(app_engine: Engine) -> None:
    SessionLocal.configure(bind=app_engine)
    try:
        with pytest.raises(ProgrammingError):
            retention.sweep()
    finally:
        SessionLocal.configure(bind=None)


COUNTS = {
    "rate_limits",
    "sessions",
    "admin_sessions",
    "account_sessions",
    "failed_sign_ins",
    "older_events",
    "platform_failures",
    "platform_events",
    "booking_origins",
    "batches",
}


# 25: FENCE. Wrong impl: a log line or span carrying a key, an address, an id; more than one line
# per run; a missing count.
def test_the_sweep_logs_one_line_of_counts_only(
    action: str, app_engine: Engine, log_lines: Lines, spans: Callable[[], list[Any]]
) -> None:
    seed_key(app_engine, f"{action}:192.0.2.77", 1, "3 days")

    retention.sweep()

    lines = [line for line in log_lines() if line.get("logger") == "app.retention"]
    assert len(lines) == 1
    line = lines[0]
    assert line["msg"] == "retention swept" and line["level"] == "INFO"
    fields = {k: v for k, v in line.items() if k not in ("ts", "level", "logger", "msg")}
    assert set(fields) == COUNTS | {"complete", "busy", "held"}
    assert all(isinstance(fields[name], int) for name in COUNTS)
    assert all(isinstance(fields[name], bool) for name in ("complete", "busy", "held"))
    assert line["complete"] is True
    assert "192.0.2.77" not in json.dumps(log_lines())
    (span,) = [s for s in spans() if s.name == "retention.sweep"]
    assert set(span.attributes or {}) == COUNTS | {"complete", "busy", "held"}


# 26: FENCE (review #4). Wrong impl: no wall-clock budget (a backlog would hold the worker's job
# loop for minutes: password-reset links wait), or a run cut short without saying so.
def test_a_run_over_its_budget_stops_between_batches_and_says_so(
    action: str, app_engine: Engine, log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(5):
        seed_key(app_engine, f"{action}:{n}", 1, "3 days")
    clock = [0.0]

    def monotonic() -> float:
        clock[0] += 15.0  # every look at the clock is 15 seconds later
        return clock[0]

    monkeypatch.setattr(retention, "BATCH", 1)
    monkeypatch.setattr(retention, "time", SimpleNamespace(monotonic=monotonic))

    swept = retention.sweep()

    assert swept.batches == 1 and swept.complete is False
    (line,) = [line for line in log_lines() if line.get("logger") == "app.retention"]
    assert line["level"] == "WARNING" and line["complete"] is False
    assert len(keys(app_engine, action)) == 4


# 26b: FENCE (review #4). Wrong impl: stop not honoured until the run ends (SIGTERM would wait out
# a whole backlog).
def test_stop_ends_a_run_after_the_batch_in_hand(
    action: str, app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(5):
        seed_key(app_engine, f"{action}:{n}", 1, "3 days")
    monkeypatch.setattr(retention, "BATCH", 1)
    looks: list[int] = []

    def should_stop() -> bool:
        looks.append(1)
        return len(looks) > 2

    swept = retention.sweep(should_stop)

    assert swept.batches == 2 and swept.complete is False
    assert len(keys(app_engine, action)) == 3


# M4 (review #4): the worker's pool is sized for its 20 executor threads and the export thread; the
# sweep holds one connection at a time, never two.
def test_the_sweep_holds_one_connection_at_a_time(app_engine: Engine) -> None:
    with engine_as_worker(app_engine.url) as nullpool:
        pooled = create_engine(nullpool.url, pool_size=3, max_overflow=0)
    most = [0]

    @event.listens_for(pooled, "checkout")
    def checkout(*args: object) -> None:
        most[0] = max(most[0], pooled.pool.checkedout())  # type: ignore[attr-defined]

    SessionLocal.configure(bind=pooled)
    try:
        retention.sweep()
    finally:
        SessionLocal.configure(bind=None)
        pooled.dispose()
    assert most[0] == 1


# 27: FENCE. Wrong impl: a busy audit sweep (another replica's batch) that stops the run, so
# sessions and rate limits wait for the other replica too.
def test_a_busy_audit_sweep_still_lets_the_rest_be_swept(
    action: str, app_engine: Engine, migrate_engine: Engine
) -> None:
    seed_key(app_engine, f"{action}:old", 1, "3 days")
    with migrate_engine.connect() as holder, holder.begin():
        holder.execute(text("SELECT pg_advisory_xact_lock(235, 0)"))
        swept = retention.sweep()
    assert swept.busy is True
    assert keys(app_engine, action) == set()


# 17: FENCE (review question 1, #8). Wrong impl: a table that stores an address or a browser and
# that no retention rule covers. Matched by type (inet) and by name, so a text-typed address is
# caught too. A new one fails here until it is swept or exempted with a reason (account_sessions,
# ZIF-146: the negation of 0057's liveness plus the margin, sweep_account_sessions).
ADDRESS_COLUMNS = text(r"""
SELECT DISTINCT table_name FROM information_schema.columns
WHERE table_schema = 'public'
  AND (data_type = 'inet' OR column_name ~ '(^|_)ip($|_)|user_agent|addr')
ORDER BY 1
""")


def test_every_address_and_browser_column_has_a_retention_rule(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        tables = set(conn.scalars(ADDRESS_COLUMNS))
    assert tables == set(retention.COVERED)
    assert "ZIF-156" in retention.COVERED["consents"]


# The 30-day platform failures: one list in the code, the same in both migrations that use it.
def test_the_platform_failure_list_matches_the_migrations() -> None:
    from pathlib import Path

    versions = Path(__file__).parent.parent / "migrations" / "versions"
    quoted = ", ".join(f"'{action}'" for action in retention.PLATFORM_FAILURES)
    for name in ("0072_retention_indexes.py", "0074_retention_sweep.py"):
        source = (versions / name).read_text()
        assert re.search(r'PLATFORM_FAILURES = "(.*)"', source).group(1) == quoted  # type: ignore[union-attr]


# 26c: a run with nothing to do finishes in one batch.
def test_a_quiet_run_takes_one_batch(action: str) -> None:
    first = retention.sweep()
    second = retention.sweep()
    assert first.complete and second.complete
    assert second.batches == 1


# ---- D6: the retention hold (ZIF_RETENTION_HOLD_UNTIL) ----

TODAY = date(2026, 11, 1)


# 28a: FENCE (build review, security #2 and the architect's ruling). Wrong impl: a value that
# turns the hold off without a word: a past or mistyped year, `0` (pydantic reads it as 1970), a
# datetime or a number; or a date far ahead that holds for years. Only a plain YYYY-MM-DD between
# tomorrow and 90 days ahead is a hold that ends by itself; a past date that parsed is "past" (not
# held, an ERROR at startup); anything else is "invalid": held until someone fixes it, loudly.
@pytest.mark.parametrize(
    ("raw", "state", "until"),
    [
        (None, "off", None),
        ("2026-11-20", "on", date(2026, 11, 20)),
        ("2027-01-30", "on", TODAY + timedelta(days=90)),
        ("2027-01-31", "invalid", None),
        ("2062-11-20", "invalid", None),
        ("2026-11-01", "past", date(2026, 11, 1)),
        ("2025-11-20", "past", date(2025, 11, 20)),
        ("0", "invalid", None),
        ("90", "invalid", None),
        ("20261120", "invalid", None),
        ("2026-11-20T00:00:00+05:00", "invalid", None),
        (" 2026-11-20 ", "invalid", None),
        ("2026-02-30", "invalid", None),
        ("next tuesday", "invalid", None),
    ],
)
def test_only_a_plain_date_within_90_days_is_a_hold_that_ends_by_itself(
    raw: str | None, state: str, until: date | None
) -> None:
    hold = retention.parse_hold(raw, TODAY)
    assert (hold.state, hold.until) == (state, until)
    assert hold.held(TODAY) is (state in ("on", "invalid"))


# The architect's ruling: the date is exclusive. A hold until D lasts until 00:00 UTC on D; on D it
# holds nothing, and D given on D itself is a date that has passed.
def test_a_hold_is_exclusive_of_its_date() -> None:
    assert retention.parse_hold("2026-11-01", TODAY).state == "past"
    assert retention.parse_hold("2026-11-02", TODAY).held(date(2026, 11, 1))
    assert not retention.parse_hold("2026-11-02", TODAY).held(date(2026, 11, 2))


def test_an_on_hold_ends_on_its_date_and_an_invalid_one_never() -> None:
    on = retention.parse_hold("2026-11-20", TODAY)
    invalid = retention.parse_hold("0", TODAY)
    assert on.held(date(2026, 11, 19)) and not on.held(date(2026, 11, 20))
    assert invalid.held(date(2030, 1, 1))


def test_the_worker_starts_with_any_hold_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed hold must never stop the worker (emails would stop): it is held, loudly."""
    monkeypatch.setenv("ZIF_DATABASE_URL", "postgresql+psycopg://x@localhost/x")
    monkeypatch.setenv("ZIF_RETENTION_HOLD_UNTIL", "0")
    assert WorkerSettings().retention_hold_until == "0"


# 28b: FENCE. Wrong impl: a hold that is ignored, one that also stops sessions, rate limits or
# admin sessions (live state, not evidence), or one said nowhere.
@pytest.mark.parametrize("raw", ["tomorrow", "0"], ids=["on", "invalid"])
def test_a_hold_keeps_the_evidence_and_says_so_every_run(
    action: str,
    app_engine: Engine,
    superuser: Engine,
    audit_marker: str,
    log_lines: Lines,
    raw: str,
) -> None:
    seed_audit(superuser, f"{audit_marker}:old", "signed_out", "1 year 1 hour")
    seed_key(app_engine, f"{action}:old", 1, "3 days")
    today = datetime.now(UTC).date()
    value = (today + timedelta(days=1)).isoformat() if raw == "tomorrow" else raw
    hold = retention.parse_hold(value, today)

    swept = retention.sweep(hold=hold)

    assert swept.held is True and swept.complete is True
    assert audit_targets(superuser, audit_marker) == {f"{audit_marker}:old"}
    assert keys(app_engine, action) == set()
    (line,) = [line for line in log_lines() if line.get("logger") == "app.retention"]
    assert line["level"] == "WARNING" and line["hold_until"] == value


# 28c: GUARD. A hold whose date has come, or a past one, keeps nothing.
@pytest.mark.parametrize("days", [0, -400])
def test_a_past_hold_keeps_nothing(
    action: str, superuser: Engine, audit_marker: str, days: int
) -> None:
    seed_audit(superuser, f"{audit_marker}:old", "signed_out", "1 year 1 hour")
    today = datetime.now(UTC).date()
    hold = retention.parse_hold((today + timedelta(days=days)).isoformat(), today)

    swept = retention.sweep(hold=hold)

    assert swept.held is False
    assert audit_targets(superuser, audit_marker) == set()


# Build review (correctness #1) FENCE. Wrong impl: a walk that starts at the first business on every
# run. A run that cannot finish the pass (budget, MAX_BATCHES, a backlog elsewhere) would throw its
# place away, and the businesses past what one run reaches (the newest: ids are uuidv7) would keep
# their booking addresses forever. The reviewer's probe: 200 businesses sorting first, one batch
# per run; the target business must be reached within the runs a full pass needs.
def test_the_origin_walk_resumes_where_the_last_run_stopped(
    people: People, superuser: Engine, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    booking_id = seed_booking(superuser, people.a, people.both, "18 months 2 hours")
    event_id = seed_event(superuser, people.a, booking_id, "18 months 2 hours")
    with migrate_engine.begin() as conn:
        ahead = conn.scalars(
            text("""
            INSERT INTO tenants (id, name)
            SELECT CAST('00000000-0000-7000-8000-' || lpad(n::text, 12, '0') AS uuid), 'ahead'
            FROM generate_series(1, 200) AS n
            RETURNING id
            """)
        ).all()
        businesses = conn.scalar(text("SELECT count(*) FROM tenants"))
    monkeypatch.setattr(retention, "MAX_BATCHES", 1)
    try:
        with jobs_as_the_worker():
            for _ in range(-(-businesses // 200) + 1):
                retention.sweep()
                if event_rows(superuser, [event_id])[event_id].ip is None:
                    break
        assert event_rows(superuser, [event_id])[event_id].ip is None
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = ANY(:i)"), {"i": list(ahead)})


# Build review (security #3) FENCE. Wrong impl: a busy run logged at INFO. Production runs one
# worker, so a lasting busy means something else holds the sweep's advisory lock (the app role can
# take any advisory lock), and audit retention has stopped: that must show.
def test_a_busy_run_is_a_warning(action: str, migrate_engine: Engine, log_lines: Lines) -> None:
    with migrate_engine.connect() as holder, holder.begin():
        holder.execute(text("SELECT pg_advisory_xact_lock(235, 0)"))
        retention.sweep()
    (line,) = [line for line in log_lines() if line.get("logger") == "app.retention"]
    assert line["busy"] is True and line["level"] == "WARNING"
