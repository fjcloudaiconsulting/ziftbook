"""app.logs: one setup for the API, the worker and migrations. See CONTRIBUTING "Logging"."""

import asyncio
import contextvars
import json
import logging
import logging.config
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, MutableMapping
from datetime import timedelta
from typing import Any

import pytest
import uvicorn.config
from fastapi import FastAPI
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app import logs
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


# 3: exc never holds an exception's message, only its class and frames.
def test_the_exc_chain_never_holds_a_message(log_lines: Lines) -> None:
    secret_a = "".join(["sec", "ret-a-", uuid.uuid4().hex[:8]])
    secret_b = "".join(["sec", "ret-b-", uuid.uuid4().hex[:8]])
    try:
        try:
            raise KeyError(secret_b)
        except KeyError as cause:
            raise ValueError(secret_a) from cause
    except ValueError as error:
        logging.getLogger("app.tests.logs").error("m", exc_info=error)

    line = log_lines()[-1]

    assert [link["type"] for link in line["exc"]] == ["builtins.ValueError", "builtins.KeyError"]
    frames = [f for link in line["exc"] for f in link["frames"]]
    assert any(f["function"] == "test_the_exc_chain_never_holds_a_message" for f in frames)
    dumped = json.dumps(line)
    assert secret_a not in dumped
    assert secret_b not in dumped


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
    assert "constraint" in link
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
    line = next(rec for rec in reversed(log_lines()) if rec["msg"] == "access")
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
    line = next(rec for rec in reversed(log_lines()) if rec["msg"] == "access")
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
    session_lines = [rec for rec in log_lines() if rec.get("route") == "/api/session"]
    assert session_lines[-1]["level"] == "INFO"

    before = len(log_lines())
    monkeypatch.setenv("ZIF_LOG_LEVEL", "INFO")
    logs.configure()
    client.get("/api/healthz")
    assert len(log_lines()) == before  # nothing new: no access line at INFO

    client.get("/api/session")
    session_lines = [rec for rec in log_lines() if rec.get("route") == "/api/session"]
    assert session_lines[-1]["level"] == "INFO"


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
    import os

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
