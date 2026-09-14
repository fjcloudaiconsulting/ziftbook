import asyncio
import os
import threading
from collections import Counter
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app.db import SessionLocal
from app.jobs import Job, JobKind, enqueue, run_once


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


def test_a_job_enqueued_twice_runs_once() -> None:
    runs: list[Job] = []
    add("test.once", "test.once:1")
    add("test.once", "test.once:1")
    run_until_idle({"test.once": JobKind(runs.append, 5, timedelta(hours=1))})
    assert len(runs) == 1


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
        assert last_error is not None and "TimeoutError" in last_error

        assert await run_once(kinds) == 0  # backing off
        with SessionLocal.begin() as session:
            session.execute(
                text("UPDATE jobs SET next_attempt_at = now() WHERE dedupe_key = 'test.slow:1'")
            )
        assert await run_once(kinds) == 1

    asyncio.run(scenario())
    attempts, _, completed, _ = job("test.slow:1")
    assert (attempts, completed, len(calls)) == (2, True, 2)


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
