import ipaddress
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import (
    accounts,
    admin_auth,
    audit,
    auth,
    availability,
    booking_links,
    booking_page,
    bookings,
    business_settings,
    clients,
    invites,
    logs,
    members,
    payments,
    photos,
    schedule,
    services,
    setup,
    time_off,
    totp,
    tracing,
    turnstile,
)
from app.config import (
    DatabaseSettings,
    Settings,
    StorageSettings,
    StripeSettings,
    TurnstileSettings,
)
from app.db import SessionLocal
from app.errors import ApiError, Error

HEALTH_PATH = "/api/healthz"
REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
CONTENT_LENGTH = re.compile(r"[0-9]+")
# _OTHER for anything else: the client controls the method, so span-name cardinality must stay
# bounded.
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
WRITE = {"POST", "PUT", "PATCH", "DELETE"}
# The largest legitimate bodies are NoteChange (clients.py, ~48.0 KB worst case, astral characters
# escaped by json.dumps) and ServiceIn (~47.6 KB); a browser sends raw UTF-8 (~21 KB worst).
MAX_BODY = 64 * 1024
# ZIF-158: Stripe's events dwarf anything a browser sends. The webhook alone gets this cap.
WEBHOOK_MAX_BODY = 256 * 1024
logger = logging.getLogger(__name__)
# ZB-13. The public dependencies probe answers from its last result for this long, "unavailable"
# included, so a flood costs the database one SELECT per window and the log one line per window.
PROBE_TTL = 5.0
# (monotonic checked_at, ok). The lock guards only this read and write, never the database call:
# holding it there would queue every request behind one slow probe. Probes racing at expiry each
# run, bounded by the 2s statement and 5s connect timeouts.
_probe: tuple[float, bool] | None = None
# Single flight: set under the lock by the one caller that probes. Callers arriving meanwhile answer
# the last result while it is younger than 2 * PROBE_TTL (else unavailable, as with none yet) and
# neither probe nor log. The one probing thread may wait on a pooled connection (pool_timeout, 30s);
# the others never do, and the pool's behaviour for every other route is left alone.
_probing = False
_probe_lock = threading.Lock()
_now = time.monotonic


def reset_dependencies_probe() -> None:
    global _probe, _probing
    with _probe_lock:
        _probe = None
        _probing = False


access_logger = logging.getLogger("app.access")


def body_cap(path: str) -> int:
    return WEBHOOK_MAX_BODY if path == payments.WEBHOOK_PATH else MAX_BODY


class PathBodyLimit:
    """RequestBodyLimitMiddleware with the cap chosen by path (Starlette's per-route size is not
    exposed by FastAPI's routes)."""

    def __init__(self, app: ASGIApp) -> None:
        self.default = RequestBodyLimitMiddleware(app, max_body_size=MAX_BODY)
        self.webhook = RequestBodyLimitMiddleware(app, max_body_size=WEBHOOK_MAX_BODY)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        webhook = scope["type"] == "http" and scope["path"] == payments.WEBHOOK_PATH
        await (self.webhook if webhook else self.default)(scope, receive, send)


class Health(BaseModel):
    status: str
    version: str
    revision: str


class Dependencies(BaseModel):
    status: str
    database: str


def _access_fields(request: Request, status: int, started: float) -> dict[str, Any]:
    route = getattr(request.scope.get("route"), "path", "unmatched")
    tenant_id = getattr(request.state, "tenant_id", None)
    admin_user_id = getattr(request.state, "admin_user_id", None)
    # The route template, never the path or query; no headers, cookies or bodies.
    return {
        "method": request.method,
        "route": route,
        "status": status,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "tenant_id": str(tenant_id) if tenant_id else None,
        "admin_user_id": str(admin_user_id) if admin_user_id else None,
    }


