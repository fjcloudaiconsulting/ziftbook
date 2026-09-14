import asyncio
import os
import threading
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app.db import SessionLocal
from app.jobs import Job, JobKind, enqueue
from app.worker import work


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
