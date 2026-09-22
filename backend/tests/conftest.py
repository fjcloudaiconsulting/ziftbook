import asyncio
import hashlib
import io
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app import auth, logs, passwords
from app.db import SessionLocal, tenant_context
from app.jobs import run_once
from app.worker import KINDS

# pytest's own threading.excepthook (installed in its pytest_configure, which every
# conftest.py's pytest_configure hooks run alongside): captured with trylast so it runs after
# pytest's, and before collection imports app.main and calls logs.configure(), which reassigns
# threading.excepthook process-wide for the rest of the session.
_pytest_thread_hook: Callable[[threading.ExceptHookArgs], object] = threading.excepthook


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    global _pytest_thread_hook
    _pytest_thread_hook = threading.excepthook


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


@pytest.fixture(autouse=True)
def _keep_pytest_thread_hook() -> Iterator[None]:
    """Put pytest's threading.excepthook back before and after every test.

    logs.configure() (an app.main import at collection, the worker, migrations) reassigns
    threading.excepthook process-wide, so without this an uncaught exception in a thread would
    silently stop failing tests. This only covers the test boundaries: log_lines below calls
    configure() itself during the test, and restores the hook right after so the test body still
    runs under pytest's own hook.
    """
    threading.excepthook = _pytest_thread_hook
    yield
    threading.excepthook = _pytest_thread_hook


@pytest.fixture
def log_lines(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """Captured JSON log records at ZIF_LOG_LEVEL (DEBUG unless the test reconfigures), parsed one
    dict per line."""
    monkeypatch.setenv("ZIF_LOG_LEVEL", "DEBUG")
    logs.configure()
    threading.excepthook = _pytest_thread_hook  # configure() just reassigned it; put it back
    stream = io.StringIO()
    handler = logs.stream_handler(stream, "json")
    root = logging.getLogger()
    root.addHandler(handler)
    try:

        def lines() -> list[dict[str, Any]]:
            return [json.loads(line) for line in stream.getvalue().splitlines() if line]

        yield lines
    finally:
        root.removeHandler(handler)
        monkeypatch.delenv("ZIF_LOG_LEVEL", raising=False)
        monkeypatch.delenv("ZIF_LOG_FORMAT", raising=False)
        logs.configure()
        threading.excepthook = _pytest_thread_hook  # ditto, for finalizers still to come


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


def fresh_address() -> str:
    """A new IPv6 /64, so per-IP rate limits never carry over between tests or runs. No zero groups:
    Postgres would print 2001:db8:abc:0::1 as 2001:db8:abc::1."""
    return f"2001:db8:{secrets.randbelow(65535) + 1:x}:{secrets.randbelow(65535) + 1:x}::1"


def new_client(app: FastAPI, address: str | None = None) -> TestClient:
    return TestClient(
        app,
        base_url="https://testserver",
        client=(address or fresh_address(), 1),
        raise_server_exceptions=False,
    )


def wait_until_blocked(engine: Engine, backends: int) -> None:
    """Wait until that many sessions block on a lock: the interleaving the test needs."""
    for _ in range(100):
        with engine.connect() as conn:
            waiting = conn.scalar(
                text("SELECT count(DISTINCT pid) FROM pg_locks WHERE NOT granted")
            )
        if waiting >= backends:
            return
        time.sleep(0.05)
    raise AssertionError(f"{backends} sessions never waited on a lock")


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


def delete_services(conn: Connection, tenant_ids: Iterable[object]) -> None:
    """The app role can't delete services; fixtures remove them as the migrate role, one business at
    a time (forced row-level security)."""
    for tenant_id in tenant_ids:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(text("DELETE FROM services"))


def delete_clients(conn: Connection, tenant_ids: Iterable[object]) -> None:
    """The app role can delete neither consents nor clients; fixtures remove them as the migrate
    role, one business at a time (forced row-level security), consents first for the foreign key."""
    for tenant_id in tenant_ids:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(text("DELETE FROM consents"))
        conn.execute(text("DELETE FROM clients"))


def delete_bookings(conn: Connection, tenant_ids: Iterable[object]) -> None:
    """The app role can delete neither booking_events nor bookings; fixtures remove them as the
    migrate role, one business at a time (forced row-level security), events first for the foreign
    key."""
    for tenant_id in tenant_ids:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(text("DELETE FROM booking_events"))
        conn.execute(text("DELETE FROM bookings"))


def events(migrate_engine: Engine, **match: Any) -> list[dict[str, Any]]:
    """Audit events read as an operator, oldest first, matched on the given columns."""
    where = " AND ".join(
        f"ip = cast(:{column} AS inet)" if column == "ip" else f"{column} = :{column}"
        for column in match
    )
    with migrate_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.audit_review = 'on'"))
        rows = conn.execute(
            text(f"""
            SELECT action, tenant_id, actor_user_id, target, details, host(ip) AS ip, user_agent
            FROM audit_events WHERE {where} ORDER BY id
            """),
            match,
        ).mappings()
        return [dict(row) for row in rows]


def failing(monkeypatch: pytest.MonkeyPatch, module: Any, name: str) -> None:
    """Make one function raise, to check what its caller leaves behind."""

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"{name} failed")

    monkeypatch.setattr(module, name, broken)


def put_settings(client: TestClient, body: Any) -> Response:
    return client.put("/api/settings", json=body)


def saved_settings(tenant_id: uuid.UUID) -> dict[str, Any]:
    with tenant_context(tenant_id) as session:
        return dict(session.execute(text("SELECT key, value FROM settings")).tuples().all())


def save_setting(tenant_id: uuid.UUID, key: str, value: Any) -> None:
    """A saved value straight in the table, including one the registry would refuse today."""
    with tenant_context(tenant_id) as session:
        session.execute(
            text("""
            INSERT INTO settings (tenant_id, key, value)
            VALUES (current_setting('app.tenant_id')::uuid, :key, CAST(:value AS jsonb))
            """),
            {"key": key, "value": json.dumps(value)},
        )


def signed_in(app: FastAPI, tenant_id: uuid.UUID, user_id: uuid.UUID) -> TestClient:
    """A client holding a session in that business, as that person."""
    with tenant_context(tenant_id) as session:
        token = auth.create(session, user_id, ip=None, user_agent=None)
    client = new_client(app)
    client.cookies.set(auth.COOKIE, token)
    return client


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


def member_id(tenant_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    with tenant_context(tenant_id) as session:
        found: uuid.UUID = session.scalar(
            text("SELECT id FROM memberships WHERE user_id = :u"), {"u": user_id}
        )
    return found


def set_role(tenant_id: uuid.UUID, user_id: uuid.UUID, role: str) -> None:
    with tenant_context(tenant_id) as session:
        session.execute(
            text("UPDATE memberships SET role = :r WHERE user_id = :u"), {"u": user_id, "r": role}
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
    # First, and as the migrate role: bookings reference memberships (no ON DELETE), clients and
    # services, so they must go before any of the three. The app role has no DELETE on them.
    with migrate_engine.begin() as conn:
        delete_bookings(conn, (a, b))
    for tenant_id in (a, b):
        with tenant_context(tenant_id) as session:
            # Not cascaded by deleting memberships: opening_hours references tenants, not a
            # membership (ZIF-105).
            session.execute(text("DELETE FROM opening_hours"))
            session.execute(text("DELETE FROM settings"))  # they reference the business
            session.execute(text("DELETE FROM invites"))
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
        delete_clients(conn, (a, b))
        delete_services(conn, (a, b))
        conn.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": a, "b": b})
