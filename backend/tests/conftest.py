import asyncio
import hashlib
import io
import ipaddress
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
from contextlib import contextmanager
from dataclasses import dataclass
from email import message_from_bytes
from email.policy import default as email_policy
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, cast

import boto3
import pytest
import requests
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from moto import mock_aws
from mypy_boto3_s3 import S3Client
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from PIL import Image
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import admin, auth, logs, mail, passwords, payments, stripe_gateway, tracing
from app.db import SessionLocal, tenant_context
from app.jobs import run_once
from app.worker import KINDS
from tests.stripe_fake import WEBHOOK_SECRET, FakeGateway

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
LOCAL_DATABASE_HOSTS = ("localhost", "127.0.0.1", "::1", "postgres")


def refuse_remote_database(url: str) -> None:
    """Exit before anything connects when the suite is aimed at a non-local database (ZB-11).

    The suite DROPs and CREATEs databases, so it only runs against the machine's own Postgres (or
    the CI service container, `postgres`). ZIF_TESTS_ALLOW_DB, set to the exact database name,
    allows one deliberate exception. Must run at import, before the rewrite and DROP below:
    pytest_configure fires after conftest is imported, so it would be too late. The URL is never
    printed: it holds a password.
    """
    parsed = make_url(url)
    if parsed.database and os.environ.get("ZIF_TESTS_ALLOW_DB") == parsed.database:
        return
    if not _targets_are_local(parsed):
        raise SystemExit(
            f"refusing to run the test suite against database {parsed.database!r} on host "
            f"{parsed.host!r}: it drops and recreates databases. Use a local Postgres, or set "
            "ZIF_TESTS_ALLOW_DB to that database name to allow it."
        )


def _targets_are_local(parsed: URL) -> bool:
    """Every place the driver would connect to is local: libpq lets ?host= override the URL host
    (comma-separated for several) and ?hostaddr= set the real address, so all of them count."""

    def values(key: str) -> list[str]:
        raw = parsed.query.get(key, ())
        return [v for item in ([raw] if isinstance(raw, str) else raw) for v in item.split(",")]

    hosts = values("host") or (parsed.host or "").split(",")
    if not all(h.startswith("/") or h in LOCAL_DATABASE_HOSTS for h in hosts):
        return False
    for addr in values("hostaddr"):
        try:
            if not ipaddress.ip_address(addr).is_loopback:
                return False
        except ValueError:
            return False
    return True


# No Turnstile secret here, so opt out explicitly as development does (ZIF-116): without it every
# public booking is refused. Tests of the refusal itself delete it.
os.environ.setdefault("ZIF_TURNSTILE_DISABLED", "true")
# A fixed admin TOTP key (base64 of 32 bytes), test only. Tests of a missing key delete it.
os.environ.setdefault("ZIF_TOTP_KEY", "dGVzdC1vbmx5LXRvdHAta2V5LW5vdC1hLXNlY3JldCE=")
for _guarded in ("ZIF_MIGRATE_DATABASE_URL", "ZIF_DATABASE_URL"):
    refuse_remote_database(os.environ[_guarded])
