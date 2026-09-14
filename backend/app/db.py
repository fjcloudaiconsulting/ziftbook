from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Connection, MetaData, event, text
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

# Deterministic constraint names, so migrations can reference (and later drop) them by name.
metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

# Unbound, so importing this module needs no configuration (migrations/env.py imports it).
# Each process binds it once: SessionLocal.configure(bind=create_engine(url, pool_pre_ping=True)).
SessionLocal = sessionmaker()


@event.listens_for(SessionLocal, "after_begin")
def _set_tenant(session: Session, transaction: SessionTransaction, connection: Connection) -> None:
    tenant_id = session.info.get("tenant_id")
    if tenant_id is not None:
        # Transaction-local (is_local = true): the setting ends with the transaction, so a pooled
        # connection never carries a tenant into its next use. Never a plain SET.
        connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )


@contextmanager
def tenant_context(tenant_id: UUID) -> Iterator[Session]:
    """One transaction scoped to a tenant, for jobs and webhooks.

    Commits on exit and rolls back on error. Row-level security reads the tenant from this
    transaction; outside one, queries on tenant tables raise.
    """
    with SessionLocal(info={"tenant_id": tenant_id}) as session, session.begin():
        yield session