def operation_id(route: APIRoute) -> str:
    # Stable names for the generated TypeScript client: renaming one handler
    # must not churn the others.
    return f"{route.tags[0]}-{route.name}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Read at startup, not in create_app(), so the OpenAPI document builds without a database.
    # hide_parameters: a failed statement's message would otherwise carry its values, password
    # hashes included, into the logs.
    # Starlette sends str(exc) as the lifespan.startup.failed / .shutdown.failed ASGI message, and
    # uvicorn logs that message with no exc_info, bypassing our formatter (a malformed database URL
    # would otherwise leak straight to stdout). Log the real error safely ourselves, then raise a
    # message-free one so nothing repeats the leak.
    try:
        engine = create_engine(
            DatabaseSettings().database_url,
            pool_pre_ping=True,
            hide_parameters=True,
            # ZIF-114. No request waits longer than this for any lock, the tenant advisory lock
            # included: past it the statement fails with 55P03, which every route answers as 503
            # busy (lock_timed_out below). Without it one stalled holder pins a pooled
            # connection per waiting request until every tenant's requests hang on checkout.
            # Booking writers hold the lock for milliseconds, so 5s is hundreds of queued bookings.
            # ZB-13. connect_timeout bounds a connection attempt to an unreachable database.
            connect_args={"options": "-c lock_timeout=5s", "connect_timeout": 5},
        )
        SessionLocal.configure(bind=engine)
    except Exception:
        logger.critical("startup failed", exc_info=True)
        raise RuntimeError("startup failed") from None
    setup_token = setup.token_state()
    totp_key = totp.key_state()
    storage_on = StorageSettings().configured()
    payments_on = StripeSettings().configured()
    # Here, not at import: `python -m app.main` prints the OpenAPI document to stdout.
    logger.info(
        "api started",
        extra={
            **logs.settings_fields(),
            "trusted_proxies": Settings().trusted_proxies,
            # Never the secret. "off": public bookings are refused unless ZIF_TURNSTILE_DISABLED.
            "turnstile": "on" if turnstile.enabled() else "off",
            # Never the token. "unset"/"weak": first-run setup is refused (ZIF-70).
            "setup_token": setup_token,
            # Never the key. "unset"/"invalid": admin TOTP answers 503 (ZIF-71).
            "totp_key": totp_key,
            # "off": photo uploads answer 503 photos_unavailable (ZIF-133).
            "storage": "on" if storage_on else "off",
            # "off": owner payment routes answer 503 and booking pages book with no payment step.
            "payments": "on" if payments_on else "off",
        },
    )
    # ZIF-116: logged, never a refusal to start. The deployments run one replica with Recreate, so a
    # failed start would take the whole API down over a control only public booking needs.
    if not turnstile.enabled() and not TurnstileSettings().turnstile_disabled:
        logger.error("turnstile secret unset: public bookings refused")
    # Only while setup is still to be done: operators may remove the token afterwards. Never a
    # refusal to start, and a database error skips the line.
    if setup_token != "set" and setup.needed_at_startup(DatabaseSettings().database_url):
        logger.error("setup token unset: first-run setup refused")
    # Unconditional, unlike the setup token: the key is needed for the deployment's whole life.
    # Never a refusal to start (one replica, Recreate): recovery codes still let admins in.
    if totp_key != "set":
        logger.error("totp key unset: admin second factor unavailable")
    # A legitimate state until the object store exists: logged, never a refusal to start.
    if not storage_on:
        logger.warning("storage unset: photo uploads refused")
    if not payments_on:
        logger.warning("stripe unset: payments off")
    try:
        yield
    finally:
        try:
            SessionLocal.configure(bind=None)
            engine.dispose()
        except Exception:
            logger.critical("shutdown failed", exc_info=True)
            raise RuntimeError("shutdown failed") from None


