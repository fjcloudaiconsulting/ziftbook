"""One logging setup for every process: the API, the worker and migrations.

configure() once per process. Context (request_id, job_id, job_kind, tenant_id) rides in one
ContextVar and is copied onto each record by a filter on our handler. Records never carry an
exception's message: only its class, its frames and, for database errors, the SQLSTATE and
constraint/table names.
"""

import json
import logging
import sys
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal

from opentelemetry import trace
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from app import config
from app.config import LogSettings

CONTEXT: ContextVar[dict[str, str]] = ContextVar("log_context")  # no default: ruff B039
HANDLER = "zif"
# Our own deployment-config classes (Settings, WorkerSettings, ...), not lookalikes like
# BusinessSettings (a plain BaseModel with UI data, whose title also ends in "Settings").
_SETTINGS_NAMES = {
    name
    for name, obj in vars(config).items()
    if isinstance(obj, type) and issubclass(obj, BaseSettings) and obj.__module__ == config.__name__
}
# Everything a LogRecord has on its own; any other attribute came from extra=.
STANDARD = set(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "color_message",
    "context",
}
RESERVED = {"ts", "level", "logger", "msg"}  # set by _head(); context/extras never overwrite them
uncaught_logger = logging.getLogger("app.uncaught")


@contextmanager
def bound(**fields: str) -> Iterator[None]:
    """Add fields to every record logged inside, in this context and what it spawns.

    Restores with set(), not ContextVar.reset(): FastAPI enters and exits a sync generator
    dependency in two different copied contexts (fastapi/concurrency.py:18-29), where reset()
    raises ValueError.
    """
    before = CONTEXT.get({})
    CONTEXT.set({**before, **fields})
    try:
        yield
    finally:
        CONTEXT.set(before)


def _add_context(record: logging.LogRecord) -> bool:
    context = CONTEXT.get({})
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        # A new dict: mutating the one held in the contextvar would leave stale ids on later
        # lines logged after this span (or another one) has ended.
        record.context = {
            **context,
            "trace_id": format(span_context.trace_id, "032x"),
            "span_id": format(span_context.span_id, "016x"),
        }
    else:
        # No current span: keep whatever ids CONTEXT already carries (access_log sets them once
        # and never resets, so the "unhandled error" line after the span exited still has them),
        # and add neither key when it never carried any.
        record.context = dict(context)
    return True


def _causes(error: BaseException | None) -> Iterator[BaseException]:
    """Walk __cause__/__context__ (honoring __suppress_context__), each error at most once: a
    manual __cause__ cycle (see test_an_uncaught_error_...cycle) must still terminate."""
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        suppressed = error.__suppress_context__
        error = error.__cause__ or (None if suppressed else error.__context__)


def _chain(error: BaseException | None) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for cause in _causes(error):
        kind = type(cause)
        link: dict[str, Any] = {
            "type": f"{kind.__module__}.{kind.__qualname__}",
            "frames": [
                {"file": f.filename, "line": f.lineno, "function": f.name, "code": f.line}
                for f in traceback.extract_tb(cause.__traceback__)
            ],
        }
        # psycopg errors (sqlalchemy's DBAPIError has one as __cause__): identifiers only,
        # never diag.message_detail, which quotes row values ("Key (email)=(...)").
        if sqlstate := getattr(cause, "sqlstate", None):
            link["sqlstate"] = sqlstate
        if diag := getattr(cause, "diag", None):
            link["constraint"] = diag.constraint_name
            link["table"] = diag.table_name
        links.append(link)
    return links


def error_summary(error: BaseException) -> str:
    """Short and human-readable, never the message: the error's class, plus SQLSTATE and
    constraint for a database error (ZIF-93: jobs.last_error, which erasure doesn't cover).
    Reuses _chain's walk so this and a log record find the same sqlstate/constraint.

    e.g. "SMTPRecipientsRefused" or "IntegrityError sqlstate=23505 constraint=uq_users_email".
    """
    for link in _chain(error):
        if sqlstate := link.get("sqlstate"):
            summary = f"{type(error).__name__} sqlstate={sqlstate}"
            if constraint := link.get("constraint"):
                summary += f" constraint={constraint}"
            return summary
    return type(error).__name__


def _escape(value: object) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


class Formatter(logging.Formatter):
    def __init__(self, kind: Literal["json", "text"]) -> None:
        super().__init__()
        self.kind = kind

    def format(self, record: logging.LogRecord) -> str:
        # A non-string message (logger.warning(error)) is logged as its class, never str().
        template = record.msg if isinstance(record.msg, str) else type(record.msg).__name__
        try:
            message = record.getMessage() if isinstance(record.msg, str) else template
            fields: dict[str, Any] = {
                **self._head(record, message),
                **{k: v for k, v in getattr(record, "context", {}).items() if k not in RESERVED},
                **{
                    k: v for k, v in vars(record).items() if k not in STANDARD and k not in RESERVED
                },
            }
            if record.exc_info and record.exc_info[1] is not None:
                fields["exc"] = _chain(record.exc_info[1])
            return self._render({k: v for k, v in fields.items() if v is not None})
        except Exception:
            # Never raise, never the arguments: the template only, still JSON in json mode.
            return self._render(self._head(record, template))

    @staticmethod
    def _head(record: logging.LogRecord, message: str) -> dict[str, Any]:
        ts = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        return {
            "ts": ts.replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": message,
        }

    def _render(self, fields: dict[str, Any]) -> str:
        if self.kind == "json":
            return json.dumps(fields, ensure_ascii=False, default=str)
        fields = dict(fields)
        exc = fields.pop("exc", [])
        ts, level, name, message = (fields.pop(k) for k in ("ts", "level", "logger", "msg"))
        line = f"{ts} {level} {name}: {_escape(message)}"
        line += "".join(f" {k}={_escape(v)}" for k, v in fields.items())
        for link in exc:
            line += f"\n  {link['type']}"
            for f in link["frames"]:
                where = f"{_escape(f['file'])}:{f['line']} {_escape(f['function'])}"
                line += f"\n    {where}: {_escape(f['code'])}"
        return line


