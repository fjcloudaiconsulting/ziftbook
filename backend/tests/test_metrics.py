"""The queue and connection-pool gauges and the after-commit counter (ZIF-88). Each fence names the
wrong implementation it kills."""

import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app import jobs
from app.db import SessionLocal, count_after_commit
from app.jobs import enqueue
from app.worker import KINDS
from tests.conftest import closed, points

Lines = Callable[[], list[dict[str, Any]]]


class Spy:
    def __init__(self) -> None:
        self.added: list[dict[str, str] | None] = []

    def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
        self.added.append(attributes)


@pytest.fixture
def due_jobs(bound: None, app_engine: Engine) -> Iterator[None]:
    """Two claimable email.send jobs (one waited an hour) and a due job of an unregistered kind."""
    suffix = uuid.uuid4().hex
    with SessionLocal.begin() as session:
        enqueue(session, "email.send", f"test.queue:{suffix}:1", {})
        enqueue(session, "test.unregistered", f"test.queue:{suffix}:2", {})
        enqueue(session, "email.send", f"test.queue:{suffix}:3", {})  # waiting no time at all
        session.execute(
            text(
                "UPDATE jobs SET next_attempt_at = now() - interval '1 hour'"
                " WHERE dedupe_key IN (:one, :two)"
            ),
            {"one": f"test.queue:{suffix}:1", "two": f"test.queue:{suffix}:2"},
        )
    yield
    with app_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM jobs WHERE dedupe_key LIKE :k"), {"k": f"test.queue:{suffix}:%"}
        )


# M4 fence: the queue gauges report every registered kind (0 when empty), count only claimable
# registered kinds, and age from when the job became claimable.
# Kills: no zero fill, kind = ANY(:kinds) dropped, a constant or max() age, the gauge unregistered.
def test_the_queue_gauges_report_every_registered_kind_and_nothing_else(
    due_jobs: None, metric_reader: InMemoryMetricReader
) -> None:
    sizes = {
        p.attributes["job.kind"]: p.value for p in points(metric_reader, "ziftbook.job.queue.size")
    }
    ages = {
        p.attributes["job.kind"]: p.value
        for p in points(metric_reader, "ziftbook.job.queue.oldest_age")
    }

    assert set(sizes) == set(ages) == set(KINDS)
    assert sizes["email.send"] >= 2
    assert ages["email.send"] >= 3600
    assert "test.unregistered" not in jobs.queue(list(KINDS))
    closed(metric_reader)


# M4 guard: with no session bound (collection, a test importing the worker) the gauges report
# nothing and nothing is logged as a failed callback.
def test_unbound_gauges_report_nothing_and_do_not_fail(
    metric_reader: InMemoryMetricReader, log_lines: Lines
) -> None:
    assert points(metric_reader, "ziftbook.job.queue.size") == []
    assert points(metric_reader, "ziftbook.job.queue.oldest_age") == []
    assert not [line for line in log_lines() if "Callback failed" in line["msg"]]


@pytest.fixture
def small_pool(monkeypatch: pytest.MonkeyPatch, migrated: None) -> Iterator[Engine]:
    # The worker's queue gauge would take the only connection while the test holds it.
    monkeypatch.setattr(jobs, "queue", lambda kinds: {})
    engine = create_engine(
        os.environ["ZIF_DATABASE_URL"], pool_size=1, max_overflow=0, pool_timeout=5
    )
    SessionLocal.configure(bind=engine)
    yield engine
    SessionLocal.configure(bind=None)
    engine.dispose()


def pool(reader: InMemoryMetricReader) -> tuple[dict[str, int], list[int]]:
    states = {
        p.attributes["db.client.connection.state"]: p.value
        for p in points(reader, "db.client.connection.count")
    }
    return states, [p.value for p in points(reader, "db.client.connection.pending_requests")]


# M6 fence: used and idle follow the pool, and pending counts a checkout blocked on it.
# Kills: pending hard-coded 0, used and idle swapped, SQLAlchemy or CPython moving the internals
# the waiter count reads.
def test_the_pool_gauges_follow_checkouts_and_waiters(
    small_pool: Engine, metric_reader: InMemoryMetricReader
) -> None:
    held = small_pool.connect()
    waiter = threading.Thread(target=lambda: small_pool.connect().close())
    try:
        assert pool(metric_reader) == ({"used": 1, "idle": 0}, [0])

        waiter.start()
        deadline = time.monotonic() + 5
        while pool(metric_reader)[1] != [1] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pool(metric_reader)[1] == [1]
    finally:
        held.close()
        if waiter.is_alive():
            waiter.join(5)
    assert pool(metric_reader) == ({"used": 0, "idle": 1}, [0])
    closed(metric_reader)


# M6 guard: an engine without a queue pool (the tests' NullPool) exports no pool points.
def test_a_pool_that_cannot_be_read_exports_nothing(
    bound: None, metric_reader: InMemoryMetricReader, log_lines: Lines
) -> None:
    assert points(metric_reader, "db.client.connection.count") == []
    assert points(metric_reader, "db.client.connection.pending_requests") == []
    assert not [line for line in log_lines() if "Callback failed" in line["msg"]]


# M9 guard: a count waits for the root commit, not a released savepoint, and a rollback drops it.
# Kills: a plain after_commit listener (it fires when a savepoint is released), counts surviving a
# rollback on the same session.
def test_a_count_waits_for_the_root_commit_and_a_rollback_drops_it() -> None:
    engine = create_engine("sqlite://", poolclass=NullPool)
    spy = Spy()
    with SessionLocal(bind=engine) as session:
        with session.begin():
            count_after_commit(session, spy, {"job.kind": "x"})  # type: ignore[arg-type]
            with session.begin_nested():
                pass
            assert spy.added == []
        assert spy.added == [{"job.kind": "x"}]

        session.begin()
        count_after_commit(session, spy)  # type: ignore[arg-type]
        session.rollback()
        with session.begin():
            pass
        assert len(spy.added) == 1
