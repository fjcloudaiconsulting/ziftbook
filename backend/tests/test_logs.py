"""app.logs: one setup for the API, the worker and migrations. See CONTRIBUTING "Logging"."""

import asyncio
import contextvars
import json
import logging
import logging.config
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, MutableMapping
from datetime import timedelta
from typing import Any

import pytest
import uvicorn.config
from fastapi import FastAPI
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app import jobs, logs
from app.db import SessionLocal, tenant_context
from app.jobs import Job, JobKind, enqueue, run_once
from app.main import create_app
from tests.conftest import API_DIR, People, fresh_email, new_client, signed_in

Lines = Callable[[], list[dict[str, Any]]]


def _raised(kind: type[BaseException], *args: object) -> BaseException:
    try:
        raise kind(*args)
    except BaseException as error:  # noqa: BLE001 - the point is to capture it
        return error


def _boom() -> None:
    raise RuntimeError("boom")


def _with_boom_route(app: FastAPI) -> FastAPI:
    app.add_api_route("/api/boom", _boom, methods=["GET"], tags=["test"])
    return app


async def _raw_request(
    app: FastAPI, method: str, path: str, headers: list[tuple[bytes, bytes]]
) -> tuple[int, dict[str, str]]:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("2001:db8:1234:5678::1", 12345),
        "server": ("testserver", 443),
    }
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        messages.append(dict(message))

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    headers_out = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in start["headers"]}
    return start["status"], headers_out


def raw_request(
    app: FastAPI, method: str, path: str, headers: list[tuple[bytes, bytes]]
) -> tuple[int, dict[str, str]]:
    return asyncio.run(_raw_request(app, method, path, headers))


