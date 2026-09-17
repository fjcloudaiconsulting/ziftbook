import asyncio
import json
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app import logs, worker
from app.config import WorkerSettings
from app.db import SessionLocal
from app.jobs import Job, JobKind, enqueue
from app.worker import work

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
    bound_session: None, healthcheck: tuple[str, threading.Event]
) -> None:
    url, pinged = healthcheck
    ran: list[Job] = []
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


# B7: a failed healthcheck ping logs the error's class only, never the URL (worker.py:49).
def test_a_failed_ping_logs_the_class_only(
    bound_session: None, log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = uuid.uuid4().hex
    tried = threading.Event()

    def ping(url: str) -> None:
        tried.set()
        raise OSError(sentinel)

    monkeypatch.setattr(worker, "ping", ping)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(worker.work({}, f"http://127.0.0.1:9/{sentinel}", stop))
        await asyncio.to_thread(tried.wait, 10)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())

    line = next((rec for rec in log_lines() if rec.get("msg") == "healthcheck ping failed"), None)
    assert line is not None
    assert line["error"] == "OSError"
    assert sentinel not in json.dumps(log_lines())