def stream_handler(stream: Any, kind: Literal["json", "text"]) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(Formatter(kind))
    handler.addFilter(_add_context)
    return handler


def _invalid_env(error: BaseException) -> list[str]:
    """ZIF_<FIELD> names for one of our Settings classes' ValidationError in the chain (as
    configure() reports for LogSettings), names only, never the rejected input. A ValidationError
    from something other than one of our Settings classes (an ad hoc TypeAdapter, or a lookalike
    like BusinessSettings, a plain BaseModel) names nothing, and so does a model-level error with
    no field location."""
    names: set[str] = set()
    for cause in _causes(error):
        if isinstance(cause, ValidationError) and cause.title in _SETTINGS_NAMES:
            names |= {f"ZIF_{str(e['loc'][0]).upper()}" for e in cause.errors() if e["loc"]}
    return sorted(names)


def _excepthook(kind: type[BaseException], error: BaseException, tb: TracebackType | None) -> None:
    if issubclass(kind, KeyboardInterrupt):
        sys.__excepthook__(kind, error, tb)  # a deliberate interrupt, not a crash
        return
    try:
        extra = {"invalid_env": names} if (names := _invalid_env(error)) else {}
    except Exception:
        # Never let building extras fall through to Python's own excepthook, which prints the
        # message (and, for a ValidationError, the rejected input).
        extra = {}
    uncaught_logger.critical("uncaught error", exc_info=(kind, error, tb), extra=extra)


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is not None and not isinstance(args.exc_value, SystemExit):
        info = (args.exc_type, args.exc_value, args.exc_traceback)
        uncaught_logger.critical("uncaught error", exc_info=info)


def _unraisable_hook(args: sys.UnraisableHookArgs) -> None:
    # Not CRITICAL: a finalizer/__del__ error doesn't take the process down. Never args.err_msg
    # or repr(args.object): either can carry a value (e.g. a row's __repr__).
    if args.exc_value is None:
        return
    info = (args.exc_type, args.exc_value, args.exc_traceback)
    uncaught_logger.error("unraisable error", exc_info=info)


def settings_fields() -> dict[str, Any]:
    """The effective settings every process's startup line carries."""
    settings = LogSettings()
    return {
        "version": settings.app_version,
        "log_level": settings.log_level,
        "log_format": settings.log_format,
        "log_sql": settings.log_sql,
    }


def configure() -> None:
    """Idempotent. Emits nothing: `python -m app.main` prints the OpenAPI document to stdout."""
    try:
        settings = LogSettings()
    except ValidationError as error:
        # Not str(error): pydantic quotes the rejected input.
        names = ", ".join(sorted({f"ZIF_{str(e['loc'][0]).upper()}" for e in error.errors()}))
        raise SystemExit(
            f"invalid {names}: ZIF_LOG_LEVEL is DEBUG, INFO, WARNING or ERROR; "
            "ZIF_LOG_FORMAT is json or text"
        ) from None
    level = logging.getLevelNamesMapping()[settings.log_level]
    logging.raiseExceptions = False  # a broken stream never takes a request down
    # Crashes outside any handler (a worker settings ValidationError, which quotes its input; a
    # migration error: alembic's run_cmd only catches CommandError) come out as one JSON line,
    # class and frames only. Plain assignment, so a second configure() doesn't wrap twice.
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    sys.unraisablehook = _unraisable_hook

    root = logging.getLogger()
    for old in [h for h in root.handlers if h.name == HANDLER]:  # ours only: pytest's stay
        root.removeHandler(old)
    handler = stream_handler(sys.stdout, settings.log_format)
    handler.name = HANDLER
    root.addHandler(handler)
    # Third-party DEBUG never shows; their INFO (alembic's "Running upgrade") does.
    root.setLevel(max(level, logging.INFO))
    # INFO, never DEBUG: sqlalchemy.engine logs result rows at DEBUG.
    sql = logging.INFO if settings.log_sql and level == logging.DEBUG else logging.WARNING

    for name, value in {
        "app": level,
        "uvicorn": level,
        "uvicorn.error": logging.NOTSET,  # follows "uvicorn"; never below DEBUG: no TRACE (5)
        "sqlalchemy": logging.WARNING,
        "sqlalchemy.engine": sql,
        "psycopg": logging.WARNING,
        "httpx2": logging.WARNING,  # the test client; its INFO line has the URL and query
    }.items():
        logging.getLogger(name).setLevel(value)
    # uvicorn configured these before importing the app (see "uvicorn" in the spec): drop its
    # format.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    # Its access line has the client IP and the raw path with query. With no handler and no
    # propagation, hasHandlers() is False and uvicorn never builds the line
    # (h11_impl.py:57, httptools_impl.py:61).
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
