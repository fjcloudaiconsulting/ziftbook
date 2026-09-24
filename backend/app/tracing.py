"""Manual OpenTelemetry spans: no contrib instrumentation (see docs/specs/zif-87-traces.md, R1),
so every attribute set anywhere in this codebase comes from an explicit allowlist. Exception
recording is off everywhere (R2): a span's error status carries logs.error_summary(e), never the
exception's own message or the SDK's default record_exception, both of which can quote an email.
"""

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from sqlalchemy import Engine, event

from app import logs

# Only W3C tracecontext: never the global propagator, whose default also reads baggage (R3), which
# would let arbitrary client data ride into the global jobs table.
PROPAGATOR = TraceContextTextMapPropagator()

_SQL_KEYWORD = re.compile(r"^\s*(\w+)")

_configured = False


def _provider(service: str) -> TracerProvider:
    """Pure, so tests call it directly with a monkeypatched environment."""
    os.environ.setdefault("OTEL_SERVICE_NAME", service)  # an explicit env value still wins
    provider = TracerProvider(resource=Resource.create())
    # Gated: without an endpoint the exporter falls back to localhost:4318 and logs WARNING/ERROR
    # noise plus a slow exit (measured, 7.7s) on every process that has no collector.
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
    )
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


def configure(service: str) -> None:
    """Idempotent: a second call does nothing and logs nothing (set_tracer_provider would WARN)."""
    global _configured
    if _configured:
        return
    _configured = True
    trace.set_tracer_provider(_provider(service))


@contextmanager
def span(
    name: str, kind: SpanKind, attributes: dict[str, Any], context: Context | None = None
) -> Iterator[Span]:
    """Every manual span goes through this: the SDK's own exception recording stays off, and the
    error status/error.type this codebase's logs already use (logs.error_summary) replace it."""
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(
        name,
        context=context,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except Exception as error:
            current.set_status(trace.Status(trace.StatusCode.ERROR, logs.error_summary(error)))
            current.set_attribute("error.type", type(error).__name__)
            raise


# Registered once on the Engine class (module import happens once per process), so the API, worker
# and migrations engines are all covered without each having to wire it up itself.
@event.listens_for(Engine, "before_cursor_execute")
def _before_cursor_execute(
    conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
) -> None:
    current = trace.get_current_span()
    if not current.get_span_context().is_valid or not current.is_recording():
        # Drops the worker's 30s claim poll and connection pre-ping: neither runs inside a span.
        return
    match = _SQL_KEYWORD.match(statement)
    name = match.group(1).upper() if match else "SQL"
    tracer = trace.get_tracer(__name__)
    started = tracer.start_span(
        name,
        kind=SpanKind.CLIENT,
        # The psycopg dialect is pyformat (%(name)s placeholders); parameters are never read, so no
        # bound value can ever reach db.query.text.
        attributes={"db.system.name": "postgresql", "db.query.text": statement},
        record_exception=False,
        set_status_on_exception=False,
    )
    # Kept on the execution context, not a module-level variable, so nested or concurrent
    # statements (different ExecutionContext each) never cross.
    context._zif_span = started


@event.listens_for(Engine, "after_cursor_execute")
def _after_cursor_execute(
    conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
) -> None:
    started = getattr(context, "_zif_span", None)
    if started is not None:
        started.end()
        context._zif_span = None


@event.listens_for(Engine, "handle_error")
def _handle_error(ctx: Any) -> None:
    # On a connect failure execution_context is None: nothing was started for this attempt.
    started = getattr(ctx.execution_context, "_zif_span", None)
    if started is None:
        return
    error = ctx.original_exception
    started.set_status(trace.Status(trace.StatusCode.ERROR, logs.error_summary(error)))
    started.set_attribute("error.type", type(error).__name__)
    started.end()
    ctx.execution_context._zif_span = None