def create_app() -> FastAPI:
    settings = Settings()
    router = APIRouter(prefix="/api")

    @router.get("/healthz", tags=["health"])
    def healthz() -> Health:
        return Health(status="ok", version=settings.app_version, revision=settings.app_revision)

    # INFRA-120. The truth surface for the post-deploy smoke, as TBD's /health/dependencies: 503
    # when a required dependency is unusable. /healthz stays cheap and database-free.
    # Unauthenticated: a coarse code only, never the driver message or host.
    # The 503 is the Error shape (the contract requires it), so unlike TBD's it does not name the
    # dependency; the log line does.
    # The result is cached for PROBE_TTL and the probe is time-bounded (ZB-13).
    @router.get(
        "/health/dependencies",
        tags=["health"],
        response_model=Dependencies,
        responses={503: {"model": Error}},
    )
    def dependencies() -> Any:
        global _probe, _probing
        with _probe_lock:
            cached = _probe
            probe_now = not _probing and (cached is None or _now() - cached[0] >= PROBE_TTL)
            if probe_now:
                _probing = True
        if probe_now:
            ok = True
            try:
                with SessionLocal() as session:
                    # SET LOCAL ends with this transaction; the pooled connection keeps its default.
                    session.execute(text("SET LOCAL statement_timeout = '2s'"))
                    session.execute(text("SELECT 1"))
            except Exception:
                ok = False
                logger.error(
                    "dependencies unhealthy", extra={"dependency": "database"}, exc_info=True
                )
            finally:
                with _probe_lock:
                    _probe = (_now(), ok)
                    _probing = False
        else:
            # Fresh, or another caller's probe is in flight. That one can hang (pool_timeout, a
            # half-open socket), so its last result is served only while younger than 2 * PROBE_TTL.
            ok = cached is not None and cached[1] and _now() - cached[0] < 2 * PROBE_TTL
        if not ok:
            return JSONResponse({"code": "database_unavailable"}, status_code=503)
        return Dependencies(status="ok", database="ok")

    # No deploy version in the OpenAPI document: it is a committed contract
    # and must not vary per environment.
    # redirect_slashes=False (ZIF-136): a trailing slash is a plain 404, never a 307 whose
    # Location names the internal API origin.
    app = FastAPI(
        title="ziftbook",
        generate_unique_id_function=operation_id,
        lifespan=lifespan,
        redirect_slashes=False,
        # The telemetry standard (cloud-infra docs/architecture.md, Telemetry). Native metrics only,
        # recorded on the MeterProvider tracing.configure() registered (its View is the attribute
        # allowlist). The app owns the SERVER span and the logs: native tracing would add a second
        # SERVER span carrying url.path and url.query, native logs export exception messages, and
        # auto_configure would add a second exporter to our providers.
        telemetry={
            "tracing": False,
            "metrics": True,
            "logs": False,
            "operation_spans": False,
            "auto_configure": False,
            "exclude": lambda scope: scope["path"] == HEALTH_PATH,
        },
    )
    # The body cap can answer any write route, whatever it declares: add the 413 to each, on the
    # router itself (app.routes holds only wrappers by now), before it is included.
    for each in (
        router,
        auth.router,
        accounts.router,
        audit.router,
        business_settings.router,
        clients.router,
        services.router,
        photos.router,
        members.router,
        invites.router,
        schedule.router,
        schedule.opening_router,
        time_off.router,
        availability.router,
        availability.merchant_router,
        booking_page.router,
        bookings.router,
        bookings.merchant_router,
        booking_links.router,
        payments.router,
        payments.public_router,
        payments.webhook_router,
        admin_auth.admin_sign_in_router,
        admin_auth.admin_half_router,
        admin_auth.admin_router,
        admin_auth.admin_write_router,
        setup.router,
    ):
        for route in each.routes:
            if isinstance(route, APIRoute) and WRITE & set(route.methods or ()):
                route.responses.setdefault(413, {"model": Error})
        app.include_router(each)

    # Registered before json_only, so innermost: the byte counter for a chunked body (the proxy
    # strips content-length and streams). Only json_only's fast path stops a body that was declared
    # too large before the handler runs; here Starlette would swap the response after the fact.
    app.add_middleware(PathBodyLimit)

    @app.middleware("http")
    async def json_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # CSRF defence: browsers send JSON cross-origin only after a CORS preflight, never granted.
        # Parse the media type: "text/plain; application/json" is still a plain form type.
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        declared = request.headers.get("content-length", "")
        # Every method, before the handler: a write that takes no body (DELETE /api/sessions) is
        # stopped only here, not by the byte counter. A non-numeric length counts as absent.
        # Compared by length first (leading zeros dropped): int() of a huge digit string is slow.
        digits = declared.lstrip("0")
        cap = body_cap(request.url.path)
        if CONTENT_LENGTH.fullmatch(declared) and (
            len(digits) > len(str(cap)) or int(digits or "0") > cap
        ):
            response: Response = JSONResponse({"code": "content_too_large"}, status_code=413)
        elif request.method not in ("GET", "HEAD", "OPTIONS") and media_type != "application/json":
            response = JSONResponse({"code": "unsupported_media_type"}, status_code=415)
        else:
            response = await call_next(request)
        # No API response is a page to link from.
        response.headers["Referrer-Policy"] = "no-referrer"
        # ZIF-54 spec S4: every guest-link answer, errors included, is private to its bearer. So is
        # every admin answer (ZIF-71): some carry a TOTP secret or recovery codes.
        if request.url.path.startswith(
            ("/api/public/booking-link", "/api/public/booking-pages", "/api/admin")
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Registered after json_only, so outside it (only ProxyHeadersMiddleware is further out):
        # it logs json_only's 415s too.
        given = request.headers.get("x-request-id", "")
        request_id = given if REQUEST_ID.fullmatch(given) else uuid.uuid4().hex
        # A fresh context, set and not reset: the 500 handler and uvicorn's error line run
        # after this returns, in this task, and still need the id.
        logs.CONTEXT.set({"request_id": request_id})
        request.state.request_id = request_id
        started = time.perf_counter()
        status = 500  # if call_next raises, the 500 handler answers

        async def run() -> Response:
            nonlocal status
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response

        # No span for the healthcheck: nothing else below runs for it either.
        if request.url.path == HEALTH_PATH:
            try:
                return await run()
            finally:
                access_logger.log(
                    logging.DEBUG, "access", extra=_access_fields(request, status, started)
                )

        ctx = tracing.PROPAGATOR.extract({"traceparent": request.headers.get("traceparent", "")})
        method = request.method if request.method in METHODS else "_OTHER"
        raised = False
        with tracing.span(
            method, SpanKind.SERVER, {"http.request.method": method}, context=ctx
        ) as current_span:
            span_context = current_span.get_span_context()
            # Set, never reset, exactly like request_id: the 500 handler and uvicorn's error line
            # run after this span has exited and still need the ids.
            logs.CONTEXT.set(
                {
                    **logs.CONTEXT.get({}),
                    "trace_id": format(span_context.trace_id, "032x"),
                    "span_id": format(span_context.span_id, "016x"),
                }
            )
            try:
                return await run()
            except Exception:
                raised = True  # tracing.span already set the status below; don't overwrite it
                raise
            finally:
                route = getattr(request.scope.get("route"), "path", "unmatched")
                current_span.update_name(f"{method} {route}")
                current_span.set_attribute("http.route", route)
                current_span.set_attribute("http.response.status_code", status)
                if status >= 500 and not raised:
                    current_span.set_status(trace.Status(trace.StatusCode.ERROR))
                    current_span.set_attribute("error.type", "500")
                access_logger.log(
                    logging.INFO, "access", extra=_access_fields(request, status, started)
                )

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        # Only the keys that are set: every other error body stays exactly {"code": ...}, which is
        # what the whole suite asserts (an unconditional weekday alone failed 135 tests, ZIF-105).
        body = Error(
            code=error.code,
            weekday=error.weekday,
            bookings=error.bookings,
            total=error.total,
        ).model_dump(mode="json", exclude_none=True)
        return JSONResponse(body, status_code=error.status_code)

    @app.exception_handler(413)
    async def too_large(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"code": "content_too_large"}, status_code=413)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # A code, not FastAPI's description of the body: errors are codes.
        return JSONResponse({"code": "invalid_request"}, status_code=422)

    # A lock wait past the engine's lock_timeout (ZIF-114), on any route that does not catch it
    # itself. Every other OperationalError (a lost connection, say) goes on to internal_error.
    @app.exception_handler(OperationalError)
    async def lock_timed_out(request: Request, error: OperationalError) -> JSONResponse:
        if getattr(error.orig, "sqlstate", None) != "55P03":
            raise error
        # Not an error: the request did nothing. Still worth a line, it means a holder stalled.
        logger.warning("lock wait timed out")
        return JSONResponse({"code": "busy"}, status_code=503)

    # Anything unexpected, including a commit that fails after the endpoint returned. This runs
    # outside the middleware above, so it sets Referrer-Policy itself.
    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        logger.error("unhandled error", exc_info=error)
        headers = {"Referrer-Policy": "no-referrer"}
        if request_id := getattr(request.state, "request_id", None):
            headers["X-Request-ID"] = request_id
        return JSONResponse({"code": "internal"}, status_code=500, headers=headers)

    # Last, so it is outermost: every middleware and handler sees the visitor's address as
    # request.client. A typo (or "*") stops startup, rather than trusting nobody (or everybody)
    # without a word.
    trusted = [entry.strip() for entry in settings.trusted_proxies.split(",") if entry.strip()]
    for entry in trusted:
        ipaddress.ip_network(entry)
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted)

    return app


def openapi_document() -> str:
    return json.dumps(create_app().openapi(), indent=2, ensure_ascii=False) + "\n"


logs.configure()  # emits nothing
tracing.configure("ziftbook-api")
app = create_app()

if __name__ == "__main__":
    print(openapi_document(), end="")