# Rate-limit keys unpeppered (ZB-25) whatever the developer's shell exports, so tests of the plain
# form never depend on it. Forced, like the Mailgun variables below; peppered tests set their own.
os.environ.pop("ZIF_RATE_LIMIT_KEY", None)
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
# runs). Worker databases are named after the base database (ziftbook -> ziftbook_gw0), so
# checkouts can share one Postgres instance by giving each its own base name (ziftbook_143 ->
# ziftbook_143_gw0). No WITH (FORCE) here: two suites on one base name can both claim its _gw0
# (worker ids are assigned independently per run), and FORCE would let the second DROP the first's
# database out from under it mid-run. Without FORCE that collision is a loud ObjectInUse refusal
# instead of a silent kill -- the rule is one base database name per checkout. All of it at
# import, not in a fixture:
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
# renders straight into a URL and into CREATE DATABASE, so "gw0?host=evil" would happily reinterpret
# the connection target. The base name renders into the same places, so it is held to plain
# identifier characters too.
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
    exported but empty PYTEST_XDIST_WORKER would otherwise aim the whole suite at `<base>_`. The
    base is capped at 50 characters: Postgres truncates identifiers to 63 bytes, and a truncated
    `<base>_gw10` could collide with `<base>_gw1`.

    Residual: a *serial* run (no worker id) still points at the base database (`ziftbook` is the
    one `make up` runs the app against) -- the original instance-wide false-green risk this ticket
    fixed for parallel runs survives, unchanged, for a serial one.
    """
    if not worker:
        return url
    parsed = make_url(url)
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,49}", parsed.database or ""):
        raise RuntimeError(f"database name {parsed.database!r} is not a plain identifier")
    return parsed.set(database=f"{parsed.database}_{worker}").render_as_string(hide_password=False)


def install() -> None:
    """Make this database an installed one (ZIF-70): one user, so needs_setup() is false and sign-up
    works. Setup tests use empty_install instead. As migrate: the app role can't read users."""
    engine = create_engine(os.environ["ZIF_MIGRATE_DATABASE_URL"], poolclass=NullPool)
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO users (id, email, name) VALUES (uuidv7(), 'installed@example.invalid',
            'Installed') ON CONFLICT (email) DO NOTHING
            """)
        )
    engine.dispose()


# Both roles must land on the one database this worker creates: split bases would send the app
# role to another checkout's live worker database.
if XDIST_WORKER and len({make_url(os.environ[name]).database for name in URL_NAMES}) != 1:
    raise RuntimeError("ZIF_MIGRATE_DATABASE_URL and ZIF_DATABASE_URL name different databases")
for _name in URL_NAMES:
    os.environ[_name] = worker_url(os.environ[_name], XDIST_WORKER)
if XDIST_WORKER:
    _database = make_url(os.environ["ZIF_MIGRATE_DATABASE_URL"]).database
    _admin = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with _admin.connect() as _conn:
        _conn.execute(text(f"DROP DATABASE IF EXISTS {_database}"))
        # OWNER is load-bearing: on PG15+ `public` is owned by pg_database_owner, and without it
        # alembic dies on alembic_version with InsufficientPrivilege (42501).
        _conn.execute(text(f"CREATE DATABASE {_database} OWNER ziftbook_migrate"))
    _admin.dispose()
    command.upgrade(Config(toml_file=str(API_DIR / "pyproject.toml")), "head")
    install()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """A worker drops the database it created above, after a green run only (ZIF-120).

    A red or interrupted run keeps it to inspect; the next run's DROP above replaces it anyway. This
    one name only, never a pattern: on a shared instance a LIKE '<base>_gw%' also matches another
    run's live databases. No WITH (FORCE), for the reason above.
    A connection still open to it raises ObjectInUse here, failing the run loudly rather than
    forcing the drop. Residuals: a red `-n 16` run followed by `-n 4` still strands gw4 to gw15; and
    two suites on one base name (against the rule above) give this DROP a second window to remove
    the other's database between its tests.
    """
    if XDIST_WORKER and exitstatus == pytest.ExitCode.OK:
        admin = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {_database}"))


# Mail (ZIF-151): the suite runs the production path, mail.py through the Mailgun SDK down to its
# own HTTP call, and records each message here instead of sending it. Forced, not setdefault: a
# developer's real key must never send from the suite. Subprocesses inherit the fake key but not
# the patch: one that sends mail would get a 401 from the real Mailgun, so keep them mail-free.
os.environ["ZIF_MAILGUN_API_KEY"] = "test-key"
os.environ["ZIF_MAILGUN_DOMAIN"] = "test.ziftbook.invalid"
os.environ.pop("ZIF_MAILGUN_REGION", None)
os.environ.pop("ZIF_MAILPIT_URL", None)
# Every Mailgun call this process made, oldest first; each one parsed from the MIME it carried.
MAILGUN_CALLS: list[dict[str, Any]] = []
# What the next Mailgun call for an address gets instead of a 200: a status code, or an exception
# to raise. By address, so a job running alongside in the same batch cannot take it.
MAILGUN_REPLIES: dict[str, int | Exception] = {}
_session_post = requests.Session.post


def _mailgun_post(self: requests.Session, url: str, *args: Any, **kwargs: Any) -> requests.Response:
    """requests.Session.post, which the SDK sends with, with Mailgun's hosts answered here.

    Patched for the whole process at import, not per test: a test's monkeypatch.undo() would
    otherwise drop it and send for real. An error reply quotes the address, as Mailgun's do, so a
    leak of the response into logs or spans shows."""
    if urllib.parse.urlsplit(url).hostname not in mail.MAILGUN_API.values():
        return _session_post(self, url, *args, **kwargs)
    reply = MAILGUN_REPLIES.pop(kwargs["data"]["to"], 200)
    raw: bytes = kwargs["files"]["message"][1]
    parsed = message_from_bytes(raw, policy=email_policy)
    text_part = parsed.get_body(("plain",))
    MAILGUN_CALLS.append(
        {
            "ID": str(uuid.uuid4()),
            "Status": reply if isinstance(reply, int) else type(reply).__name__,
            "URL": url,
            "Auth": tuple(kwargs["auth"]),
            "Timeout": kwargs["timeout"],
            "Form": dict(kwargs["data"]),
            "To": kwargs["data"]["to"],
            "Subject": parsed["Subject"],
            "Text": text_part.get_content() if text_part is not None else "",
            "Raw": raw.decode(),
            "Headers": {name: parsed.get_all(name) for name in parsed},
        }
    )
    if isinstance(reply, Exception):
        raise reply
    response = requests.Response()
    response.status_code = reply
    response.url = url
    response._content = json.dumps(
        {"id": "<test>", "message": "Queued. Thank you."}
        if reply < 400
        else {"message": f"rejected {kwargs['data']['to']}"}
    ).encode()
    return response


requests.Session.post = _mailgun_post  # type: ignore[method-assign,assignment]


@pytest.fixture
def mailgun_replies() -> Iterator[dict[str, int | Exception]]:
    """Set what the next Mailgun call for an address answers. One left unused fails the test: the
    send it was meant for never happened."""
    yield MAILGUN_REPLIES
    unused = dict(MAILGUN_REPLIES)
    MAILGUN_REPLIES.clear()
    assert not unused, unused


def sent_to(email: str) -> list[dict[str, Any]]:
    """Every message Mailgun accepted for exactly this address, newest first."""
    return [c for c in reversed(MAILGUN_CALLS) if c["To"] == email and c["Status"] == 200]


SHOWN_TICKETS: list[str] = []
REAL_SHOW_TICKET = admin.show_ticket


@pytest.fixture(autouse=True)
def _capture_shown_tickets(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """admin.show_ticket writes to /dev/tty, which a test run has not got: capture it instead."""
    SHOWN_TICKETS.clear()
    monkeypatch.setattr(admin, "show_ticket", SHOWN_TICKETS.append)
    yield


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
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.Formatter("json"))
    handler.addFilter(logs._add_context)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line]
    finally:
        root.removeHandler(handler)
        monkeypatch.delenv("ZIF_LOG_LEVEL", raising=False)
        monkeypatch.delenv("ZIF_LOG_FORMAT", raising=False)
        logs.configure()
        threading.excepthook = _pytest_thread_hook  # ditto, for finalizers still to come


@pytest.fixture
def spans() -> Iterator[Callable[[], list[ReadableSpan]]]:
    """Finished spans recorded since this fixture ran, oldest first."""
    SPAN_EXPORTER.clear()
    yield lambda: list(SPAN_EXPORTER.get_finished_spans())


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    """A reader on the global MeterProvider tracing.configure() registered: it sees only what is
    recorded after it was added. Removed (and shut down) afterwards, so readers never pile up."""
    provider = cast(MeterProvider, metrics.get_meter_provider())
    reader = InMemoryMetricReader()
    provider.add_metric_reader(reader)
    yield reader
    provider.remove_metric_reader(reader)


def points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


METRIC_KEYS = {
    "job.kind",
    "job.outcome",
    "email.template",
    "email.outcome",
    "booking.source",
    "booking.proven",
    "stripe.event",
    "stripe.result",
    "payment.kind",
    "payment.outcome",
    "db.client.connection.state",
    "db.client.connection.pool.name",
}
ID_LIKE = re.compile(r"@|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def closed(reader: InMemoryMetricReader) -> None:
    """Our metrics carry only the closed attribute set, and no value holds an id or an address."""
    data = reader.get_metrics_data()
    for resource_metrics in data.resource_metrics if data else []:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if not metric.name.startswith(("ziftbook.", "db.client.")):
                    continue
                for point in metric.data.data_points:
                    attributes = dict(point.attributes or {})
                    assert set(attributes) <= METRIC_KEYS, (metric.name, attributes)
                    for value in attributes.values():
                        assert not ID_LIKE.search(str(value)), (metric.name, attributes)


@pytest.fixture(scope="session")
def migrated() -> None:
    command.upgrade(Config(toml_file=str(API_DIR / "pyproject.toml")), "head")
    install()


@dataclass(frozen=True)
class EmptyInstall:
    """A freshly migrated database with no user yet: first-run setup is still to be done."""

    app_url: str
    migrate: Engine  # ziftbook_migrate
    app: Engine  # ziftbook_app


@pytest.fixture(scope="session")
def empty_database() -> Iterator[EmptyInstall]:
    """This worker's own empty-install database on the shared test Postgres (never a container),
    migrated to head and never install()ed. Dropped at the end of the session."""
    urls = {name: make_url(os.environ[name]) for name in URL_NAMES}
    name = f"{urls['ZIF_MIGRATE_DATABASE_URL'].database}_empty"
    # It renders into DROP DATABASE as the superuser: a plain identifier within 63 bytes only.
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise RuntimeError(f"database name {name!r} is not a plain identifier of 63 bytes or less")
    admin = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        conn.execute(text(f"CREATE DATABASE {name} OWNER ziftbook_migrate"))
    rendered = {
        key: url.set(database=name).render_as_string(hide_password=False)
        for key, url in urls.items()
    }
    saved = {key: os.environ[key] for key in URL_NAMES}
    os.environ.update(rendered)  # migrations/env.py reads the migrate URL from the environment
    try:
        command.upgrade(Config(toml_file=str(API_DIR / "pyproject.toml")), "head")
    finally:
        os.environ.update(saved)
    empty = EmptyInstall(
        app_url=rendered["ZIF_DATABASE_URL"],
        migrate=create_engine(rendered["ZIF_MIGRATE_DATABASE_URL"], poolclass=NullPool),
        app=create_engine(rendered["ZIF_DATABASE_URL"], poolclass=NullPool),
    )
    try:
        yield empty
    finally:
        empty.migrate.dispose()
        empty.app.dispose()
        with admin.connect() as conn:
            # FORCE: a test that failed mid-wait must not keep it. The name is this worker's alone.
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture
def empty_install(empty_database: EmptyInstall) -> Iterator[EmptyInstall]:
    """The empty-install database emptied again, with SessionLocal bound to its app role."""
    with empty_database.migrate.begin() as conn:
        conn.execute(
            text("""
            TRUNCATE users, tenants, password_credentials, platform_admins, admin_sessions,
              platform_audit_events, rate_limits, email_tokens, jobs CASCADE
            """)
        )
    SessionLocal.configure(bind=empty_database.app)
    yield empty_database
    SessionLocal.configure(bind=None)


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


@contextmanager
def engine_as_worker(url: URL) -> Iterator[Engine]:
    """A NullPool engine on url's database as ziftbook_worker, what the worker process connects as
    (ZB-08), disposed on exit.

    With ZIF_WORKER_PASSWORD, the password bootstrap.sql gave the role, or when it is unset or
    empty the local dev and CI one (docker-compose.yaml and ci.yml)."""
    password = os.environ.get("ZIF_WORKER_PASSWORD") or "ziftbook_worker"
    engine = create_engine(
        url.set(username="ziftbook_worker", password=password), poolclass=NullPool
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def worker_engine(migrated: None) -> Iterator[Engine]:
    """ziftbook_worker on the app role's database. Derived from ZIF_DATABASE_URL, which already
    names this xdist worker's database, so it lands on no other."""
    with engine_as_worker(make_url(os.environ["ZIF_DATABASE_URL"])) as engine:
        yield engine


