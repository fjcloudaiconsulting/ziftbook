import logging
import os
import time

from alembic import context
from opentelemetry.trace import SpanKind
from sqlalchemy import create_engine, text
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
    while not connection.execute(
        text("SELECT pg_try_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID}
    ).scalar():
        connection.commit()
        time.sleep(LOCK_POLL_SECONDS)
    # After the advisory lock, so a run waiting its turn waits unbounded; before any migration, so
    # one stuck behind an app transaction fails in 5s instead of queueing every query on that
    # table behind its own lock (ZIF-34). SET, not SET LOCAL: it must outlive this commit and
    # every per-migration transaction after it.
    connection.execute(text("SET lock_timeout = '5s'"))
    connection.commit()

    context.configure(
        connection=connection,
        target_metadata=metadata,
        transaction_per_migration=True,
    )
    with tracing.span("migrations upgrade", SpanKind.INTERNAL, {}):
        context.run_migrations()
