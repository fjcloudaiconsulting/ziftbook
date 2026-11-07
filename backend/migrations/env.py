import logging
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager

from alembic import context
from opentelemetry.trace import SpanKind
from psycopg.errors import DeadlockDetected
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app import logs, tracing
from app.db import metadata

logs.configure()
tracing.configure("ziftbook-migrations")
logging.getLogger("app.migrations").info("migrations started", extra=logs.settings_fields())

# Any fixed value works; it only has to be the same for every migration run against this database.
MIGRATION_LOCK_ID = 7_428_211_906
# How often a run waiting its turn tries the lock again.
LOCK_POLL_SECONDS = 0.5
# The holder of the lock, for the waiting run's WARNING: a bigint advisory key is classid (high 32
# bits) and objid (low 32 bits), objsubid 1.
LOCK_HOLDER = text("""
SELECT pid FROM pg_locks
WHERE locktype = 'advisory' AND granted AND objsubid = 1
  AND classid = CAST(:id AS bigint) >> 32 AND objid = CAST(:id AS bigint) & 4294967295
  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
""")
# Every migration statement's lock wait (ZIF-34); 0072 puts the same value back after its builds.
SET_LOCK_TIMEOUT = text("SET lock_timeout = '5s'")
# ZIF-287: set when the migration being applied enters an autocommit_block, whose work commits as
# it goes, so a retry would re-run that part; cleared as each migration is stamped.
partly_committed = False


def migration_stamped(*_: object, **__: object) -> None:
    global partly_committed
    partly_committed = False


# Read directly, not via set_main_option: URL-encoded passwords contain "%",
# which config interpolation mangles. hide_parameters: a failed statement's message would
# otherwise carry its values into the logs.
engine = create_engine(
    os.environ["ZIF_MIGRATE_DATABASE_URL"], poolclass=NullPool, hide_parameters=True
)

with engine.connect() as connection:
    # Session-level lock: a second migration run waits here instead of racing. It is
    # released when the connection closes. Polled, with a commit between tries, never waited for
    # in pg_advisory_lock: a statement blocked on the lock holds a snapshot, and CREATE INDEX
    # CONCURRENTLY in the run holding it (0072) waits for every older snapshot, so the two would
    # deadlock (ZIF-235). Between tries this run holds no snapshot and no transaction.
    # A waiting run shows as idle (last query COMMIT), never as a lock waiter, so it says who it
    # waits for: after lock_wait_warning_seconds, then every lock_wait_repeat_seconds (Alembic
    # config attributes, for tests; 30 s and 300 s).
    first_warning = context.config.attributes.get("lock_wait_warning_seconds", 30)
    repeat = context.config.attributes.get("lock_wait_repeat_seconds", 300)
    started = time.monotonic()
    warn_at = started + first_warning
    while not connection.execute(
        text("SELECT pg_try_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID}
    ).scalar():
        if time.monotonic() >= warn_at:
            holder = connection.scalar(LOCK_HOLDER, {"id": MIGRATION_LOCK_ID})
            logging.getLogger("app.migrations").warning(
                "migration lock held by another run",
                extra={"holder_pid": holder, "waited_seconds": int(time.monotonic() - started)},
            )
            warn_at = time.monotonic() + repeat
        connection.commit()
        time.sleep(LOCK_POLL_SECONDS)
    # After the advisory lock, so a run waiting its turn waits unbounded; before any migration, so
    # one stuck behind an app transaction fails in 5s instead of queueing every query on that
    # table behind its own lock (ZIF-34). SET, not SET LOCAL: it must outlive this commit and
    # every per-migration transaction after it.
    connection.execute(SET_LOCK_TIMEOUT)
    connection.commit()

    context.configure(
        connection=connection,
        target_metadata=metadata,
        transaction_per_migration=True,
        on_version_apply=migration_stamped,
    )
    migration_context = context.get_context()
    plain_block = migration_context.autocommit_block

    @contextmanager
    def tracked_block() -> Iterator[None]:
        global partly_committed
        partly_committed = True
        with plain_block():
            yield

    # Migrations reach it as op.get_context().autocommit_block(), this same object.
    migration_context.autocommit_block = tracked_block  # type: ignore[method-assign]

    with tracing.span("migrations upgrade", SpanKind.INTERNAL, {}):
        retry = False
        try:
            context.run_migrations()
        except DBAPIError as error:
            # ZIF-287: an autovacuum that starts between a migration's GRANT on a table and its
            # ALTER of it waits for this transaction while the ALTER waits for the vacuum, and
            # Postgres fails the migration. Its transaction rolled back and the ones before it
            # committed, so run_migrations resumes from alembic_version: once, only towards an
            # absolute target ("-2" would count from where the first try stopped), and never into
            # an autocommit_block that already committed part of the migration. The DETAIL a
            # client gets holds pids, lock modes and OIDs, never query text or row values.
            target = migration_context.opts.get("destination_rev")
            absolute = isinstance(target, str) and re.fullmatch(r"\w+", target)
            if not isinstance(error.orig, DeadlockDetected) or not absolute:
                raise
            detail = error.orig.diag.message_detail
            if partly_committed:
                logging.getLogger("app.migrations").warning(
                    "migration not retried after a deadlock",
                    extra={"detail": detail, "reason": "its autocommit_block committed part of it"},
                )
                raise
            logging.getLogger("app.migrations").warning(
                "migration retried after a deadlock", extra={"detail": detail}
            )
            retry = True
        # Outside the except, so a second failure is reported alone, not chained to the first.
        if retry:
            # Back to the connection's startup settings, then this run's lock_timeout again.
            connection.execute(text("RESET ALL"))
            connection.execute(SET_LOCK_TIMEOUT)
            connection.commit()
            context.run_migrations()
