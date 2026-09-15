import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app.db import SessionLocal, tenant_context

API_DIR = Path(__file__).parent.parent

# Local dev defaults (docker-compose.yaml / bootstrap.sql); CI sets the same values explicitly.
os.environ.setdefault(
    "ZIF_MIGRATE_DATABASE_URL",
    "postgresql+psycopg://ziftbook_migrate:ziftbook_migrate@localhost:5432/ziftbook",
)
os.environ.setdefault(
    "ZIF_DATABASE_URL",
    "postgresql+psycopg://ziftbook_app:ziftbook_app@localhost:5432/ziftbook",
)
# Mailpit from docker-compose.yaml.
os.environ.setdefault("ZIF_SMTP_HOST", "localhost")
os.environ.setdefault("ZIF_SMTP_PORT", "1025")
os.environ.setdefault("ZIF_SMTP_STARTTLS", "false")


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


@dataclass(frozen=True)
class People:
    a: uuid.UUID  # tenant
    b: uuid.UUID  # tenant
    only_a: uuid.UUID  # user, worker in a
    only_b: uuid.UUID  # user, worker in b
    both: uuid.UUID  # user, owner in a and worker in b


def add_user(engine: Engine, email: str | None = None, locale: str | None = None) -> uuid.UUID:
    # The id comes from the app: a new user is invisible to its own policy, so RETURNING would fail.
    user_id = uuid.uuid7()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email, locale) VALUES (:id, :email, :locale)"),
            {"id": user_id, "email": email or f"{user_id}@example.com", "locale": locale},
        )
    return user_id


def add_membership(tenant_id: uuid.UUID, user_id: uuid.UUID, role: str = "worker") -> None:
    with tenant_context(tenant_id) as session:
        session.execute(
            text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r)"),
            {"t": tenant_id, "u": user_id, "r": role},
        )


@pytest.fixture
def people(app_engine: Engine, migrate_engine: Engine) -> Iterator[People]:
    SessionLocal.configure(bind=app_engine)
    with app_engine.begin() as conn:
        a = conn.scalar(text("INSERT INTO tenants (name) VALUES ('a') RETURNING id"))
        b = conn.scalar(text("INSERT INTO tenants (name) VALUES ('b') RETURNING id"))
    people = People(
        a=a,
        b=b,
        only_a=add_user(app_engine),
        only_b=add_user(app_engine),
        both=add_user(app_engine, locale="nl"),
    )
    add_membership(a, people.only_a)
    add_membership(b, people.only_b)
    add_membership(a, people.both, "owner")
    add_membership(b, people.both)
    yield people
    for tenant_id in (a, b):
        with tenant_context(tenant_id) as session:
            session.execute(text("DELETE FROM memberships"))
    # The app role can't delete users; the test cleans up as the migrate role.
    with migrate_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM password_credentials WHERE user_id IN (:x, :y, :z)"),
            {"x": people.only_a, "y": people.only_b, "z": people.both},
        )
        conn.execute(
            text("DELETE FROM users WHERE id IN (:x, :y, :z)"),
            {"x": people.only_a, "y": people.only_b, "z": people.both},
        )
        conn.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": a, "b": b})
    SessionLocal.configure(bind=None)