@contextmanager
def jobs_as_the_worker() -> Iterator[None]:
    """SessionLocal bound to ziftbook_worker on the database it is bound to now, as the worker
    process runs jobs (only it may mint email tokens, ZB-08); the binding is restored after."""
    bind = SessionLocal.kw["bind"]
    with engine_as_worker(bind.url) as engine:
        SessionLocal.configure(bind=engine)
        try:
            yield
        finally:
            SessionLocal.configure(bind=bind)


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


def add_user(
    migrate_engine: Engine,
    email: str | None = None,
    locale: str | None = None,
    name: str | None = "Test Person",
) -> uuid.UUID:
    # As the migrate role: the app role creates no user (ZB-10). The id comes from here, as
    # complete_sign_up's does. name=None is an account from before ZIF-131: registration not
    # complete.
    user_id = uuid.uuid7()
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, locale, name) VALUES (:id, :email, :locale, :name)"
            ),
            {
                "id": user_id,
                "email": email or f"{user_id}@example.com",
                "locale": locale,
                "name": name,
            },
        )
    return user_id


def email_of(user_id: uuid.UUID) -> str:
    """The email add_user gives a user."""
    return f"{user_id}@example.com"


def issue_link(app_engine: Engine, purpose: str, email: str, locale: str = "en") -> str | None:
    """A request (the API's, as the app role) plus its email job (the worker's, the only role that
    mints, ZB-08): the token a link would carry, or None if nothing was sent."""
    token = secrets.token_urlsafe(32)
    with app_engine.begin() as conn:
        token_id = conn.scalar(
            text("SELECT start_email_token(:p, :e, :l)"), {"p": purpose, "e": email, "l": locale}
        )
    with engine_as_worker(app_engine.url) as worker, worker.begin() as conn:
        minted = conn.execute(
            text("SELECT * FROM mint_email_token(:id, :h)"),
            {"id": token_id, "h": hashlib.sha256(token.encode()).digest()},
        ).first()
    return token if minted and not minted.registered else None


