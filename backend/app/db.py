from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from alembic import op
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Counter, Observation
from sqlalchemy import Connection, MetaData, event, text
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker
from sqlalchemy.pool import QueuePool

from app import logs

# Deterministic constraint names, so migrations can reference (and later drop) them by name. All
# columns are in uq and fk names, so composite (tenant_id, ...) constraints never collide.
metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
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


def count_after_commit(
    session: Session, counter: Counter, attributes: dict[str, str] | None = None
) -> None:
    """Add 1 once this session's transaction commits; nothing if it rolls back. Call it in the
    transaction, outside any savepoint that may roll back."""
    session.info.setdefault("counts", []).append((counter, attributes))


@event.listens_for(SessionLocal, "after_commit")
def _count(session: Session) -> None:
    if session.in_nested_transaction():  # a released savepoint: nothing is committed yet
        return
    for counter, attributes in session.info.pop("counts", ()):
        counter.add(1, attributes)


@event.listens_for(SessionLocal, "after_transaction_end")
def _forget(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:  # the root ended: a commit already took them, a rollback drops
        session.info.pop("counts", None)


POOL = {"db.client.connection.pool.name": "default"}


def _pool() -> QueuePool | None:
    """The one engine each process binds to SessionLocal (NullPool in tests, unbound: nothing)."""
    pool = getattr(SessionLocal.kw.get("bind"), "pool", None)
    return pool if isinstance(pool, QueuePool) else None


def _connections(options: CallbackOptions) -> list[Observation]:
    pool = _pool()
    if pool is None:
        return []
    return [
        Observation(pool.checkedout(), POOL | {"db.client.connection.state": "used"}),
        Observation(pool.checkedin(), POOL | {"db.client.connection.state": "idle"}),
    ]


def _pending(options: CallbackOptions) -> list[Observation]:
    pool = _pool()
    # ponytail: SQLAlchemy has no public waiter count; this reads QueuePool's and CPython's
    # internals (SQLAlchemy 2.1, Python 3.14). The pool gauge test fails if either moves.
    if pool is None:
        return []
    return [Observation(len(pool._pool.not_empty._waiters), POOL)]  # type: ignore[attr-defined]


_meter = metrics.get_meter("ziftbook")
_meter.create_observable_up_down_counter(
    "db.client.connection.count", callbacks=[_connections], unit="{connection}"
)
_meter.create_observable_up_down_counter(
    "db.client.connection.pending_requests", callbacks=[_pending], unit="{request}"
)


@contextmanager
def tenant_context(tenant_id: UUID) -> Iterator[Session]:
    """One transaction scoped to a tenant, for jobs and webhooks.

    Commits on exit and rolls back on error. Row-level security reads the tenant from this
    transaction; outside one, queries on tenant tables raise.
    """
    with (
        logs.bound(tenant_id=str(tenant_id)),
        SessionLocal(info={"tenant_id": tenant_id}) as session,
        session.begin(),
    ):
        yield session


def join_tenant(session: Session, tenant_id: UUID) -> None:
    """Scope the rest of this transaction to a tenant found inside it, such as a session's or a new
    business's. Transaction-local, like tenant_context."""
    session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)}
    )


def enable_tenant_isolation(table: str, *, referenced: bool = True) -> None:
    """Isolate a tenant-owned table by tenant. Call it in the migration that creates the table.

    The table needs `id` and `tenant_id` (REFERENCES tenants (id)) columns. Every reference to it
    from another tenant-owned table must be a composite foreign key, (tenant_id, x_id) REFERENCES
    table (tenant_id, id): Postgres checks foreign keys with row-level security bypassed, so a
    plain one would let a row point at another tenant's data. A link table nothing references has
    no id: pass referenced=False.
    """
    tenant_matches = "tenant_id = current_setting('app.tenant_id')::uuid"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    # FORCE applies the policy to the table owner (ziftbook_migrate) too.
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({tenant_matches}) WITH CHECK ({tenant_matches})"
    )
    # The target of the composite foreign keys above. A link table (service_workers) is never
    # referenced and has no id: its primary key already leads with tenant_id.
    if referenced:
        op.create_unique_constraint(None, table, ["tenant_id", "id"])
