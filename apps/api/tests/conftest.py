import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import NullPool

API_DIR = Path(__file__).parent.parent

# Local dev defaults (compose.yaml / bootstrap.sql); CI sets the same values explicitly.
os.environ.setdefault(
    "ZIF_MIGRATE_DATABASE_URL",
    "postgresql+psycopg://ziftbook_migrate:ziftbook_migrate@localhost:5432/ziftbook",
)
os.environ.setdefault(
    "ZIF_DATABASE_URL",
    "postgresql+psycopg://ziftbook_app:ziftbook_app@localhost:5432/ziftbook",
)


@pytest.fixture(scope="session")
def migrated() -> None:
    command.upgrade(Config(toml_file=str(API_DIR / "pyproject.toml")), "head")


@pytest.fixture
def migrate_engine(migrated: None) -> Iterator[Engine]:
    engine = create_engine(os.environ["ZIF_MIGRATE_DATABASE_URL"], poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture
def app_engine(migrated: None) -> Iterator[Engine]:
    engine = create_engine(os.environ["ZIF_DATABASE_URL"], poolclass=NullPool)
    yield engine
    engine.dispose()