# 1: an invalid ZIF_LOG_LEVEL or ZIF_LOG_FORMAT stops the process, without echoing the value.
@pytest.mark.parametrize(
    ("var", "value"), [("ZIF_LOG_LEVEL", "LOUD-7f3a"), ("ZIF_LOG_FORMAT", "xml-7f3a")]
)
def test_an_invalid_log_setting_exits_cleanly_and_never_echoes_the_value(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    monkeypatch.setenv(var, value)

    with pytest.raises(SystemExit) as raised:
        logs.configure()

    message = str(raised.value)
    assert var in message
    assert value not in message


# 2: the JSON shape, key order included.
def test_a_json_record_has_exactly_the_expected_keys_in_order(log_lines: Lines) -> None:
    logger = logging.getLogger("app.tests.logs")
    with logs.bound(request_id="r", tenant_id="t"):
        logger.error("m", extra={"k": 1}, exc_info=_raised(ValueError, "x"))

    line = log_lines()[-1]

    assert list(line.keys()) == [
        "ts",
        "level",
        "logger",
        "msg",
        "request_id",
        "tenant_id",
        "k",
        "exc",
    ]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", line["ts"])


# extra/context keys never overwrite the standard head fields.
def test_extra_and_context_keys_never_overwrite_the_standard_fields(log_lines: Lines) -> None:
    logger = logging.getLogger("app.tests.logs")
    with logs.bound(msg="evil-context"):
        logger.info("m", extra={"level": "CRITICAL", "logger": "evil-extra"})

    line = log_lines()[-1]
    assert line["msg"] == "m"
    assert line["level"] == "INFO"
    assert line["logger"] == "app.tests.logs"


# B5: ts is always UTC, whatever the host's local timezone. A subprocess (its own TZ, never
# touching this process's) means no time.tzset() leak into other tests.
def test_ts_is_utc_whatever_the_host_zone() -> None:
    output = _run_uncaught_script(
        "import json\n"
        "import logging\n"
        "import app.logs as logs\n"
        "record = logging.makeLogRecord({'msg': 'm', 'created': 0.0})\n"
        "print(json.loads(logs.Formatter('json').format(record))['ts'])\n",
        {"TZ": "America/Sao_Paulo"},
    )

    assert output.strip() == "1970-01-01T00:00:00.000Z"


# 3: exc never holds an exception's message, only its class and frames.
def test_the_exc_chain_never_holds_a_message(log_lines: Lines) -> None:
    secret_a = "".join(["sec", "ret-a-", uuid.uuid4().hex[:8]])
    secret_b = "".join(["sec", "ret-b-", uuid.uuid4().hex[:8]])
    secret_c = "".join(["sec", "ret-c-", uuid.uuid4().hex[:8]])
    try:
        try:
            raise KeyError(secret_b)
        except KeyError as cause:
            error = ValueError(secret_a)
            error.add_note(secret_c)  # notes never reach the log either
            raise error from cause
    except ValueError as error:
        logging.getLogger("app.tests.logs").error("m", exc_info=error)

    line = log_lines()[-1]

    assert [link["type"] for link in line["exc"]] == ["builtins.ValueError", "builtins.KeyError"]
    frames = [f for link in line["exc"] for f in link["frames"]]
    assert any(f["function"] == "test_the_exc_chain_never_holds_a_message" for f in frames)
    dumped = json.dumps(line)
    assert secret_a not in dumped
    assert secret_b not in dumped
    assert secret_c not in dumped


# 4: a database error's identifiers, never the row values it quotes.
def test_a_database_error_never_quotes_the_row(migrate_engine: Engine, log_lines: Lines) -> None:
    email = fresh_email()

    with pytest.raises(IntegrityError) as raised, migrate_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (email) VALUES (:email), (:email)"), {"email": email})

    assert email in str(raised.value)  # the raw error does quote it

    logging.getLogger("app.tests.logs").error("m", exc_info=raised.value)
    line = log_lines()[-1]
    link = next(link for link in line["exc"] if "sqlstate" in link)

    assert link["sqlstate"] == "23505"
    assert link["table"] == "users"
    assert link["constraint"] == "uq_users_email"
    assert email not in json.dumps(line)


# 5: format() never raises and always stays JSON, even for bad %-args.
def test_bad_percent_style_args_still_produce_one_parseable_json_line(log_lines: Lines) -> None:
    logging.getLogger("app.tests.logs").info("%d", "x")

    line = log_lines()[-1]

    assert line["msg"] == "%d"


# 6: a non-string message logs its class, never str().
def test_a_non_string_message_logs_the_class_not_str(log_lines: Lines) -> None:
    secret = "".join(["sec", "ret-", uuid.uuid4().hex[:8]])

    logging.getLogger("app.tests.logs").warning(ValueError(secret))

    line = log_lines()[-1]
    assert line["msg"] == "ValueError"
    assert secret not in json.dumps(line)


# 7: text format is exactly one line per record, with control characters escaped.
def test_text_format_is_one_line_with_control_characters_escaped() -> None:
    record = logging.LogRecord(
        name="app.tests.logs",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="a\nb",
        args=(),
        exc_info=None,
    )
    record.k = "x\ry"

    line = logs.Formatter("text").format(record)

    assert len(line.splitlines()) == 1
    assert "a\\nb" in line
    assert "k=x\\ry" in line


# 8: the request id is echoed when valid, replaced with a fresh one when not.
def test_a_valid_request_id_is_echoed_and_matches_the_access_line(log_lines: Lines) -> None:
    app = create_app()

    status, headers = raw_request(app, "GET", "/api/healthz", [(b"x-request-id", b"abc-123.X:y_z")])

    assert status == 200
    assert headers["x-request-id"] == "abc-123.X:y_z"
    line = next((rec for rec in reversed(log_lines()) if rec.get("msg") == "access"), None)
    assert line is not None
    assert line["request_id"] == "abc-123.X:y_z"


@pytest.mark.parametrize("given", ["a\nb", "abc\n", "x" * 65, "", "a b", "é"])
def test_an_invalid_request_id_is_replaced_with_a_fresh_one(log_lines: Lines, given: str) -> None:
    app = create_app()

    status, headers = raw_request(
        app, "GET", "/api/healthz", [(b"x-request-id", given.encode("latin-1"))]
    )

    assert status == 200
    request_id = headers["x-request-id"]
    assert request_id != given
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    line = next((rec for rec in reversed(log_lines()) if rec.get("msg") == "access"), None)
    assert line is not None
    assert line["request_id"] == request_id


# 9: the access line carries the route template, never the path or the query string.
def test_the_access_line_has_the_route_template_never_the_path_or_query(
    people: People, log_lines: Lines
) -> None:
    app = create_app()
    owner = signed_in(app, people.a, people.both)
    member_id = uuid.uuid4()

    owner.get(f"/api/members/{member_id}?q=zz-secret")

    line = next(rec for rec in reversed(log_lines()) if rec["msg"] == "access")
    assert line["route"] == "/api/members/{member_id}"
    dumped = json.dumps(line)
    assert str(member_id) not in dumped
    assert "zz-secret" not in dumped


# 10 & 11: exactly one access line per request, at the right status, 500 included with the
# request id shared between the response header and the error log line.
@pytest.mark.parametrize(
    ("method", "path", "headers", "status", "route"),
    [
        ("GET", "/api/healthz", {}, 200, "/api/healthz"),
        ("GET", "/api/session", {}, 401, "/api/session"),
        ("GET", "/api/nope", {}, 404, "unmatched"),
        ("POST", "/api/healthz", {"content-type": "text/plain"}, 415, "unmatched"),
        ("GET", "/api/boom", {}, 500, "/api/boom"),
    ],
)
def test_exactly_one_access_line_per_request(
    log_lines: Lines,
    method: str,
    path: str,
    headers: dict[str, str],
    status: int,
    route: str,
) -> None:
    app = _with_boom_route(create_app())
    client = new_client(app)

    response = client.request(method, path, headers=headers)

    assert response.status_code == status
    access_lines = [rec for rec in log_lines() if rec["msg"] == "access"]
    assert len(access_lines) == 1
    assert access_lines[0]["status"] == status
    assert access_lines[0]["route"] == route


def test_the_500_response_and_its_log_line_share_the_request_id(log_lines: Lines) -> None:
    app = _with_boom_route(create_app())
    client = new_client(app)

    response = client.get("/api/boom")

    assert response.status_code == 500
    request_id = response.headers["x-request-id"]
    error_lines = [
        rec for rec in log_lines() if rec["level"] == "ERROR" and rec["msg"] == "unhandled error"
    ]
    assert len(error_lines) == 1
    assert error_lines[0]["request_id"] == request_id
    assert "exc" in error_lines[0]


# B6: the access line has method, status and duration_ms.
def test_access_line_has_method_status_and_duration_in_ms(log_lines: Lines) -> None:
    app = create_app()

    def slow() -> None:
        time.sleep(0.06)

    app.add_api_route("/api/slow", slow, methods=["GET"], tags=["test"], status_code=204)
    response = new_client(app).get("/api/slow")

    assert response.status_code == 204
    line = next((rec for rec in log_lines() if rec.get("route") == "/api/slow"), None)
    assert line is not None
    assert line["method"] == "GET"
    assert line["status"] == 204
    assert 60 <= line.get("duration_ms", -1) < 5000


# B8: a request never inherits a stale context left over by unrelated code (a job, another
# request in the same event loop iteration). raw_request's fake receive() signals a disconnect
# up front, which makes Starlette's BaseHTTPMiddleware re-raise instead of returning a response.
def test_a_request_never_inherits_a_stale_context(log_lines: Lines) -> None:
    app = _with_boom_route(create_app())

    with logs.bound(job_id="stale", tenant_id="stale"), pytest.raises(RuntimeError):
        raw_request(app, "GET", "/api/boom", [])

    line = next((rec for rec in log_lines() if rec.get("msg") == "unhandled error"), None)
    assert line is not None
    assert "job_id" not in line
    assert "tenant_id" not in line


# 12: healthz only logs an access line at DEBUG; /api/session logs one at both levels.
def test_healthz_is_debug_only_while_session_always_logs(
    log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app()
    client = new_client(app)

    client.get("/api/healthz")
    healthz_lines = [rec for rec in log_lines() if rec.get("route") == "/api/healthz"]
    assert len(healthz_lines) == 1
    assert healthz_lines[0]["level"] == "DEBUG"

    client.get("/api/session")
    last_session = next(
        (rec for rec in reversed(log_lines()) if rec.get("route") == "/api/session"), None
    )
    assert last_session is not None
    assert last_session["level"] == "INFO"

    before = len(log_lines())
    monkeypatch.setenv("ZIF_LOG_LEVEL", "INFO")
    logs.configure()
    client.get("/api/healthz")
    assert len(log_lines()) == before  # nothing new: no access line at INFO

    client.get("/api/session")
    last_session = next(
        (rec for rec in reversed(log_lines()) if rec.get("route") == "/api/session"), None
    )
    assert last_session is not None
    assert last_session["level"] == "INFO"


# 13: tenant_id on the access line matches the session's tenant; absent when anonymous.
def test_tenant_id_on_the_access_line_matches_the_session(people: People, log_lines: Lines) -> None:
    app = create_app()
    owner = signed_in(app, people.a, people.both)

    owner.get("/api/session")
    line = next(rec for rec in reversed(log_lines()) if rec.get("route") == "/api/session")
    assert line["tenant_id"] == str(people.a)

    new_client(app).get("/api/session")
    anon_line = next(rec for rec in reversed(log_lines()) if rec.get("route") == "/api/session")
    assert "tenant_id" not in anon_line


# 14: tenant_context binds tenant_id inside, and always restores after, even when entered and
# exited in two different copied contexts (what FastAPI does for a sync dependency).
def test_tenant_context_binds_and_restores_in_the_same_context(
    people: People, log_lines: Lines
) -> None:
    logger = logging.getLogger("app.tests.logs")
    before = dict(logs.CONTEXT.get({}))

    with tenant_context(people.a):
        logger.info("inside")
    logger.info("outside")

    lines = log_lines()
    assert lines[-2]["tenant_id"] == str(people.a)
    assert "tenant_id" not in lines[-1]
    assert dict(logs.CONTEXT.get({})) == before


def test_tenant_context_restores_after_being_entered_and_exited_in_different_contexts(
    people: People,
) -> None:
    cm = tenant_context(people.a)

    contextvars.Context().run(cm.__enter__)
    contextvars.Context().run(cm.__exit__, None, None, None)  # must not raise


def test_tenant_context_restores_after_an_error(people: People) -> None:
    with pytest.raises(RuntimeError), tenant_context(people.a):
        raise RuntimeError("x")

    assert "tenant_id" not in logs.CONTEXT.get({})


# A real exc_info rendered as text: the class shows, the message never does.
def test_text_format_with_an_error_never_holds_the_message() -> None:
    sentinel = "".join(["sec", "ret-", uuid.uuid4().hex[:8]])
    try:
        raise ValueError(sentinel)
    except ValueError as error:
        record = logging.LogRecord(
            "app.tests.logs",
            logging.ERROR,
            __file__,
            1,
            "m",
            (),
            (ValueError, error, error.__traceback__),
        )
        text_line = logs.Formatter("text").format(record)

    assert sentinel not in text_line
    assert "builtins.ValueError" in text_line


# 15: job context matches its own job; a failing handler logs the class, never %r.
def test_job_context_matches_its_own_job_and_a_failure_never_leaks(
    people: People, bound: None, app_engine: Engine, log_lines: Lines
) -> None:
    logger = logging.getLogger("app.tests.logs.jobs")
    sentinel = "".join(["sec", "ret-", uuid.uuid4().hex[:8]])
    seen: dict[str, dict[str, str]] = {}

    def record_context(job: Job) -> None:
        seen[str(job.id)] = dict(logs.CONTEXT.get({}))
        logger.info("job ran")

    def fail(job: Job) -> None:
        raise RuntimeError(sentinel)

    key = uuid.uuid4()
    with SessionLocal.begin() as session:
        enqueue(session, "test.logs.ok", f"test.logs.ok:{key}:t", {}, tenant_id=people.a)
        enqueue(session, "test.logs.ok", f"test.logs.ok:{key}:none", {})
        enqueue(session, "test.logs.fail", f"test.logs.fail:{key}", {})

    kinds = {
        "test.logs.ok": JobKind(record_context, 5, timedelta(hours=1)),
        "test.logs.fail": JobKind(fail, 5, timedelta(hours=1)),
    }
    try:
        asyncio.run(run_once(kinds))

        with SessionLocal() as session:
            rows = session.execute(
                text("SELECT id, dedupe_key FROM jobs WHERE dedupe_key = ANY(:keys)"),
                {
                    "keys": [
                        f"test.logs.ok:{key}:t",
                        f"test.logs.ok:{key}:none",
                        f"test.logs.fail:{key}",
                    ]
                },
            )
            job_ids = {row.dedupe_key: str(row.id) for row in rows}

        tenant_job = job_ids[f"test.logs.ok:{key}:t"]
        no_tenant_job = job_ids[f"test.logs.ok:{key}:none"]

        assert seen[tenant_job] == {
            "job_id": tenant_job,
            "job_kind": "test.logs.ok",
            "tenant_id": str(people.a),
        }
        assert seen[no_tenant_job] == {"job_id": no_tenant_job, "job_kind": "test.logs.ok"}

        fail_lines = [rec for rec in log_lines() if rec["msg"] == "job failed"]
        assert len(fail_lines) == 1
        assert fail_lines[0]["error"] == "RuntimeError"
        assert fail_lines[0]["job_kind"] == "test.logs.fail"
        assert sentinel not in json.dumps(log_lines())
    finally:
        with app_engine.begin() as conn:
            conn.execute(text("DELETE FROM jobs WHERE kind LIKE 'test.logs.%'"))


def _fields(line: dict[str, Any]) -> dict[str, Any]:
    """A record without what every record has (ts, logger) and the error chain."""
    return {k: v for k, v in line.items() if k not in ("ts", "logger", "exc")}


# ZIF-95: job claimed (DEBUG), done (INFO), failed (WARNING, retried) and gave up (ERROR, the last
# attempt), each with the job's id, kind, tenant (only when it has one) and attempt.
def test_job_events_have_their_level_and_fields(
    people: People, bound: None, app_engine: Engine, log_lines: Lines
) -> None:
    kind = f"test.logs.{uuid.uuid4().hex}"
    ok, failing = f"{kind}.ok", f"{kind}.fail"

    def fail(job: Job) -> None:
        raise RuntimeError("x")

    with SessionLocal.begin() as session:
        enqueue(session, ok, f"{kind}:t", {}, tenant_id=people.a)
        enqueue(session, ok, f"{kind}:none", {})
        enqueue(session, failing, f"{kind}:retry", {})
        enqueue(session, failing, f"{kind}:last", {})
        session.execute(
            text("UPDATE jobs SET attempts = :n WHERE dedupe_key = :key"),
            {"n": jobs.MAX_ATTEMPTS - 1, "key": f"{kind}:last"},
        )
    try:
        asyncio.run(
            run_once(
                {
                    ok: JobKind(lambda job: None, 5, timedelta(hours=1)),
                    failing: JobKind(fail, 5, timedelta(hours=1)),
                }
            )
        )
        with SessionLocal() as session:
            rows = session.execute(
                text("SELECT id, dedupe_key FROM jobs WHERE kind LIKE :kind"), {"kind": f"{kind}.%"}
            )
            ids = {row.dedupe_key.split(":")[1]: str(row.id) for row in rows}

        def job(name: str, level: str, msg: str, attempts: int, **more: str) -> dict[str, Any]:
            kind_of = ok if name in ("t", "none") else failing
            fields = {"level": level, "msg": msg, "job_id": ids[name], "job_kind": kind_of}
            return {**fields, **more, "attempts": attempts}

        def logged(msg: str) -> list[dict[str, Any]]:
            found = [
                _fields(line)
                for line in log_lines()
                if line["msg"] == msg and line.get("job_kind", "").startswith(kind)
            ]
            return sorted(found, key=lambda line: line["job_id"])

        def by_id(*events: dict[str, Any]) -> list[dict[str, Any]]:
            return sorted(events, key=lambda line: line["job_id"])

        tenant = {"tenant_id": str(people.a)}
        assert logged("job claimed") == by_id(
            job("t", "DEBUG", "job claimed", 1, **tenant),
            job("none", "DEBUG", "job claimed", 1),
            job("retry", "DEBUG", "job claimed", 1),
            job("last", "DEBUG", "job claimed", jobs.MAX_ATTEMPTS),
        )
        assert logged("job done") == by_id(
            job("t", "INFO", "job done", 1, **tenant), job("none", "INFO", "job done", 1)
        )
        assert logged("job failed") == [
            job("retry", "WARNING", "job failed", 1, error="RuntimeError")
        ]
        assert logged("job gave up") == [
            job("last", "ERROR", "job gave up", jobs.MAX_ATTEMPTS, error="RuntimeError")
        ]
    finally:
        with app_engine.begin() as conn:
            conn.execute(text("DELETE FROM jobs WHERE kind LIKE :kind"), {"kind": f"{kind}.%"})


# ZIF-95: SQL is logged only with ZIF_LOG_SQL at DEBUG, through the API's own engine, and never
# with its bound values.
@pytest.mark.parametrize(
    ("log_sql", "level", "logged"),
    [
        (None, "DEBUG", False),
        (None, "INFO", False),
        ("true", "INFO", False),
        ("true", "DEBUG", True),
    ],
)
def test_sql_is_logged_only_when_asked_at_debug_and_never_its_values(
    log_lines: Lines,
    monkeypatch: pytest.MonkeyPatch,
    log_sql: str | None,
    level: str,
    logged: bool,
) -> None:
    if log_sql is None:
        monkeypatch.delenv("ZIF_LOG_SQL", raising=False)
    else:
        monkeypatch.setenv("ZIF_LOG_SQL", log_sql)
    monkeypatch.setenv("ZIF_LOG_LEVEL", level)
    logs.configure()
    sentinel = uuid.uuid4().hex

    with new_client(create_app()), SessionLocal() as session:  # the lifespan binds its engine
        assert session.scalar(text("SELECT CAST(:v AS text)"), {"v": sentinel}) == sentinel

    sql = [line for line in log_lines() if line["logger"].startswith("sqlalchemy")]
    assert any("SELECT CAST" in line["msg"] for line in sql) is logged
    assert any("parameters hidden" in line["msg"] for line in sql) is logged
    assert sentinel not in json.dumps(log_lines())


# ZIF-95: the API logs one startup line with its effective settings, from its lifespan: never on
# import or create_app(), which `python -m app.main` runs to print the OpenAPI document.
def test_the_api_logs_its_settings_once_at_startup(
    log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZIF_APP_VERSION", "9.8.7")
    monkeypatch.setenv("ZIF_TRUSTED_PROXIES", "10.1.0.0/16")
    app = create_app()
    assert not any(line["msg"] == "api started" for line in log_lines())

    with new_client(app):
        pass

    started = [_fields(line) for line in log_lines() if line["msg"] == "api started"]
    assert started == [
        {
            "level": "INFO",
            "msg": "api started",
            "version": "9.8.7",
            "log_level": "DEBUG",
            "log_format": "json",
            "log_sql": False,
            "trusted_proxies": "10.1.0.0/16",
        }
    ]
    assert "postgresql" not in json.dumps(log_lines())


# 16: configure() is idempotent and polite to handlers it doesn't own.
def test_configure_is_idempotent_and_polite_to_other_handlers() -> None:
    root = logging.getLogger()
    dummy = logging.NullHandler()
    dummy.name = "dummy"
    root.addHandler(dummy)
    try:
        logs.configure()
        logs.configure()

        ours = [h for h in root.handlers if h.name == logs.HANDLER]
        assert len(ours) == 1
        assert dummy in root.handlers
        assert sys.excepthook is logs._excepthook
    finally:
        root.removeHandler(dummy)
        logs.configure()


# 17: uvicorn's own loggers are silenced; the access logger never even builds its line.
def test_uvicorn_loggers_are_silenced_after_configure() -> None:
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    # dictConfig already sets propagate False on uvicorn.access; force it back on so the
    # assertion below only passes if configure() itself disables it.
    logging.getLogger("uvicorn.access").propagate = True

    logs.configure()

    assert not logging.getLogger("uvicorn").handlers
    assert logging.getLogger("uvicorn").propagate
    assert not logging.getLogger("uvicorn.error").handlers
    assert logging.getLogger("uvicorn.error").propagate
    access = logging.getLogger("uvicorn.access")
    assert not access.propagate
    assert not access.hasHandlers()

    captured: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    capture = Capture()
    logging.getLogger().addHandler(capture)
    try:
        access.info("should never appear")
    finally:
        logging.getLogger().removeHandler(capture)
    assert captured == []


# 18: at DEBUG, our loggers and uvicorn.error show DEBUG; noisy libraries stay at WARNING; an
# unconfigured third party shows INFO but not DEBUG (root sits at max(level, INFO)).
def test_levels_at_debug(log_lines: Lines) -> None:
    assert logging.getLogger("app.x").isEnabledFor(logging.DEBUG)
    assert logging.getLogger("uvicorn.error").isEnabledFor(logging.DEBUG)
    assert not logging.getLogger("uvicorn.error").isEnabledFor(5)
    assert not logging.getLogger("sqlalchemy.engine.Engine").isEnabledFor(logging.INFO)
    assert logging.getLogger("some.thirdparty").isEnabledFor(logging.INFO)
    assert not logging.getLogger("some.thirdparty").isEnabledFor(logging.DEBUG)


def _run_uncaught_script(body: str, env_extra: dict[str, str]) -> str:
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=API_DIR,
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, **env_extra},
    )
    return result.stdout + result.stderr


# 19: an uncaught error, in the main thread or a spawned one, is one safe JSON line.
def test_an_uncaught_error_in_the_main_thread_is_one_json_line() -> None:
    sentinel = uuid.uuid4().hex
    output = _run_uncaught_script(
        "import os\n"
        "import app.logs as logs\n"
        "logs.configure()\n"
        "raise ValueError(os.environ['ZIF_TEST_SENTINEL'])\n",
        {"ZIF_TEST_SENTINEL": sentinel},
    )

    lines = [rec for rec in output.splitlines() if rec.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["level"] == "CRITICAL"
    assert record["msg"] == "uncaught error"
    assert record["exc"][0]["type"] == "builtins.ValueError"
    assert sentinel not in output


# A deliberate Ctrl-C is not a crash: defer to Python's default handler, no CRITICAL line.
def test_keyboard_interrupt_defers_to_the_default_excepthook(
    monkeypatch: pytest.MonkeyPatch, log_lines: Lines
) -> None:
    calls: list[BaseException | None] = []
    monkeypatch.setattr(
        sys, "__excepthook__", lambda kind, error, tb: calls.append(error), raising=False
    )

    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        logs._excepthook(*sys.exc_info())

    assert len(calls) == 1
    assert isinstance(calls[0], KeyboardInterrupt)
    assert not any(line["msg"] == "uncaught error" for line in log_lines())


# ALSO FOLD: an uncaught ValidationError also names its missing/invalid ZIF_* variables.
def test_an_uncaught_validation_error_names_the_missing_variable() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.worker"],
        cwd=API_DIR,
        capture_output=True,
        text=True,
        timeout=15,
        env={k: v for k, v in os.environ.items() if k != "ZIF_DATABASE_URL"},
    )

    output = result.stdout + result.stderr
    lines = [json.loads(rec) for rec in output.splitlines() if rec.startswith("{")]
    assert any(
        rec["level"] == "CRITICAL" and "ZIF_DATABASE_URL" in rec.get("invalid_env", [])
        for rec in lines
    )


# R2-B1 (a): a ValidationError with an empty loc, from something other than one of our Settings
# classes, must not crash the excepthook itself (the old `e["loc"][0]` raised IndexError there,
# and Python's own "Error in sys.excepthook" fallback then printed the message, sentinel and all).
def test_an_uncaught_non_settings_validation_error_is_still_one_safe_json_line() -> None:
    sentinel = uuid.uuid4().hex
    output = _run_uncaught_script(
        "import os\n"
        "from pydantic import TypeAdapter\n"
        "import app.logs as logs\n"
        "logs.configure()\n"
        "TypeAdapter(int).validate_python(os.environ['ZIF_TEST_SENTINEL'])\n",
        {"ZIF_TEST_SENTINEL": sentinel},
    )

    lines = [rec for rec in output.splitlines() if rec.strip()]
    assert len(lines) == 1, output
    record = json.loads(lines[0])
    assert record["level"] == "CRITICAL"
    assert "invalid_env" not in record
    assert sentinel not in output
    assert "Error in sys.excepthook" not in output


# R2-B1 (a'): a *real* Settings class's error can still have an empty loc (model_validate on
# something other than a dict is a model-level error, not a per-field one) — the guard above must
# hold there too, not just for a non-Settings ValidationError.
def test_an_uncaught_settings_validation_error_with_no_field_location_is_still_safe() -> None:
    sentinel = uuid.uuid4().hex
    output = _run_uncaught_script(
        "import os\n"
        "from app.config import MailSettings\n"
        "import app.logs as logs\n"
        "logs.configure()\n"
        "MailSettings.model_validate(os.environ['ZIF_TEST_SENTINEL'])\n",
        {"ZIF_TEST_SENTINEL": sentinel},
    )

    lines = [rec for rec in output.splitlines() if rec.strip()]
    assert len(lines) == 1, output
    record = json.loads(lines[0])
    assert record["level"] == "CRITICAL"
    assert "invalid_env" not in record
    assert sentinel not in output
    assert "Error in sys.excepthook" not in output


# R3-B1: a lookalike class whose title also ends in "Settings" (BusinessSettings, a plain BaseModel
# holding a business's UI settings, not one of our pydantic-settings deployment classes) must never
# leak a rejected field's name: _invalid_env matches the title against our own Settings classes
# exactly, not any class whose name happens to end in "Settings".
def test_an_uncaught_lookalike_settings_error_names_nothing() -> None:
    sentinel = uuid.uuid4().hex
    output = _run_uncaught_script(
        "import os\n"
        "from app.business_settings import BusinessSettings\n"
        "import app.logs as logs\n"
        "logs.configure()\n"
        "sentinel = os.environ['ZIF_TEST_SENTINEL']\n"
        "BusinessSettings.model_validate({sentinel: 1, 'timezone': sentinel})\n",
        {"ZIF_TEST_SENTINEL": sentinel},
    )

    lines = [rec for rec in output.splitlines() if rec.strip()]
    assert len(lines) == 1, output
    record = json.loads(lines[0])
    assert record["level"] == "CRITICAL"
    assert "invalid_env" not in record
    assert sentinel not in output
    assert sentinel.upper() not in output


# R2-B1 (b): a manual __cause__ cycle through a Settings ValidationError must terminate, not hang
# the excepthook forever (the old _invalid_env walk had no seen-id guard).
def test_a_cyclic_cause_chain_through_a_settings_error_terminates() -> None:
    body = (
        "import app.logs as logs\n"
        "from app.config import WorkerSettings\n"
        "from pydantic import ValidationError\n"
        "logs.configure()\n"
        "try:\n"
        "    WorkerSettings()\n"
        "except ValidationError as settings_error:\n"
        "    other = RuntimeError('other')\n"
        "    other.__cause__ = settings_error\n"
        "    settings_error.__cause__ = other\n"
        "    raise other\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", body],
            cwd=API_DIR,
            capture_output=True,
            text=True,
            timeout=15,
            env={k: v for k, v in os.environ.items() if k != "ZIF_DATABASE_URL"},
        )
    except subprocess.TimeoutExpired:
        pytest.fail("excepthook hung on a cyclic __cause__ chain")

    output = result.stdout + result.stderr
    lines = [json.loads(rec) for rec in output.splitlines() if rec.startswith("{")]
    assert any(
        rec["level"] == "CRITICAL" and "ZIF_DATABASE_URL" in rec.get("invalid_env", [])
        for rec in lines
    )


