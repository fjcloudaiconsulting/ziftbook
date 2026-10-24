import asyncio
import http.client
import json
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app import bookings, jobs, logs, worker
from app.config import WorkerSettings
from app.db import SessionLocal, tenant_context
from app.jobs import Job, JobKind, enqueue, run_once
from app.worker import work
from tests.conftest import People
from tests.stripe_fake import FakeGateway, connect

Lines = Callable[[], list[dict[str, Any]]]


@pytest.fixture
def healthcheck() -> Iterator[tuple[str, threading.Event]]:
    pinged = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            pinged.set()
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/ping", pinged
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.fixture
def bound_session(migrated: None, app_engine: Engine) -> Iterator[None]:
    engine = create_engine(os.environ["ZIF_DATABASE_URL"], poolclass=NullPool)
    SessionLocal.configure(bind=engine)
    yield
    SessionLocal.configure(bind=None)
    engine.dispose()
    with app_engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs WHERE kind LIKE 'test.%'"))


def test_worker_runs_due_jobs_pings_and_stops(
    bound_session: None,
    healthcheck: tuple[str, threading.Event],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, pinged = healthcheck
    ran: list[Job] = []
    monkeypatch.setattr(bookings, "sweep", lambda: 0)  # this test is about jobs
    with SessionLocal.begin() as session:
        enqueue(session, "test.worker", "test.worker:1", {})

    async def scenario() -> None:
        stop = asyncio.Event()
        kinds = {"test.worker": JobKind(ran.append, 5, timedelta(hours=1))}
        worker = asyncio.create_task(work(kinds, url, stop))
        await asyncio.to_thread(pinged.wait, 10)
        stop.set()
        await asyncio.wait_for(worker, 5)  # stops without waiting out the 30 second poll

    asyncio.run(scenario())
    assert len(ran) == 1
    assert pinged.is_set()


# B3: worker.main() must call logs.configure() before anything else, or its logging never gets
# set up. Make both configure() and WorkerSettings() raise and record who went first, so a
# reorder (settings built before logging is configured) fails on the order, not just on `raises`.
def test_worker_main_configures_logging_first(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    def configure() -> None:
        order.append("configure")
        raise SystemExit("stop after configure")

    def settings_init(self: WorkerSettings, **kw: object) -> None:
        order.append("settings")
        raise SystemExit("stop after settings")

    async def serve(settings: object) -> None:
        return None

    monkeypatch.setattr(logs, "configure", configure)
    monkeypatch.setattr(WorkerSettings, "__init__", settings_init)
    monkeypatch.setattr(worker, "serve", serve)
    monkeypatch.setattr(SessionLocal, "configure", lambda **kw: None)

    with pytest.raises(SystemExit):
        worker.main()

    assert order[0] == "configure", order


# ZIF-95: one startup line with the effective settings, never the Mailgun key or the healthcheck
# URL (a secret-ish check id).
def test_the_worker_logs_its_settings_once_at_startup(
    log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = uuid.uuid4().hex
    monkeypatch.setenv("ZIF_APP_VERSION", "9.8.7")
    monkeypatch.setenv("ZIF_MAILGUN_API_KEY", secret)
    monkeypatch.setenv("ZIF_MAILGUN_DOMAIN", "m.example.test")
    monkeypatch.setenv("ZIF_MAILGUN_REGION", "us")
    monkeypatch.setenv("ZIF_HEALTHCHECK_URL", f"https://hc.example.test/{secret}")

    async def work(*args: object) -> None:
        return None

    monkeypatch.setattr(worker, "work", work)
    asyncio.run(worker.serve(WorkerSettings()))

    started = [
        {k: v for k, v in line.items() if k not in ("ts", "logger")}
        for line in log_lines()
        if line["msg"] == "worker started"
    ]
    assert started == [
        {
            "level": "INFO",
            "msg": "worker started",
            "version": "9.8.7",
            "log_level": "DEBUG",
            "log_format": "json",
            "log_sql": False,
            "kinds": sorted(worker.KINDS),
            "mail": "mailgun",
            "mailgun_domain": "m.example.test",
            "mailgun_region": "us",
            "payments": "off",
            "rate_limit_key": "unset",
        }
    ]
    assert "stripe.event" in worker.KINDS
    dumped = json.dumps(log_lines())
    assert secret not in dumped
    assert "postgresql" not in dumped


# ZB-25: the worker builds peppered keys too (the recipient limits hold a client's address), so its
# startup line carries the key's state, never the key, with the API's WARNING while it is not set.
@pytest.mark.parametrize(
    ("value", "state", "warnings"),
    [
        (None, "unset", 1),
        ("short", "weak", 1),
        ("dev-only-rate-limit-key-not-a-secret-0000", "dev", 1),
        ("k" * 40, "set", 0),
    ],
    ids=["unset", "weak", "dev", "set"],
)
def test_the_worker_startup_line_carries_the_rate_limit_key_state(
    log_lines: Lines,
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    state: str,
    warnings: int,
) -> None:
    if value is None:
        monkeypatch.delenv("ZIF_RATE_LIMIT_KEY", raising=False)
    else:
        monkeypatch.setenv("ZIF_RATE_LIMIT_KEY", value)
    # A public URL: the development key counts as unset there; any other key keeps its state.
    monkeypatch.setenv("ZIF_APP_URL", "https://app.example.com")

    async def work(*args: object) -> None:
        return None

    monkeypatch.setattr(worker, "work", work)
    asyncio.run(worker.serve(WorkerSettings()))

    lines = log_lines()
    assert [line["rate_limit_key"] for line in lines if line["msg"] == "worker started"] == [state]
    unpeppered = [
        line
        for line in lines
        if line["msg"] == "rate limit key unset: rate-limit keys are not peppered"
    ]
    assert [line["level"] for line in unpeppered] == ["WARNING"] * warnings
    if value:
        assert value not in json.dumps(lines)


# B7: a failed healthcheck ping logs the error's class only, never the URL.
# ZIF-21: whatever the ping raises, the worker loops on. urlopen raises ValueError for a URL with
# no scheme and http.client.HTTPException for a garbled response, neither of them an OSError.
@pytest.mark.parametrize("error", [OSError, ValueError, http.client.BadStatusLine])
def test_a_failed_ping_logs_the_class_only(
    error: type[Exception], bound_session: None, log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = uuid.uuid4().hex
    calls: list[str] = []
    tried = threading.Event()

    def ping(url: str) -> None:
        calls.append(url)
        if len(calls) == 2:  # a second loop ran: the first failure didn't end the worker
            tried.set()
        raise error(sentinel)

    monkeypatch.setattr(worker, "ping", ping)
    monkeypatch.setattr(worker, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(bookings, "sweep", lambda: 0)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(worker.work({}, f"http://127.0.0.1:9/{sentinel}", stop))
        looped = await asyncio.to_thread(tried.wait, 10)
        stop.set()
        await asyncio.wait_for(task, 5)
        assert looped, calls

    asyncio.run(scenario())

    line = next((rec for rec in log_lines() if rec.get("msg") == "healthcheck ping failed"), None)
    assert line is not None
    assert line["error"] == error.__name__
    assert sentinel not in json.dumps(log_lines())


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


# ZIF-122: the expiry sweep runs on the first iteration, then every SWEEP_SECONDS. A sweep that
# raises is logged, withholds the ping and is retried next iteration; jobs still run.
def test_the_worker_sweeps_on_a_timer_and_retries_a_failing_sweep(
    bound_session: None, monkeypatch: pytest.MonkeyPatch, log_lines: Lines
) -> None:
    sweeps: list[int] = []
    fail = [True]
    pings: list[str] = []
    ran: list[Job] = []
    clock = [1000.0]

    def sweep() -> int:
        sweeps.append(len(ran))  # jobs drained so far: the sweep runs after the drain
        if fail[0]:
            raise RuntimeError("sweep boom")
        return 0

    monkeypatch.setattr(bookings, "sweep", sweep)
    monkeypatch.setattr(worker, "ping", pings.append)
    # Only the worker's clock: patching time.monotonic itself would freeze asyncio's.
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(worker, "SWEEP_SECONDS", 100)
    with SessionLocal.begin() as session:
        enqueue(session, "test.worker", "test.worker:sweep", {})

    async def scenario() -> None:
        stop = asyncio.Event()
        kinds = {"test.worker": JobKind(ran.append, 5, timedelta(hours=1))}
        monkeypatch.setattr(worker, "POLL_SECONDS", 0.05)
        task = asyncio.create_task(work(kinds, "http://ping.invalid/x", stop))

        await until(lambda: len(sweeps) >= 2)  # failed on iteration 1, retried on iteration 2
        assert pings == [] and len(ran) == 1  # no ping while it fails; the job still ran
        assert sweeps[0] == 1  # the first sweep came after the job, not before
        fail[0] = False
        await until(lambda: len(sweeps) >= 3 and len(pings) >= 1)
        count = len(sweeps)
        await asyncio.sleep(0.3)  # several polls, clock frozen: not again before SWEEP_SECONDS
        assert len(sweeps) == count
        clock[0] += 101
        await until(lambda: len(sweeps) > count)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert any(line["msg"] == "worker iteration failed" for line in log_lines())


# ZIF-35: prune rides the sweep timer, before the sweep. Kills: prune outside the timer `if` (every
# poll), prune placed after the sweep (hostage to a failing tenant), prune not wired in.
def test_the_worker_prunes_on_the_sweep_timer_not_every_poll(
    bound_session: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    prunes: list[int] = []
    sweeps: list[int] = []
    fail_sweep = [False]
    pings: list[str] = []
    clock = [1000.0]

    def sweep() -> int:
        sweeps.append(len(prunes))  # prunes so far: prune comes first
        if fail_sweep[0]:
            raise RuntimeError("sweep boom")
        return 0

    def prune() -> int:
        prunes.append(1)
        return 0

    monkeypatch.setattr(jobs, "prune", prune)
    monkeypatch.setattr(bookings, "sweep", sweep)
    monkeypatch.setattr(worker, "ping", pings.append)
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(worker, "SWEEP_SECONDS", 100)
    monkeypatch.setattr(worker, "POLL_SECONDS", 0.05)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(work({}, "http://ping.invalid/x", stop))
        await until(lambda: len(pings) >= 4)  # several polls, clock frozen
        assert len(prunes) == 1 and sweeps == [1]
        clock[0] += 101
        await until(lambda: len(prunes) == 2)
        fail_sweep[0] = True
        clock[0] += 101
        await until(lambda: len(sweeps) >= 3)  # the sweep raised on this tick...
        assert sweeps[2] == 3  # ...and prune had already run on it (no race with the next poll)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


# Kills: `try: prune() except Exception: log` (a swallowed failure: a timeout would never alert).
def test_a_failing_prune_withholds_the_ping_and_retries(
    bound_session: None, monkeypatch: pytest.MonkeyPatch, log_lines: Lines
) -> None:
    calls: list[int] = []
    pings: list[str] = []
    pings_at_retry: list[list[str]] = []

    def prune() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("prune boom")
        pings_at_retry.append(list(pings))
        return 0

    monkeypatch.setattr(jobs, "prune", prune)
    monkeypatch.setattr(bookings, "sweep", lambda: 0)
    monkeypatch.setattr(worker, "ping", pings.append)
    monkeypatch.setattr(worker, "POLL_SECONDS", 0.05)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(work({}, "http://ping.invalid/x", stop))
        await until(lambda: len(calls) >= 2)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert pings_at_retry[0] == []  # no ping after the failed iteration
    assert any(line["msg"] == "worker iteration failed" for line in log_lines())


# ZIF-158 F21. fence. Kills: gating worker jobs on configured() (all three keys), so every
# production worker, which has no use for the webhook or publishable key, is "off" and nothing is
# released.
def test_the_worker_needs_only_the_secret_key(
    people: People,
    fake_stripe: FakeGateway,
    log_lines: Lines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZIF_STRIPE_WEBHOOK_SECRET")
    monkeypatch.delenv("ZIF_STRIPE_PUBLISHABLE_KEY")
    account = connect(fake_stripe, people.a)
    fake_stripe.accounts[account]["charges_enabled"] = True

    async def idle(*args: object) -> None:
        return None

    monkeypatch.setattr(worker, "work", idle)
    asyncio.run(worker.serve(WorkerSettings()))
    with tenant_context(people.a) as db:
        enqueue(
            db,
            "stripe.event",
            f"stripe.event:{uuid.uuid4()}",
            {"event_id": "evt_1", "type": "account.updated", "object_id": account},
            tenant_id=people.a,
        )

    async def drain() -> None:
        while await run_once(worker.KINDS):
            pass

    asyncio.run(drain())

    lines = log_lines()
    assert [line["payments"] for line in lines if line["msg"] == "worker started"] == ["on"]
    assert not [line for line in lines if line["level"] == "WARNING" and "stripe" in line["msg"]]
    with tenant_context(people.a) as db:
        assert db.scalar(text("SELECT charges_enabled FROM payment_accounts")) is True
