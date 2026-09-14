import os
import uuid
from collections.abc import Iterator

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from psycopg.errors import ForeignKeyViolation, InsufficientPrivilege
from sqlalchemy import (
    Column,
    Engine,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    Table,
    Uuid,
    create_engine,
    select,
    text,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.db import SessionLocal, enable_tenant_isolation, metadata, tenant_context


class Base(DeclarativeBase):
    # Test-only tables, kept out of the app metadata.
    metadata = MetaData(naming_convention=metadata.naming_convention)


Table("tenants", Base.metadata, Column("id", Uuid, primary_key=True))


class Parent(Base):
    __tablename__ = "isolation_parents"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))


class Child(Base):
    __tablename__ = "isolation_children"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "parent_id"], ["isolation_parents.tenant_id", "isolation_parents.id"]
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    parent_id: Mapped[uuid.UUID]


@pytest.fixture
def tenants(migrate_engine: Engine, app_engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    with app_engine.begin() as conn:
        a, b = conn.scalars(
            text("INSERT INTO tenants (name) VALUES ('A'), ('B') RETURNING id")
        ).all()

    # Created the way a migration would: as ziftbook_migrate, each table isolated before the next
    # references it.
    with migrate_engine.begin() as conn:
        ctx = MigrationContext.configure(conn, opts={"target_metadata": Base.metadata})
        with Operations.context(ctx):
            for model in (Parent, Child):
                Base.metadata.tables[model.__tablename__].create(conn)
                enable_tenant_isolation(model.__tablename__)

    # One pooled connection, so consecutive sessions reuse the same database backend.
    engine = create_engine(os.environ["ZIF_DATABASE_URL"], pool_size=1, max_overflow=0)
    SessionLocal.configure(bind=engine)
    yield a, b
    SessionLocal.configure(bind=None)
    engine.dispose()

    with migrate_engine.begin() as conn:
        conn.execute(text("DROP TABLE isolation_children, isolation_parents"))
    with app_engine.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": a, "b": b})


def add_parent(tenant_id: uuid.UUID) -> uuid.UUID:
    with tenant_context(tenant_id) as session:
        parent = Parent(tenant_id=tenant_id)
        session.add(parent)
        session.flush()
        return parent.id


def test_a_tenant_cannot_read_another_tenants_rows(tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, b = tenants
    b_parent = add_parent(b)
    with tenant_context(a) as session:
        assert session.scalars(select(Parent)).all() == []
        assert session.get(Parent, b_parent) is None


def test_a_tenant_cannot_write_rows_for_another_tenant(
    tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, b = tenants
    with pytest.raises(DBAPIError) as error, tenant_context(a) as session:
        session.add(Parent(tenant_id=b))
    assert isinstance(error.value.orig, InsufficientPrivilege)


def test_a_tenant_cannot_reference_another_tenants_rows(
    tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, b = tenants
    b_parent = add_parent(b)
    with pytest.raises(DBAPIError) as error, tenant_context(a) as session:
        session.add(Child(tenant_id=a, parent_id=b_parent))
    assert isinstance(error.value.orig, ForeignKeyViolation)


def test_a_query_without_tenant_context_raises(
    tenants: tuple[uuid.UUID, uuid.UUID], app_engine: Engine
) -> None:
    add_parent(tenants[0])
    # A fresh connection, where app.tenant_id was never set.
    with Session(app_engine) as session, pytest.raises(DBAPIError):
        session.scalars(select(Parent)).all()


def test_a_reused_pooled_connection_serves_no_tenant(tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    add_parent(tenants[0])
    with SessionLocal() as session, pytest.raises(DBAPIError):
        session.scalars(select(Parent)).all()