def test_an_uncaught_error_in_a_thread_is_one_json_line() -> None:
    sentinel = uuid.uuid4().hex
    output = _run_uncaught_script(
        "import os\n"
        "import threading\n"
        "import app.logs as logs\n"
        "logs.configure()\n"
        "def boom():\n"
        "    raise ValueError(os.environ['ZIF_TEST_SENTINEL'])\n"
        "t = threading.Thread(target=boom)\n"
        "t.start()\n"
        "t.join()\n",
        {"ZIF_TEST_SENTINEL": sentinel},
    )

    lines = [rec for rec in output.splitlines() if rec.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["level"] == "CRITICAL"
    assert record["msg"] == "uncaught error"
    assert record["exc"][0]["type"] == "builtins.ValueError"
    assert sentinel not in output


# R2-B3: a finalizer's error (__del__, uncatchable any other way) goes through the same logger, at
# ERROR (it doesn't take the process down) not CRITICAL, class and frames only — never
# args.err_msg or repr(args.object), either of which can carry a value.
def test_an_unraisable_error_is_one_error_json_line_with_no_repr() -> None:
    sentinel = uuid.uuid4().hex
    output = _run_uncaught_script(
        "import gc\n"
        "import os\n"
        "import app.logs as logs\n"
        "logs.configure()\n"
        "sentinel = os.environ['ZIF_TEST_SENTINEL']\n"
        "class D:\n"
        "    def __del__(self):\n"
        "        raise ValueError(sentinel)\n"
        "D()\n"
        "gc.collect()\n",
        {"ZIF_TEST_SENTINEL": sentinel},
    )

    lines = [rec for rec in output.splitlines() if rec.strip()]
    assert len(lines) == 1, output
    record = json.loads(lines[0])
    assert record["level"] == "ERROR"
    assert record["msg"] == "unraisable error"
    assert record["exc"][0]["type"] == "builtins.ValueError"
    assert sentinel not in output


# B4: migrations/env.py calls configure() too, so `alembic current` logs JSON through it, not
# through alembic's own logging.basicConfig-style handler.
def test_migrations_log_json_lines() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=API_DIR,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "ZIF_LOG_FORMAT": "json",
            "ZIF_LOG_LEVEL": "INFO",
            "ZIF_APP_VERSION": "9.8.7",
        },
    )

    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert any(r["logger"].startswith("alembic") and r["level"] == "INFO" for r in records)
    # ZIF-95: one startup line with the effective settings, never the database URL.
    started = [_fields(r) for r in records if r["msg"] == "migrations started"]
    assert started == [
        {
            "level": "INFO",
            "msg": "migrations started",
            "version": "9.8.7",
            "log_level": "INFO",
            "log_format": "json",
            "log_sql": False,
        }
    ]
    assert "postgresql" not in result.stdout + result.stderr
    assert "INFO  [alembic" not in result.stdout + result.stderr


