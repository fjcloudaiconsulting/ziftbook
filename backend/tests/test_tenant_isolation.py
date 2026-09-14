import uuid
from collections.abc import Iterator

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from psycopg.errors import ForeignKeyViolation, InsufficientPrivilege
from sqlalchemy import Engine, ForeignKeyConstraint, MetaData, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.db import SessionLocal, enable_tenant_isolation, metadata, tenant_context


class Base(DeclarativeBase):
    # Test-only tables, kept out of the app metadata.
    metadata = MetaData(naming_convention=metadata.naming_convention)


class Parent(Base):
    __tablename__ = "isolation_parents"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    tenant_id: Mapped[uuid.UUID]


class Child(Base):
    __tablename__ = "isolation_children"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "parent_id"], ["isolation_parents.tenant_id", "isolation_parents.id"]
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    tenant_id: Mapped[uuid.UUID]
    parent_id: Mapped[uuid.UUID]


@pytest.fixture
def tenants(migrate_engine: Engine, app_engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    # Created the way a migration would: as ziftbook_migrate, each table isolated before the next
    # references it.
    with migrate_engine.begin() as conn:
        ctx = MigrationContext.configure(conn, opts={"target_metadata": Base.metadata})
        with Operations.context(ctx):
            for model in (Parent, Child):
                Base.metadata.tables[model.__tablename__].create(conn)
                enable_tenant_isolation(model.__tablename__)

    SessionLocal.configure(bind=app_engine)
    yield uuid.uuid4(), uuid.uuid4()
    SessionLocal.configure(bind=None)
    with migrate_engine.begin() as conn:
        conn.execute(text("DROP TABLE isolation_children, isolation_parents"))


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
