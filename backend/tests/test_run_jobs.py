import asyncio
import os
import threading
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import timedelta

import pytest
import requests
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app import jobs
from app.db import SessionLocal
from app.jobs import Job, JobKind, enqueue, run_once
from tests.conftest import closed, points


@pytest.fixture(autouse=True)
def bound_session(migrated: None, app_engine: Engine) -> Iterator[None]:
    engine = create_engine(os.environ["ZIF_DATABASE_URL"], poolclass=NullPool)
    SessionLocal.configure(bind=engine)
    yield
    SessionLocal.configure(bind=None)
    engine.dispose()
    with app_engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs WHERE kind LIKE 'test.%'"))


def add(kind: str, key: str) -> None:
    with SessionLocal.begin() as session:
        enqueue(session, kind, key, {})


def run_until_idle(kinds: dict[str, JobKind], workers: int = 1) -> None:
    async def loop() -> None:
        while sum(await asyncio.gather(*(run_once(kinds) for _ in range(workers)))):
            pass

    asyncio.run(loop())


def job(key: str) -> tuple[int, str | None, bool, bool]:
    with SessionLocal() as session:
        row = session.execute(
            text(
                "SELECT attempts, last_error, completed_at IS NOT NULL, skipped"
                " FROM jobs WHERE dedupe_key = :key"
            ),
            {"key": key},
        ).one()
    return row[0], row[1], row[2], row[3]


def test_concurrent_workers_run_each_job_exactly_once() -> None:
    runs: Counter[str] = Counter()
    lock = threading.Lock()

    def count(job: Job) -> None:
        with lock:
            runs[str(job.id)] += 1

    for n in range(60):
        add("test.count", f"test.count:{n}")
    run_until_idle({"test.count": JobKind(count, 5, timedelta(hours=1))}, workers=2)

    assert len(runs) == 60
    assert set(runs.values()) == {1}


def test_a_handler_that_times_out_is_retried() -> None:
    release = threading.Event()
    calls = []

    def hang_first_time(job: Job) -> None:
        calls.append(job.id)
        if len(calls) == 1:
            release.wait(5)

    kinds = {"test.slow": JobKind(hang_first_time, 0.2, timedelta(hours=1))}
    add("test.slow", "test.slow:1")

    async def scenario() -> None:
        assert await run_once(kinds) == 1
        release.set()  # let the abandoned thread finish
        attempts, last_error, completed, _ = job("test.slow:1")
        assert (attempts, completed) == (1, False)
        assert last_error == "TimeoutError"

        assert await run_once(kinds) == 0  # backing off
        with SessionLocal.begin() as session:
            session.execute(
                text("UPDATE jobs SET next_attempt_at = now() WHERE dedupe_key = 'test.slow:1'")
            )
        assert await run_once(kinds) == 1

    asyncio.run(scenario())
    attempts, _, completed, _ = job("test.slow:1")
    assert (attempts, completed, len(calls)) == (2, True, 2)


def test_a_failed_mail_job_stores_the_class_never_the_address() -> None:
    address = f"{uuid.uuid4()}@example.com"

    def refuse(job: Job) -> None:
        raise requests.HTTPError(f"400 rejected {address}")

    add("test.mail_fail", "test.mail_fail:1")
    run_until_idle({"test.mail_fail": JobKind(refuse, 5, timedelta(hours=1))})

    _, last_error, _, _ = job("test.mail_fail:1")
    assert last_error == "HTTPError"
    assert address not in (last_error or "")


def test_a_failed_db_job_stores_sqlstate_and_constraint_never_the_row() -> None:
    address = f"{uuid.uuid4()}@example.com"

    def duplicate(job: Job) -> None:
        with SessionLocal.begin() as session:
            session.execute(text("INSERT INTO users (email) VALUES (:e), (:e)"), {"e": address})

    add("test.db_fail", "test.db_fail:1")
    run_until_idle({"test.db_fail": JobKind(duplicate, 5, timedelta(hours=1))})

    _, last_error, _, _ = job("test.db_fail:1")
    assert last_error == "IntegrityError sqlstate=23505 constraint=uq_users_email"
    assert address not in (last_error or "")


