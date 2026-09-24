import asyncio
import hashlib
import io
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from opentelemetry import trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs._internal import ReadableLogRecord
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app import auth, logs, passwords, tracing
from app.db import SessionLocal, tenant_context
from app.jobs import run_once
from app.worker import KINDS

# A developer's own collector must never receive test spans holding test emails, and a ratio
# sampler would make span-count assertions flaky: strip every OTel env var before anything
# (including the import-time migrate below, which imports migrations/env.py) can configure a real
# exporter or sampler from it.
for _otel_name in [n for n in os.environ if n.startswith("OTEL_")]:
    del os.environ[_otel_name]
tracing.configure("ziftbook-api")
SPAN_EXPORTER = InMemorySpanExporter()
# tracing.configure() always builds an SDK TracerProvider (never the abstract API base), so this
# cast just tells mypy what set_tracer_provider already guarantees at runtime.
cast(TracerProvider, trace.get_tracer_provider()).add_span_processor(
    SimpleSpanProcessor(SPAN_EXPORTER)
)

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
# Admin connection for CREATE/DROP DATABASE: ziftbook_migrate has rolcreatedb = f and must not be
# granted it -- it is a production role. Derived from ZIF_MIGRATE_DATABASE_URL, never hardcoded, so
# it always aims at the instance the suite is already talking to: a literal localhost:5432 would
# point DROP DATABASE at an unrelated server the moment ZIF_MIGRATE_DATABASE_URL does not.
# render_as_string(hide_password=False), never str(): str(URL) renders the password as "***".
ADMIN_DATABASE_URL = (
    make_url(os.environ["ZIF_MIGRATE_DATABASE_URL"])
    .set(database="postgres", username="ziftbook", password="ziftbook")
    .render_as_string(hide_password=False)
)

# One database per xdist worker (ZIF-111). Dropped, recreated and migrated every run, not reused: a
# reused database keeps its rows, and `jobs` is not tenant-scoped (ZIF-110), so mailed()'s run_once
# drains every earlier run's backlog and the suite silently degrades 2.7x (65s -> 178s over three
# runs). No WITH (FORCE) here: two suites on one instance can both claim ziftbook_gw0 (worker ids
# are assigned independently per run), and FORCE would let the second DROP the first's database out
# from under it mid-run. Without FORCE that collision is a loud ObjectInUse refusal instead of a
# silent kill -- the rule is one Postgres instance per checkout. All of it at import, not in a
# fixture:
#   - migrations/env.py:20-22 reads ZIF_MIGRATE_DATABASE_URL at module level, and
#     test_logs/test_api_database spawn subprocesses that inherit os.environ;
#   - test_logs.py's `python -m alembic current` does not depend on `migrated`, so a worker that
#     reaches it first would find no database at all (also true of any -k or single-file subset);
#   - nothing has opened an engine yet, so the DROP cannot fail with ObjectInUse against a
#     connection this worker itself already holds.
XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER")
# pytest-xdist only ever sets this to gw<N>. Anything else is some other export (an operator's
# shell, a CI matrix, a parent pytest) that happens to share the name, and it must not silently
# steer this suite at a per-"worker" database -- a serial run with PYTEST_XDIST_WORKER=prod would
# otherwise drop and recreate ziftbook_prod. It also closes an injection path: an unvalidated value
# renders straight into a URL (make_url(url).set(database=f"ziftbook_{worker}")), so
# "gw0?host=evil" would happily reinterpret the connection target.
if XDIST_WORKER is not None and not re.fullmatch(r"gw\d+", XDIST_WORKER):
    raise RuntimeError(
        f"PYTEST_XDIST_WORKER={XDIST_WORKER!r} is not a pytest-xdist worker id (expected gw<N>); "
        "refusing to guess which database this run owns"
    )
URL_NAMES = ("ZIF_MIGRATE_DATABASE_URL", "ZIF_DATABASE_URL")
# Captured before the rewrite below, so a test can compare against them independently of whatever
# the rewrite did.
ORIGINAL_URLS = {name: os.environ[name] for name in URL_NAMES}


def worker_url(url: str, worker: str | None) -> str:
    """That worker's own database, or the URL untouched when there is no worker.

    The guard is on the worker id being set to something, not on the variable merely existing: an
    exported but empty PYTEST_XDIST_WORKER would otherwise aim the whole suite at `ziftbook_`.

    Residual: a *serial* run (no worker id) still points at the shared `ziftbook` that `make up`
    runs the app against -- the original instance-wide false-green risk this ticket fixed for
    parallel runs survives, unchanged, for a serial one.
    """
    if not worker:
        return url
    return make_url(url).set(database=f"ziftbook_{worker}").render_as_string(hide_password=False)


for _name in URL_NAMES:
    os.environ[_name] = worker_url(os.environ[_name], XDIST_WORKER)
if XDIST_WORKER:
    _database = f"ziftbook_{XDIST_WORKER}"
    _admin = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with _admin.connect() as _conn:
        _conn.execute(text(f"DROP DATABASE IF EXISTS {_database}"))
        # OWNER is load-bearing: on PG15+ `public` is owned by pg_database_owner, and without it
        # alembic dies on alembic_version with InsufficientPrivilege (42501).
        _conn.execute(text(f"CREATE DATABASE {_database} OWNER ziftbook_migrate"))
    _admin.dispose()
    command.upgrade(Config(toml_file=str(API_DIR / "pyproject.toml")), "head")
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