def cookie_named(response: Response, name: str) -> Any:
    """The Set-Cookie morsel for one cookie: a sign-in sets two (ZIF-146), so headers["set-cookie"]
    joins them into a string no cookie parser reads."""
    for header in response.headers.get_list("set-cookie"):
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(header)
        if name in cookie:
            return cookie[name]
    raise KeyError(name)


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
    """Runs the queued jobs as the worker does, then returns the one message sent to email."""

    async def run_until_idle() -> None:
        while await run_once(KINDS):
            pass

    with jobs_as_the_worker():
        asyncio.run(run_until_idle())
    (sent,) = sent_to(email)
    return sent


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


@contextmanager
def migrate_tenant_context(migrate_engine: Engine, tenant_id: object) -> Iterator[Connection]:
    """tenant_context as the migrate role, for a write the app role may not make (forced row-level
    security binds the migrate role too). Commits on exit."""
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        yield conn


def delete_memberships(conn: Connection, tenant_ids: Iterable[object]) -> None:
    """The app role has no DELETE on memberships (ZB-10); fixtures remove them as the migrate role,
    one business at a time (forced row-level security)."""
    for tenant_id in tenant_ids:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(text("DELETE FROM memberships"))


def delete_payment_accounts(conn: Connection, tenant_ids: Iterable[object]) -> None:
    """The app role has no DELETE on payment_accounts (ZIF-158); fixtures remove them as the migrate
    role, one business at a time (forced row-level security)."""
    for tenant_id in tenant_ids:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(text("DELETE FROM payment_accounts"))


