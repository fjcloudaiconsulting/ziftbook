import os

from alembic import context
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app import logs
from app.db import metadata

logs.configure()

# Any fixed value works; it only has to be the same for every migration run against this database.
MIGRATION_LOCK_ID = 7_428_211_906

# Read directly, not via set_main_option: URL-encoded passwords contain "%",
# which config interpolation mangles. hide_parameters: a failed statement's message would
# otherwise carry its values into the logs.
engine = create_engine(
    os.environ["ZIF_MIGRATE_DATABASE_URL"], poolclass=NullPool, hide_parameters=True
)

with engine.connect() as connection:
    # Session-level lock: a second migration run waits here instead of racing. It is
    # released when the connection closes. Commit so Alembic can open its own
    # transaction per migration.
    connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
    connection.commit()

    context.configure(
        connection=connection,
        target_metadata=metadata,
        transaction_per_migration=True,
    )
    context.run_migrations()
