"""ZIF-87: manual OpenTelemetry spans. Table ids (T1..T12) match docs/specs/zif-87-traces.md."""

import asyncio
import http.server
import logging
import os
import re
import smtplib
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import Any

import pytest
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind
from sqlalchemy import Engine, text

from app import clients as clients_module
from app import logs, mail, tracing, worker
from app.db import SessionLocal
from app.jobs import JobKind, enqueue, run_once
from app.main import create_app
from tests.conftest import API_DIR, People, failing, fresh_email, new_client, signed_in
from tests.test_account_email import bound, inbox, link_in, message  # noqa: F401
from tests.test_clients_api import add_client
from tests.test_invite_email import run_jobs

Lines = Callable[[], list[dict[str, Any]]]
Spans = Callable[[], list[ReadableSpan]]


def _records(lines: Lines) -> list[Any]:
    return lines.records()  # type: ignore[attr-defined,no-any-return]


def _one(spans: Iterable[ReadableSpan], **match: Any) -> ReadableSpan:
    found = [
        s for s in spans if all(getattr(s, key, None) == value for key, value in match.items())
    ]
    assert len(found) == 1, f"expected exactly one span matching {match}, found {len(found)}"
    return found[0]


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


# T1: request -> job -> email, one trace throughout.
def test_t1_request_job_email_is_one_trace(people: People, spans: Spans) -> None:
    app = create_app()
    owner = signed_in(app, people.a, people.both)
    email = fresh_email()

    response = owner.post("/api/invites", json={"email": email})
    assert response.status_code == 201
    run_jobs()

    finished = spans()
    server = _one(finished, kind=SpanKind.SERVER)
    assert server.parent is None  # a root: nothing sent a traceparent

    sql_children = [
        s
        for s in finished
        if s.kind == SpanKind.CLIENT
        and _attrs(s).get("db.system.name") == "postgresql"
        and s.parent is not None
        and s.parent.span_id == server.context.span_id
    ]
    assert sql_children, "the request's own SQL statements should be children of the SERVER span"

    # By name and trace, not name alone: the shared `jobs` table can carry an unrelated leftover
    # job from another test, which would otherwise also match "job email.invite"/"smtp send".
    consumer_candidates = [
        s
        for s in finished
        if s.name == "job email.invite"
        and s.parent is not None
        and s.parent.trace_id == server.context.trace_id
    ]
    assert len(consumer_candidates) == 1
    consumer = consumer_candidates[0]
    assert consumer.parent is not None
    assert consumer.parent.span_id == server.context.span_id

    smtp_candidates = [
        s
        for s in finished
        if s.name == "smtp send"
        and s.parent is not None
        and s.parent.span_id == consumer.context.span_id
    ]
    assert len(smtp_candidates) == 1
    smtp = smtp_candidates[0]
    assert smtp.parent is not None
    assert smtp.parent.trace_id == server.context.trace_id


# T2: no span anywhere carries the email, the password, or a literal "?" (a raw query string).
def test_t2_no_span_carries_pii(people: People, spans: Spans) -> None:
    app = create_app()
    owner = signed_in(app, people.a, people.both)
    invite_email = fresh_email()
    owner.post("/api/invites", json={"email": invite_email})
    run_jobs()

    owner.get(f"/api/clients?q={invite_email}")

    signup_email = fresh_email()
    password = "correct horse battery staple 9"
    signup_body = {"email": signup_email, "locale": "en", "password": password}
    new_client(app).post("/api/sign-up", json=signup_body)
    run_jobs()  # also sweeps its own job/smtp spans below, and never leaves the row behind

    dupe_email = fresh_email()
    add_client(owner, {"name": "First", "email": dupe_email})
    owner.post("/api/clients", json={"name": "Second", "email": dupe_email})

    needles = [invite_email, signup_email, password, dupe_email, "?"]

    def haystacks(span: ReadableSpan) -> Iterable[str]:
        yield span.name
        if span.status.description:
            yield span.status.description
        for value in _attrs(span).values():
            yield str(value)
        for event in span.events:
            yield event.name
            for value in (event.attributes or {}).values():
                yield str(value)
        for value in span.resource.attributes.values():
            yield str(value)

    for span in spans():
        for haystack in haystacks(span):
            for needle in needles:
                assert needle not in haystack, f"{needle!r} leaked into {span.name}: {haystack!r}"