def delete_bookings(conn: Connection, tenant_ids: Iterable[object]) -> None:
    """The app role can delete neither booking_events, booking_links nor bookings; fixtures remove
    them as the migrate role, one business at a time (forced row-level security), payments, links
    and events first for the foreign key.

    booking_links and booking_payments may not exist: some test_migrations.py tests call this while
    temporarily downgraded below 0029 or 0044, where the table is gone.
    """
    has_links = conn.scalar(text("SELECT to_regclass('booking_links')")) is not None
    has_payments = conn.scalar(text("SELECT to_regclass('booking_payments')")) is not None
    for tenant_id in tenant_ids:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        if has_payments:
            conn.execute(text("DELETE FROM booking_payments"))
        if has_links:
            conn.execute(text("DELETE FROM booking_links"))
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
        return dict(session.execute(text("SELECT key, value FROM settings")).all())


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
    # As the migrate role: the app role inserts no membership (ZB-10); sign-up and invites do,
    # through SECURITY DEFINER functions.
    migrate = create_engine(os.environ["ZIF_MIGRATE_DATABASE_URL"], poolclass=NullPool)
    try:
        with migrate_tenant_context(migrate, tenant_id) as conn:
            conn.execute(
                text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r)"),
                {"t": tenant_id, "u": user_id, "r": role},
            )
    finally:
        migrate.dispose()


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