def test_a_job_past_its_grace_is_skipped() -> None:
    runs: list[Job] = []
    add("test.stale", "test.stale:1")
    with SessionLocal.begin() as session:
        session.execute(
            text("UPDATE jobs SET due_at = now() - interval '2 hours' WHERE kind = 'test.stale'")
        )
    run_until_idle({"test.stale": JobKind(runs.append, 5, timedelta(hours=1))})

    assert runs == []
    attempts, _, completed, skipped = job("test.stale:1")
    assert (attempts, completed, skipped) == (1, True, True)


def runs(reader: InMemoryMetricReader) -> dict[tuple[str, str], int]:
    return {
        (p.attributes["job.kind"], p.attributes["job.outcome"]): p.value
        for p in points(reader, "ziftbook.job.runs")
    }


def refuse(job: Job) -> None:
    raise RuntimeError("no")


# M1 fence: a done job counts once as done, with one attempt, and nothing is left running.
# Kills: done counted before _record or on the wrong branch, running never decremented.
def test_a_done_job_counts_one_run_one_attempt_and_leaves_nothing_running(
    metric_reader: InMemoryMetricReader,
) -> None:
    add("test.ok", "test.ok:1")
    run_until_idle({"test.ok": JobKind(lambda job: None, 5, timedelta(hours=1))})

    assert runs(metric_reader) == {("test.ok", "done"): 1}
    [attempts] = points(metric_reader, "ziftbook.job.attempts")
    assert (attempts.count, attempts.bucket_counts[0]) == (1, 1)
    [running] = points(metric_reader, "ziftbook.job.running")
    assert running.value == 0
    closed(metric_reader)


# M1 fence: a job whose completion cannot be recorded is not counted as done.
# Kills: done counted before _record.
def test_a_completion_that_cannot_be_recorded_is_not_counted_as_done(
    metric_reader: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = jobs._record

    def broken(job_id: uuid.UUID, statement: str, **params: object) -> None:
        if "completed_at = now() WHERE" in statement:
            raise RuntimeError("database gone")
        real(job_id, statement, **params)

    monkeypatch.setattr(jobs, "_record", broken)
    add("test.ok", "test.ok:2")
    with pytest.raises(RuntimeError, match="database gone"):
        asyncio.run(run_once({"test.ok": JobKind(lambda job: None, 5, timedelta(hours=1))}))

    assert runs(metric_reader) == {}
    assert points(metric_reader, "ziftbook.job.attempts") == []
    closed(metric_reader)


# M2 fence: a failing handler counts failed, the last attempt counts gave_up, and neither records
# attempts. Kills: gave_up folded into failed, attempts recorded on failure.
def test_a_failing_job_counts_failed_then_gave_up_and_records_no_attempts(
    metric_reader: InMemoryMetricReader,
) -> None:
    kinds = {"test.bad": JobKind(refuse, 5, timedelta(hours=1))}
    add("test.bad", "test.bad:1")
    run_until_idle(kinds)
    assert runs(metric_reader) == {("test.bad", "failed"): 1}

    with SessionLocal.begin() as session:
        session.execute(
            text("UPDATE jobs SET attempts = 4, next_attempt_at = now() WHERE kind = 'test.bad'")
        )
    run_until_idle(kinds)

    assert runs(metric_reader) == {("test.bad", "failed"): 1, ("test.bad", "gave_up"): 1}
    assert points(metric_reader, "ziftbook.job.attempts") == []
    closed(metric_reader)


# M3 fence: a skipped job counts skipped and never shows as running.
# Kills: skip counted as done, running incremented before the grace check.
def test_a_skipped_job_counts_skipped_and_exports_no_running_point(
    metric_reader: InMemoryMetricReader,
) -> None:
    add("test.stale", "test.stale:2")
    with SessionLocal.begin() as session:
        session.execute(
            text("UPDATE jobs SET due_at = now() - interval '2 hours' WHERE kind = 'test.stale'")
        )
    run_until_idle({"test.stale": JobKind(lambda job: None, 5, timedelta(hours=1))})

    assert runs(metric_reader) == {("test.stale", "skipped"): 1}
    assert points(metric_reader, "ziftbook.job.running") == []
    assert points(metric_reader, "ziftbook.job.attempts") == []
    closed(metric_reader)