# T3: a raw id in the path never reaches the span name or its attributes; the attribute keys are
# exactly the allowlist.
def test_t3_span_name_and_attributes_use_the_route_template(people: People, spans: Spans) -> None:
    app = create_app()
    owner = signed_in(app, people.a, people.both)
    client = add_client(owner, {"name": "Someone", "email": fresh_email()})
    client_id = client["id"]

    response = owner.patch(f"/api/clients/{client_id}", json={"client_note": "called back"})
    assert response.status_code == 200

    server = _one(spans(), name="PATCH /api/clients/{client_id}")
    assert client_id not in server.name
    attrs = _attrs(server)
    assert attrs.get("http.route") == "/api/clients/{client_id}"
    assert client_id not in str(attrs["http.route"])
    assert set(attrs) == {
        "http.request.method",
        "http.route",
        "http.response.status_code",
    }


# T4: a unique violation's SQL span carries logs.error_summary(...), never str(exc), and never the
# bound value.
def test_t4_unique_violation_sql_span(people: People, spans: Spans) -> None:
    app = create_app()
    owner = signed_in(app, people.a, people.both)
    email = fresh_email()
    add_client(owner, {"name": "Original", "email": email})

    conflict = owner.post("/api/clients", json={"name": "Second", "email": email})
    assert conflict.status_code == 409

    inserts = [
        s for s in spans() if s.name == "INSERT" and s.status.status_code == trace.StatusCode.ERROR
    ]
    (insert,) = inserts
    expected = "UniqueViolation sqlstate=23505 constraint=uq_clients_tenant_id_email"
    assert insert.status.description == expected
    assert insert.events == ()
    assert email not in insert.status.description
    query_text = str(_attrs(insert)["db.query.text"])
    assert "%(" in query_text
    assert email not in query_text


# T5: an SMTP failure never records the recipient; the SDK's own exception recording stays off.
def test_t5_smtp_failure_span(monkeypatch: pytest.MonkeyPatch, spans: Spans) -> None:
    address = fresh_email()

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float = 10) -> None:
            pass

        def send_message(self, msg: Any, to_addrs: list[str]) -> None:
            raise smtplib.SMTPRecipientsRefused({to_addrs[0]: (550, b"no such user")})

        def quit(self) -> None:
            pass

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    with pytest.raises(smtplib.SMTPRecipientsRefused):
        mail.deliver("hello", address, "subject", "body")

    smtp_span = _one(spans(), name="smtp send")
    assert smtp_span.events == ()
    smtp_attrs = _attrs(smtp_span)
    assert smtp_attrs.get("error.type") == "SMTPRecipientsRefused"
    # error.type is set on failure; server.address/server.port are the only attributes the span
    # ever starts with.
    assert set(smtp_attrs) - {"error.type"} == {"server.address", "server.port"}
    for value in smtp_attrs.values():
        assert address not in str(value)
    assert smtp_span.status.description is None or address not in smtp_span.status.description


