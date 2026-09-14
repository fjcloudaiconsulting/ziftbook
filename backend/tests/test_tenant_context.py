import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text

from app.db import SessionLocal, tenant_context

SETTING = text("SELECT current_setting('app.tenant_id', true)")
BACKEND_PID = text("SELECT pg_backend_pid()")


@pytest.fixture(autouse=True)
def single_connection_pool(migrated: None) -> Iterator[None]:
    # One pooled connection, so every session below reuses the same database backend.
    engine = create_engine(os.environ["ZIF_DATABASE_URL"], pool_size=1, max_overflow=0)
    SessionLocal.configure(bind=engine)
    yield
    SessionLocal.configure(bind=None)
    engine.dispose()


def test_tenant_context_sets_the_tenant_for_its_transaction() -> None:
    tenant_id = uuid.uuid4()
    with tenant_context(tenant_id) as session:
        assert session.scalar(SETTING) == str(tenant_id)


def test_a_reused_pooled_connection_carries_no_tenant() -> None:
    with tenant_context(uuid.uuid4()) as session:
        first_pid = session.scalar(BACKEND_PID)

    with SessionLocal() as session:
        assert session.scalar(BACKEND_PID) == first_pid
        assert session.scalar(SETTING) == ""


def test_savepoints_keep_the_tenant() -> None:
    tenant_id = uuid.uuid4()
    with tenant_context(tenant_id) as session:
        with session.begin_nested():
            assert session.scalar(SETTING) == str(tenant_id)
        assert session.scalar(SETTING) == str(tenant_id)