def end_membership(tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """End a membership as members.remove leaves it (ZIF-112), without the API. The person must
    hold no session in that business (the sessions foreign key) and not be its only owner."""
    with tenant_context(tenant_id) as session:
        session.execute(
            text("UPDATE memberships SET role = 'removed', removed_at = now() WHERE user_id = :u"),
            {"u": user_id},
        )


@pytest.fixture
def people(migrate_engine: Engine, bound: None) -> Iterator[People]:
    # As the migrate role: the app role creates neither businesses nor users (ZB-10).
    with migrate_engine.begin() as conn:
        a = conn.scalar(text("INSERT INTO tenants (name) VALUES ('a') RETURNING id"))
        b = conn.scalar(text("INSERT INTO tenants (name) VALUES ('b') RETURNING id"))
    people = People(
        a=a,
        b=b,
        only_a=add_user(migrate_engine),
        only_b=add_user(migrate_engine),
        both=add_user(migrate_engine, locale="nl"),
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
    # The app role can delete neither memberships nor users; the test cleans up as the migrate role.
    with migrate_engine.begin() as conn:
        delete_memberships(conn, (a, b))
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
        delete_payment_accounts(conn, (a, b))
        conn.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": a, "b": b})


S3_ENDPOINT = "https://s3.amazonaws.com"  # moto intercepts AWS hosts; a custom one needs an env var


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Client]:
    """Photo storage on moto, configured through the ZIF_S3_* variables; yields an admin client."""
    for name, value in {
        "ZIF_S3_ENDPOINT": S3_ENDPOINT,
        "ZIF_S3_PUBLIC_ENDPOINT": "http://localhost:8333",
        "ZIF_S3_ACCESS_KEY_ID": "dev",
        "ZIF_S3_SECRET_ACCESS_KEY": "dev-secret",
        "ZIF_S3_UPLOADS_BUCKET": "ziftbook-uploads",
        "ZIF_S3_MEDIA_BUCKET": "ziftbook-media",
        "ZIF_MEDIA_URL": "https://img.example.test/",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        # moto refuses CreateBucket in region "auto"; the app never creates buckets.
        admin = boto3.client("s3", endpoint_url=S3_ENDPOINT, region_name="us-east-1")
        admin.create_bucket(Bucket="ziftbook-uploads")
        admin.create_bucket(Bucket="ziftbook-media")
        yield admin


def image(fmt: str, size: tuple[int, int], **kw: object) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, fmt, **kw)
    return buf.getvalue()


def upload(
    s3: S3Client, tenant: uuid.UUID, data: bytes, content_type: str = "application/octet-stream"
) -> uuid.UUID:
    """The object a browser PUT would leave in the uploads bucket; returns its upload id."""
    upload_id = uuid.uuid4()
    s3.put_object(
        Bucket="ziftbook-uploads", Key=f"{tenant}/{upload_id}", Body=data, ContentType=content_type
    )
    return upload_id


@pytest.fixture
def fake_stripe(monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    """Payments on (ZIF-158): the three keys set and the gateway swapped for the in-memory one."""
    monkeypatch.setenv("ZIF_STRIPE_SECRET_KEY", "sk_test_not_a_real_key")
    monkeypatch.setenv("ZIF_STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("ZIF_STRIPE_PUBLISHABLE_KEY", "pk_test_not_a_real_key")
    fake = FakeGateway()
    monkeypatch.setattr(stripe_gateway, "gateway", fake)

    @contextmanager
    def counted(tenant_id: uuid.UUID) -> Iterator[Session]:
        fake.open_sessions += 1
        try:
            with tenant_context(tenant_id) as db:
                yield db
        finally:
            fake.open_sessions -= 1

    # The routes' dependencies (auth) and the payments code: where a session could be held open.
    for module in (auth, payments):
        monkeypatch.setattr(module, "tenant_context", counted)
    return fake