# T6: enqueue only ever adds traceparent, inside a span, never touching the caller's dict.
def test_t6_enqueue_stamps_traceparent_only_inside_a_span(migrate_engine: Engine) -> None:
    no_span_kind = f"test.trace.t6.no_span.{uuid.uuid4().hex}"
    in_span_kind = f"test.trace.t6.in_span.{uuid.uuid4().hex}"
    payload = {"a": 1}

    with SessionLocal.begin() as session:
        assert enqueue(session, no_span_kind, no_span_kind, dict(payload))
    assert payload == {"a": 1}
    with migrate_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT payload FROM jobs WHERE dedupe_key = :k"), {"k": no_span_kind}
        ).scalar_one()
    assert stored == {"a": 1}

    with tracing.span("t6", SpanKind.INTERNAL, {}):
        with SessionLocal.begin() as session:
            # The caller's own dict, not a throwaway copy: catches enqueue() mutating its argument.
            assert enqueue(session, in_span_kind, in_span_kind, payload)
    assert payload == {"a": 1}  # never mutated
    with migrate_engine.connect() as conn:
        stored2 = conn.execute(
            text("SELECT payload FROM jobs WHERE dedupe_key = :k"), {"k": in_span_kind}
        ).scalar_one()
    assert set(stored2) - set(payload) == {"traceparent"}
    tp = stored2["traceparent"]
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", tp)
    assert int(tp.rsplit("-", 1)[1], 16) & 1  # the default sampler samples everything


# T7: a missing, malformed or non-string traceparent never fails run_once's whole batch, and a
# failed handler still marks its own job span.
def test_t7_malformed_traceparent_and_a_failed_handler(
    migrate_engine: Engine, spans: Spans
) -> None:
    batch_kind = f"test.trace.t7.{uuid.uuid4().hex}"
    payloads: dict[str, dict[str, Any]] = {
        "missing": {},
        "malformed": {"traceparent": "not-a-traceparent"},
        "nonstring": {"traceparent": 5},
    }
    with SessionLocal.begin() as session:
        for name, payload in payloads.items():
            enqueue(session, batch_kind, f"{batch_kind}:{name}", payload)

    claimed = asyncio.run(run_once({batch_kind: JobKind(lambda job: None, 5, timedelta(hours=1))}))
    assert claimed == 3
    with migrate_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT completed_at, skipped FROM jobs WHERE kind = :k"), {"k": batch_kind}
        ).all()
    assert len(rows) == 3
    assert all(row.completed_at is not None and not row.skipped for row in rows)
    batch_spans = [s for s in spans() if s.name == f"job {batch_kind}"]
    assert len(batch_spans) == 3
    assert all(s.parent is None for s in batch_spans)

    fail_kind = f"test.trace.t7fail.{uuid.uuid4().hex}"

    def fail(job: Any) -> None:
        raise RuntimeError("boom")

    with SessionLocal.begin() as session:
        enqueue(session, fail_kind, fail_kind, {})
    asyncio.run(run_once({fail_kind: JobKind(fail, 5, timedelta(hours=1))}))
    job_span = _one(spans(), name=f"job {fail_kind}")
    assert job_span.status.status_code == trace.StatusCode.ERROR
    assert _attrs(job_span).get("error.type") == "RuntimeError"