def _rebuild(item: ReadableLogRecord) -> dict[str, Any]:
    """The exported record, in the same shape as a parsed JSON line (test 2's parity fence).
    datetime math, never integer math (which flakes on millisecond rounding, test 9)."""
    record = item.log_record
    assert record.timestamp is not None
    ts = datetime.fromtimestamp(record.timestamp / 1e9, UTC).isoformat(timespec="milliseconds")
    # The SDK freezes list/dict attribute values into tuples for immutability; round-trip through
    # JSON so a rebuilt "kinds": ("a", "b") compares equal to the line's "kinds": ["a", "b"].
    attributes = json.loads(json.dumps(dict(record.attributes or {})))
    fields: dict[str, Any] = {
        "ts": ts.replace("+00:00", "Z"),
        "level": record.severity_text,
        "msg": record.body,
        **attributes,
    }
    if record.trace_id:
        fields["trace_id"] = format(record.trace_id, "032x")
    if record.span_id:
        fields["span_id"] = format(record.span_id, "016x")
    return fields


class _CaptureHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """The plain stdout-shaped capture handler, plus one flag per line recording whether that
    record was exportable -- appended inside emit(), under Handler.handle()'s lock, so the flag
    list and the written line always advance together even from a worker thread."""

    def __init__(self, stream: io.StringIO) -> None:
        super().__init__(stream)
        self.flags: list[bool] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.flags.append(logs._exportable(record))
        super().emit(record)


@pytest.fixture
def log_lines(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """Captured JSON log records at ZIF_LOG_LEVEL (DEBUG unless the test reconfigures), parsed one
    dict per line. Also attaches logs.OtlpHandler over an in-memory provider (no env, no network)
    and, on every call, asserts the one-to-one parity between every exported record and its
    flagged JSON line (test 2)."""
    monkeypatch.setenv("ZIF_LOG_LEVEL", "DEBUG")
    logs.configure()
    threading.excepthook = _pytest_thread_hook  # configure() just reassigned it; put it back
    stream = io.StringIO()
    handler = _CaptureHandler(stream)
    handler.setFormatter(logs.Formatter("json"))
    handler.addFilter(logs._add_context)
    root = logging.getLogger()
    root.addHandler(handler)

    provider = LoggerProvider(resource=Resource.create(), shutdown_on_exit=False)
    exporter = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    otlp_handler = logs.OtlpHandler(provider)
    root.addHandler(otlp_handler)
    try:

        def lines() -> list[dict[str, Any]]:
            parsed = [json.loads(line) for line in stream.getvalue().splitlines() if line]
            flagged = [p for p, exportable in zip(parsed, handler.flags, strict=True) if exportable]
            exported = [_rebuild(item) for item in exporter.get_finished_logs()]
            assert flagged == exported
            return parsed

        lines.records = exporter.get_finished_logs  # type: ignore[attr-defined]
        yield lines
    finally:
        root.removeHandler(handler)
        root.removeHandler(otlp_handler)
        provider.shutdown()
        monkeypatch.delenv("ZIF_LOG_LEVEL", raising=False)
        monkeypatch.delenv("ZIF_LOG_FORMAT", raising=False)
        logs.configure()
        threading.excepthook = _pytest_thread_hook  # ditto, for finalizers still to come


@pytest.fixture
def spans() -> Iterator[Callable[[], list[ReadableSpan]]]:
    """Finished spans recorded since this fixture ran, oldest first."""
    SPAN_EXPORTER.clear()
    yield lambda: list(SPAN_EXPORTER.get_finished_spans())


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
    """Wait until that many sessions block on a lock in THIS database: the interleaving the test
    needs.

    Both tables are load-bearing; neither half can be dropped (ZIF-111).
    pg_locks alone cannot scope by database: a row-lock waiter's `transactionid` lock carries
    pg_locks.database = NULL, so filtering on it makes the count permanently zero. Unscoped, any
    ungranted lock anywhere in the instance -- another database, a second suite, a stray psql --
    satisfies the wait, the helper returns early and the test asserts against the wrong state.
    pg_stat_activity alone cannot see the wait cross-role: watching an app-role waiter from a
    migrate-role connection, wait_event_type/state/query are all masked. Only pid, datname and
    usename survive, which is exactly what the join needs.

    Residual: `NOT l.granted` counts a waiter blocked on ANY lock type in this database, not just
    the row lock a given call site cares about -- so an advisory-lock waiter would also satisfy a
    row-lock call site. Harmless under xdist because tests within one worker's private database
    run sequentially, so nothing else is ever mid-wait here at the same time.
    """
    for _ in range(100):
        with engine.connect() as conn:
            waiting = conn.scalar(
                text(
                    "SELECT count(DISTINCT l.pid) FROM pg_locks l "
                    "JOIN pg_stat_activity a ON a.pid = l.pid "
                    "WHERE NOT l.granted AND a.datname = current_database()"
                )
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
