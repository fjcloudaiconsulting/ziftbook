import asyncio
import hashlib
import json
import os
import secrets
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app import auth, passwords
from app.db import SessionLocal, tenant_context
from app.jobs import run_once
from app.worker import KINDS

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
MAILPIT = f"http://{os.environ['ZIF_SMTP_HOST']}:8025"


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


PASSWORD = "lavender-harbour-19"
EXPIRE = "UPDATE email_tokens SET expires_at = now() - interval '1 second' WHERE token_hash = :h"


@pytest.fixture
def bound(app_engine: Engine) -> Iterator[None]:
    """SessionLocal bound to the app role for the test; fixtures that need it depend on this."""
    SessionLocal.configure(bind=app_engine)
    yield
    SessionLocal.configure(bind=None)


def new_client(app: FastAPI) -> TestClient:
    # A fresh IPv6 /64 per client, so per-IP rate limits never carry over between tests or runs.
    address = f"2001:db8:{secrets.randbelow(65536):x}:{secrets.randbelow(65536):x}::1"
    return TestClient(
        app, base_url="https://testserver", client=(address, 1), raise_server_exceptions=False
    )


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


def email_of(user_id: uuid.UUID) -> str:
    """The email add_user gives a user."""
    return f"{user_id}@example.com"


def issue_link(app_engine: Engine, purpose: str, email: str, locale: str = "en") -> str | None:
    """A request plus its email job: the token a link would carry, or None if nothing was sent."""
    token = secrets.token_urlsafe(32)
    with app_engine.begin() as conn:
        token_id = conn.scalar(
            text("SELECT start_email_token(:p, :e, :l)"), {"p": purpose, "e": email, "l": locale}
        )
        minted = conn.execute(
            text("SELECT * FROM mint_email_token(:id, :h)"),
            {"id": token_id, "h": hashlib.sha256(token.encode()).digest()},
        ).first()
    return token if minted and not minted.registered else None


def fresh_email() -> str:
    return f"{uuid.uuid4()}@example.com"


def live(app_engine: Engine, token: str, purpose: str) -> bool:
    with app_engine.connect() as conn:
        result: bool = conn.scalar(
            text("SELECT email_token_live(:h, :p)"), {"h": auth.hash_token(token), "p": purpose}
        )
    return result


def jobs_for(migrate_engine: Engine, email: str) -> int:
    """Email jobs queued for an email address."""
    with migrate_engine.connect() as conn:
        count: int = conn.scalar(
            text("""
            SELECT count(*) FROM jobs j JOIN email_tokens t ON j.payload->>'token_id' = t.id::text
            WHERE t.email = :e AND j.kind = 'email.token'
            """),
            {"e": email},
        )
    return count


def mailed(email: str) -> dict[str, Any]:
    """Runs the queued jobs, then returns the one message Mailpit got for email."""

    async def run_until_idle() -> None:
        while await run_once(KINDS):
            pass

    asyncio.run(run_until_idle())
    query = urllib.parse.urlencode({"query": f"to:{email}"})
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/search?{query}", timeout=5) as found:
        (sent,) = json.load(found)["messages"]
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/message/{sent['ID']}", timeout=5) as body:
        message: dict[str, Any] = json.load(body)
    return message


def token_in(message: dict[str, Any]) -> str:
    """The token in the fragment of the message's link."""
    token: str = message["Text"].split("#")[1].split()[0]
    return token


@pytest.fixture
def no_hashing(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Passwords that would have been hashed; nothing is."""
    hashed: list[str] = []

    def spy(password: str) -> str:
        hashed.append(password)
        return ""

    monkeypatch.setattr(passwords, "hash_password", spy)
    return hashed


def add_password(migrate_engine: Engine, user_id: uuid.UUID, password: str) -> None:
    with migrate_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO password_credentials VALUES (:u, :h)"),
            {"u": user_id, "h": passwords.hash_password(password)},
        )


def add_membership(tenant_id: uuid.UUID, user_id: uuid.UUID, role: str = "worker") -> None:
    with tenant_context(tenant_id) as session:
        session.execute(
            text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r)"),
            {"t": tenant_id, "u": user_id, "r": role},
        )


@pytest.fixture
def people(app_engine: Engine, migrate_engine: Engine, bound: None) -> Iterator[People]:
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