# T8: the exporter is gated by the endpoint, and configure() is idempotent and silent.
def test_t8_provider_exporter_gated_by_endpoint(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    with caplog.at_level(logging.WARNING):
        unset = tracing._provider("t8-unset")
    assert len(unset._active_span_processor._span_processors) == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    with_endpoint = tracing._provider("t8-set")
    assert len(with_endpoint._active_span_processor._span_processors) == 1
    with_endpoint.shutdown()  # no exporter thread left running after this test

    # An empty string is not a real endpoint: the same gate must hold, not just for an unset var.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
    with caplog.at_level(logging.WARNING):
        empty = tracing._provider("t8-empty")
    assert len(empty._active_span_processor._span_processors) == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_t8_configure_is_idempotent_and_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(tracing, "_configured", False)
    calls: list[str] = []
    monkeypatch.setattr(trace, "set_tracer_provider", lambda provider: calls.append("set"))
    with caplog.at_level(logging.WARNING):
        tracing.configure("t8-a")
        tracing.configure("t8-b")
    assert calls == ["set"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# T9: no noise from the healthcheck or the worker's claim poll.
def test_t9_no_span_for_healthz_or_a_poll_with_no_current_span(
    spans: Spans, migrate_engine: Engine
) -> None:
    client = new_client(create_app())
    response = client.get("/api/healthz")
    assert response.status_code == 200
    assert not [s for s in spans() if s.kind == SpanKind.SERVER]

    with migrate_engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    assert not [s for s in spans() if _attrs(s).get("db.system.name") == "postgresql"]


# T10: log lines carry the active span's ids, and only those; the "unhandled error" line still
# carries the SERVER span's ids after the span has exited.
def test_t10_log_lines_carry_the_active_spans_ids(
    people: People, spans: Spans, log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app()
    owner = signed_in(app, people.a, people.both)

    response = owner.get("/api/session")
    assert response.status_code == 200
    server = _one(spans(), kind=SpanKind.SERVER)
    access_line = next(line for line in log_lines() if line["msg"] == "access")
    assert access_line["trace_id"] == format(server.context.trace_id, "032x")
    assert access_line["span_id"] == format(server.context.span_id, "016x")

    logger = logging.getLogger("tests.test_tracing")
    logger.info("outside any span")
    outside_line = next(line for line in log_lines() if line["msg"] == "outside any span")
    assert "trace_id" not in outside_line
    assert "span_id" not in outside_line

    failing(monkeypatch, clients_module, "as_clients")
    crashed = owner.get("/api/clients")
    assert crashed.status_code == 500
    errored = [s for s in spans() if s.status.status_code == trace.StatusCode.ERROR]
    crashed_server = _one(errored, kind=SpanKind.SERVER)
    error_line = next(line for line in log_lines() if line["msg"] == "unhandled error")
    assert error_line["trace_id"] == format(crashed_server.context.trace_id, "032x")
    assert error_line["span_id"] == format(crashed_server.context.span_id, "016x")


# T10: _add_context must never mutate the dict CONTEXT holds: two records sharing that dict (one
# logged inside a span, one after) must not see each other's ids.
def test_t10_add_context_never_mutates_the_shared_dict() -> None:
    shared = {"request_id": "r1"}
    token = logs.CONTEXT.set(shared)
    try:
        with tracing.span("t10", SpanKind.INTERNAL, {}):
            record = logging.makeLogRecord({})
            logs._add_context(record)
            assert getattr(record, "context")["trace_id"]  # noqa: B009 -- set by _add_context
        assert shared == {"request_id": "r1"}
    finally:
        logs.CONTEXT.reset(token)


# T11: each process names itself; an explicit OTEL_SERVICE_NAME still wins.
def test_t11_provider_names_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    unset = tracing._provider("x")
    assert unset.resource.attributes["service.name"] == "x"

    monkeypatch.setenv("OTEL_SERVICE_NAME", "explicit")
    explicit = tracing._provider("y")
    assert explicit.resource.attributes["service.name"] == "explicit"


def test_t11_worker_and_migrations_configure_their_own_service_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(tracing, "configure", lambda service: calls.append(service))

    class StopEarly(Exception):
        pass

    def boom() -> Any:
        raise StopEarly

    monkeypatch.setattr(worker, "WorkerSettings", boom)
    with pytest.raises(StopEarly):
        worker.main()
    assert calls == ["ziftbook-worker"]

    env_source = (API_DIR / "migrations" / "env.py").read_text()
    assert 'tracing.configure("ziftbook-migrations")' in env_source
    assert '"migrations upgrade"' in env_source


# Review: http.request.method must carry the bounded value (the span-name method or _OTHER),
# never the raw client token -- the client fully controls that string.
def test_http_request_method_attribute_is_bounded(spans: Spans) -> None:
    client = new_client(create_app())

    response = client.request("FOO", "/api/session")

    assert response.status_code == 415
    server = _one(spans(), kind=SpanKind.SERVER)
    assert server.name.startswith("_OTHER")
    assert _attrs(server).get("http.request.method") == "_OTHER"


# Review: only traceparent is extracted at the API, never tracestate -- a stamped job payload must
# never carry it either (enqueue() injects whatever trace_state the current span context holds).
def test_extract_never_reads_tracestate(people: People, migrate_engine: Engine) -> None:
    app = create_app()
    owner = signed_in(app, people.a, people.both)
    email = fresh_email()
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    parent_span_id = "00f067aa0ba902b7"
    headers = {
        "traceparent": f"00-{trace_id}-{parent_span_id}-01",
        "tracestate": "vendor=value",
    }

    response = owner.post("/api/invites", json={"email": email}, headers=headers)
    assert response.status_code == 201

    with migrate_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT payload FROM jobs WHERE kind = 'email.invite' ORDER BY id DESC LIMIT 1")
        ).scalar_one()
    assert "tracestate" not in stored


# Review: a handler that RETURNS a 500 without raising still gets an ERROR SERVER span.
def test_a_returned_500_still_gets_an_error_span(spans: Spans) -> None:
    app = create_app()

    def returns_500() -> JSONResponse:
        return JSONResponse(status_code=500, content={"code": "internal"})

    app.add_api_route("/api/test-500", returns_500, methods=["GET"], tags=["test"])
    client = new_client(app)

    response = client.get("/api/test-500")
    assert response.status_code == 500

    server = _one(spans(), kind=SpanKind.SERVER, name="GET /api/test-500")
    assert server.status.status_code == trace.StatusCode.ERROR
    assert _attrs(server).get("error.type") == "500"


# T12 (guard): an incoming traceparent becomes the SERVER span's trace and parent.
def test_t12_incoming_traceparent_is_the_parent(people: People, spans: Spans) -> None:
    app = create_app()
    owner = signed_in(app, people.a, people.both)
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    parent_span_id = "00f067aa0ba902b7"

    incoming = f"00-{trace_id}-{parent_span_id}-01"
    response = owner.get("/api/session", headers={"traceparent": incoming})
    assert response.status_code == 200

    server = _one(spans(), kind=SpanKind.SERVER)
    assert format(server.context.trace_id, "032x") == trace_id
    assert server.parent is not None
    assert format(server.parent.span_id, "016x") == parent_span_id


# ZIF-137: export logs over OTLP with the same allowlist as stdout. Test numbers below match
# docs/specs/2026-09-24-zif-137-spec.md's "Tests" section.


# 4: fence. Native ids equal the span's inside it, no trace_id/span_id attribute; and once the
# span has exited, with the ids still in CONTEXT (the access_log pattern), they are still native,
# not attributes. Kills: reading trace.get_current_span() / context=None instead of the line's own
# fields.
def test_native_ids_match_the_span_inside_and_after_it_exits(log_lines: Lines) -> None:
    logger = logging.getLogger("app.tests.tracing")
    with tracing.span("test.span", SpanKind.INTERNAL, {}) as current:
        span_context = current.get_span_context()
        logger.info("inside span")

    log_lines()
    record = _records(log_lines)[-1].log_record
    assert record.trace_id == span_context.trace_id
    assert record.span_id == span_context.span_id
    assert "trace_id" not in record.attributes
    assert "span_id" not in record.attributes

    before = dict(logs.CONTEXT.get({}))
    with tracing.span("test.span2", SpanKind.INTERNAL, {}) as current2:
        span_context2 = current2.get_span_context()
        logs.CONTEXT.set(
            {
                **logs.CONTEXT.get({}),
                "trace_id": format(span_context2.trace_id, "032x"),
                "span_id": format(span_context2.span_id, "016x"),
            }
        )
    try:
        logger.info("after span exited")  # no current span; CONTEXT still carries the ids

        log_lines()
        after = _records(log_lines)[-1].log_record
        assert after.trace_id == span_context2.trace_id
        assert after.span_id == span_context2.span_id
        assert "trace_id" not in after.attributes
        assert "span_id" not in after.attributes
    finally:
        logs.CONTEXT.set(before)


# 7: fence. The gate: an endpoint (either name), stripped, and OTEL_<SIGNAL>_EXPORTER stripped and
# lower-cased != "none". Each signal is switched independently. Kills: trusting the SDK to read
# OTEL_*_EXPORTER, an ungated log exporter (localhost fallback, slow exit), one shared switch.
@pytest.mark.parametrize(
    ("env", "traces_on", "logs_on"),
    [
        ({}, False, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "   "}, False, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"}, True, True),
        (
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
                "OTEL_TRACES_EXPORTER": "none",
            },
            False,
            True,
        ),
        (
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
                "OTEL_LOGS_EXPORTER": " NONE ",
            },
            True,
            False,
        ),
        ({"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://127.0.0.1:9"}, False, True),
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:9"}, True, False),
    ],
)
def test_the_signal_gate(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    env: dict[str, str],
    traces_on: bool,
    logs_on: bool,
) -> None:
    for name in [n for n in os.environ if n.startswith("OTEL_")]:
        monkeypatch.delenv(name, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with caplog.at_level(logging.WARNING):
        provider = tracing._provider("test-gate")
        log_provider = tracing._log_provider()
    try:
        assert (len(provider._active_span_processor._span_processors) > 0) is traces_on
        assert (log_provider is not None) is logs_on
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    finally:
        provider.shutdown()
        if log_provider is not None:
            log_provider.shutdown()


# 11: guard. configure() is idempotent with logs on: exactly one zif-otlp handler on the root.
def test_configure_is_idempotent_with_logs_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_configured", False)
    monkeypatch.setattr(trace, "set_tracer_provider", lambda provider: None)
    memory_provider = LoggerProvider(resource=Resource.create(), shutdown_on_exit=False)
    monkeypatch.setattr(tracing, "_log_provider", lambda: memory_provider)
    root = logging.getLogger()
    try:
        tracing.configure("t")
        logs.configure()
        tracing.configure("t")

        handlers = [h for h in root.handlers if h.name == logs.OtlpHandler.name]
        assert len(handlers) == 1
    finally:
        for handler in [h for h in root.handlers if h.name == logs.OtlpHandler.name]:
            root.removeHandler(handler)


class _LogsReceiver(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path != "/v1/logs":
            self.send_response(404)
            self.end_headers()
            return
        request = ExportLogsServiceRequest()
        request.ParseFromString(body)
        self.server.received.append(request)  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, format_: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # silence the request log; nothing this test needs


# 10: fence. End to end, the only test with a real exporter: a stdlib http.server on
# 127.0.0.1:0 decodes ExportLogsServiceRequest at /v1/logs. A child process with the endpoint set
# runs logs.configure() and tracing.configure("ziftbook-migrations"), logs one line, then dies on
# an uncaught error. Both records must arrive, under service.name=ziftbook-migrations. Kills:
# shutdown_on_exit=False / no flush, a missing processor, a wrong endpoint path, a gate that never
# builds the real exporter, a wrong service name.
def test_t10_logs_reach_a_real_collector_end_to_end() -> None:
    server = http.server.HTTPServer(("127.0.0.1", 0), _LogsReceiver)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{port}"
        body = (
            "import logging\n"
            "import app.logs as logs\n"
            "import app.tracing as tracing\n"
            "logs.configure()\n"
            "tracing.configure('ziftbook-migrations')\n"
            "logging.getLogger('app.tests.tracing').info('e2e line')\n"
            "raise RuntimeError('boom')\n"
        )
        subprocess.run(
            [sys.executable, "-c", body],
            cwd=API_DIR,
            capture_output=True,
            text=True,
            timeout=25,
            env=env,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    seen: list[tuple[str | None, Any]] = []
    for request in server.received:  # type: ignore[attr-defined]
        for resource_logs in request.resource_logs:
            service_name = next(
                (
                    kv.value.string_value
                    for kv in resource_logs.resource.attributes
                    if kv.key == "service.name"
                ),
                None,
            )
            for scope_logs in resource_logs.scope_logs:
                seen.extend((service_name, record) for record in scope_logs.log_records)

    assert len(seen) == 2, seen
    assert all(service_name == "ziftbook-migrations" for service_name, _ in seen)
    bodies = {record.body.string_value for _, record in seen}
    assert "e2e line" in bodies
    assert any(record.severity_text == "CRITICAL" for _, record in seen)
