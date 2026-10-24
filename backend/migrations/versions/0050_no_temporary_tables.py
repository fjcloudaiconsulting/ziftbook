"""Only the database owner creates temporary tables (ZB-31).

PUBLIC holds TEMPORARY on every new database, so ziftbook_app (and ziftbook_worker, through it)
could create a temporary table. One is searched before the real table of the same name and, on a
pooled connection, would outlive the request that made it. Neither role uses one.

Only the database's owner (or a superuser) can take a privilege from PUBLIC; anyone else's REVOKE
changes nothing. So this runs only where ziftbook_migrate owns the database, as it does every test
database (conftest creates each OWNER ziftbook_migrate). Where the admin role that created it owns
it, as in docker-compose and production, this changes nothing and logs a WARNING saying so
(`temporary tables left to bootstrap.sql`, logger app.migrations): bootstrap.sql, which runs as
that admin before the migrations, revokes it there. The owner keeps TEMPORARY either way, as owners
do. The check is made in Python, so the skip shows in the migrate job's log (app.logs keeps the
sqlalchemy loggers at WARNING, so a NOTICE from the SQL would not), and again in the SQL, which
stays safe to run on its own.

Revision ID: 0050
Revises: 0049
"""

import logging

from alembic import op
from sqlalchemy import text

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

logger = logging.getLogger("app.migrations")

# Owner here means having the owner role's privileges, as Postgres's own ownership check does.
OWNS_DATABASE = (
    "pg_has_role((SELECT datdba FROM pg_database WHERE datname = current_database()), 'USAGE')"
)
OWNER_ONLY = """
DO $$
BEGIN
  IF {owns} THEN
    EXECUTE format('{statement}', current_database());
  END IF;
END
$$
"""
REVOKE_TEMPORARY = OWNER_ONLY.format(
    owns=OWNS_DATABASE, statement="REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC"
)
GRANT_TEMPORARY = OWNER_ONLY.format(
    owns=OWNS_DATABASE, statement="GRANT TEMPORARY ON DATABASE %I TO PUBLIC"
)


def upgrade() -> None:
    owner_only(REVOKE_TEMPORARY, "upgrade")


def downgrade() -> None:
    owner_only(GRANT_TEMPORARY, "downgrade")


def owner_only(statement: str, step: str) -> None:
    owns, role = op.get_bind().execute(text(f"SELECT {OWNS_DATABASE}, current_user")).one()
    if not owns:
        logger.warning(
            "temporary tables left to bootstrap.sql",
            extra={"revision": revision, "step": step, "role": role},
        )
    op.execute(statement)