# 20 (guard): configure() writes nothing on its own; `python -m app.main`'s stdout is still the
# OpenAPI document, valid JSON.
def test_configure_writes_nothing_and_python_dash_m_app_main_is_still_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logs.configure()
    assert capsys.readouterr().out == ""

    result = subprocess.run(
        [sys.executable, "-m", "app.main"], cwd=API_DIR, capture_output=True, text=True, timeout=20
    )

    json.loads(result.stdout)  # parses without raising

    # Reconfigure with real stdout: this test's own handler wrote into capsys's now-closed
    # capture stream, and any later test that logs would otherwise hit it.
    with capsys.disabled():
        logs.configure()


# B1: a startup failure never leaks the exception message. Starlette sends str(exc) (here a
# malformed database URL) as the lifespan.startup.failed ASGI message and uvicorn logs it with no
# exc_info, bypassing our formatter.
def test_a_startup_failure_never_leaks_the_bad_url() -> None:
    sentinel = "zz" + uuid.uuid4().hex[:10]  # non-numeric: fails int(port) in create_engine
    port = 18700 + (uuid.uuid4().int % 300)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=API_DIR,
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "ZIF_DATABASE_URL": f"postgresql+psycopg://app@db:{sentinel}/zb"},
    )

    output = result.stdout + result.stderr
    assert sentinel not in output
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    assert any(line["level"] == "CRITICAL" and line["msg"] == "startup failed" for line in lines)
