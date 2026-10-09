"""The background worker: python -m app.worker. Runs from the backend image with another command."""

import asyncio
import logging
import signal
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation
from sqlalchemy import create_engine

from app import bookings, jobs, logs, mail, tracing
from app.config import MailSettings, WorkerSettings
from app.db import SessionLocal
from app.jobs import JobKind, run_once

# Registered job kinds. A new kind ships one release before anything enqueues it.
KINDS: dict[str, JobKind] = {
    "email.send": JobKind(mail.send, timeout=30, grace=timedelta(hours=24)),
    # A pending request lives an hour; a link mailed later than this would be mostly spent.
    "email.token": JobKind(mail.send_token, timeout=30, grace=timedelta(minutes=30)),
    # The owner's page shows the invite as sent-but-waiting until then; a resend starts over.
    "email.invite": JobKind(mail.send_invite, timeout=30, grace=timedelta(hours=24)),
    # ZIF-53. Grace covers the reminder, whose due_at is already 24h out; the status gate at send
    # time (re-checked on every run, never withdrawn on enqueue) is what keeps a stale booking
    # email from going out.
    "email.booking": JobKind(mail.send_booking, timeout=30, grace=timedelta(hours=24)),
    # ZIF-117. The link lives 24 hours from the hold; the handler sends nothing once it is dead.
    "email.booking_verify": JobKind(
        mail.send_booking_verify, timeout=30, grace=timedelta(hours=24)
    ),
}


def _queue_gauge(index: int) -> Callable[[CallbackOptions], list[Observation]]:
    """Runs on the export thread, one small indexed query per instrument. A database error
    propagates: the SDK logs it and that export has no queue point, a gap being the honest value."""

    def observe(options: CallbackOptions) -> list[Observation]:
        # Tests import this module with no session bound; there is no database to ask.
        if SessionLocal.kw.get("bind") is None:
            return []
        waiting = jobs.queue(list(KINDS))
        # Every kind, 0 when empty: a drained kind reports 0 instead of going stale.
        return [
            Observation(waiting.get(kind, (0, 0.0))[index], {"job.kind": kind}) for kind in KINDS
        ]

    return observe


# Per worker: with several, query max(), not sum().
_meter = metrics.get_meter("ziftbook")
_meter.create_observable_up_down_counter(
    "ziftbook.job.queue.size", callbacks=[_queue_gauge(0)], unit="{job}"
)
_meter.create_observable_gauge(
    "ziftbook.job.queue.oldest_age", callbacks=[_queue_gauge(1)], unit="s"
)

POLL_SECONDS = 30
# ZIF-122: settle expired pending bookings; housekeeping, so it runs on a timer, not every poll.
SWEEP_SECONDS = 300

logger = logging.getLogger("app.worker")


def ping(url: str) -> None:
    urllib.request.urlopen(url, timeout=5).close()


async def work(kinds: dict[str, JobKind], healthcheck_url: str | None, stop: asyncio.Event) -> None:
    """Run due jobs until nothing is due, sweep when due, ping, then wait POLL_SECONDS or until
    stopped."""
    swept: float | None = None  # monotonic time of the last SUCCESSFUL sweep
    while not stop.is_set():
        try:
            while await run_once(kinds) and not stop.is_set():
                pass
            # After the drain, so emails go first. A failing sweep skips the ping (alerts) and
            # retries next poll, since the mark is only set on success.
            if swept is None or time.monotonic() - swept >= SWEEP_SECONDS:
                await asyncio.to_thread(bookings.sweep)
                swept = time.monotonic()
        except Exception:
            # No ping: an iteration that keeps failing (e.g. the database is down) raises the alert.
            logger.exception("worker iteration failed")
        else:
            if healthcheck_url:
                try:
                    await asyncio.to_thread(ping, healthcheck_url)
                except OSError as error:
                    # The URL is a secret-ish check id.
                    logger.warning("healthcheck ping failed", extra={"error": type(error).__name__})
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
    mail_settings = MailSettings()
    # Never the Mailgun key or the healthcheck URL. "off": email jobs fail until the key is set.
    logger.info(
        "worker started",
        extra={
            **logs.settings_fields(),
            "kinds": sorted(KINDS),
            "mail": mail.transport(mail_settings),
            "mailgun_domain": mail_settings.mailgun_domain,
            "mailgun_region": mail_settings.mailgun_region,
        },
    )
    await work(KINDS, settings.healthcheck_url, stop)
    logger.info("worker stopped")


def main() -> None:
    logs.configure()
    tracing.configure("ziftbook-worker")
    settings = WorkerSettings()
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        hide_parameters=True,  # no statement values (emails, token hashes) in logs
        # 20 executor threads (handlers, claim, sweep) and the export thread's queue query.
        pool_size=21,
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
