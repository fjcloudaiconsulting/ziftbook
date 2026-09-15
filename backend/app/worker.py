"""The background worker: python -m app.worker. Runs from the backend image with another command."""

import asyncio
import logging
import signal
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import create_engine

from app import mail
from app.config import WorkerSettings
from app.db import SessionLocal
from app.jobs import JobKind, run_once

# Registered job kinds. A new kind ships one release before anything enqueues it.
KINDS: dict[str, JobKind] = {
    "email.send": JobKind(mail.send, timeout=30, grace=timedelta(hours=24)),
    # A pending request lives an hour; a link mailed later than this would be mostly spent.
    "email.token": JobKind(mail.send_token, timeout=30, grace=timedelta(minutes=30)),
}

POLL_SECONDS = 30

logger = logging.getLogger("app.worker")


def ping(url: str) -> None:
    urllib.request.urlopen(url, timeout=5).close()


async def work(kinds: dict[str, JobKind], healthcheck_url: str | None, stop: asyncio.Event) -> None:
    """Run due jobs until nothing is due, ping, then wait POLL_SECONDS or until stopped."""
    while not stop.is_set():
        try:
            while await run_once(kinds) and not stop.is_set():
                pass
        except Exception:
            # No ping: an iteration that keeps failing (e.g. the database is down) raises the alert.
            logger.exception("worker iteration failed")
        else:
            if healthcheck_url:
                try:
                    await asyncio.to_thread(ping, healthcheck_url)
                except OSError as error:
                    logger.warning("healthcheck ping failed: %r", error)
        try:
            await asyncio.wait_for(stop.wait(), POLL_SECONDS)
        except TimeoutError:
            pass


async def serve(settings: WorkerSettings) -> None:
    loop = asyncio.get_running_loop()
    # Handlers run in these threads; the default pool is too small on a one-CPU pod.
    loop.set_default_executor(ThreadPoolExecutor(20))
    stop = asyncio.Event()
    # SIGTERM (Kubernetes, compose) finishes the current batch, at most one job timeout, then exits.
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    logger.info("worker started with kinds %s", sorted(KINDS))
    await work(KINDS, settings.healthcheck_url, stop)
    logger.info("worker stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = WorkerSettings()
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        hide_parameters=True,  # no statement values (emails, token hashes) in logs
        pool_size=21,  # 20 handler threads and the claim
        max_overflow=0,
        # Hard limits for handlers that outlive their timeout: their threads can't be killed.
        connect_args={
            "options": "-c statement_timeout=30s -c idle_in_transaction_session_timeout=60s"
        },
    )
    SessionLocal.configure(bind=engine)
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
