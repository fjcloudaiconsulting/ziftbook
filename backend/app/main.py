import ipaddress
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import create_engine
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import (
    accounts,
    audit,
    auth,
    business_settings,
    invites,
    logs,
    members,
    schedule,
    services,
    time_off,
)
from app.config import DatabaseSettings, Settings
from app.db import SessionLocal
from app.errors import ApiError

REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
logger = logging.getLogger(__name__)
access_logger = logging.getLogger("app.access")


class Health(BaseModel):
    status: str
    version: str


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
            DatabaseSettings().database_url, pool_pre_ping=True, hide_parameters=True
        )
        SessionLocal.configure(bind=engine)
    except Exception:
        logger.critical("startup failed", exc_info=True)
        raise RuntimeError("startup failed") from None
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
        return Health(status="ok", version=settings.app_version)

    # No deploy version in the OpenAPI document: it is a committed contract
    # and must not vary per environment.
    app = FastAPI(title="ziftbook", generate_unique_id_function=operation_id, lifespan=lifespan)
    app.include_router(router)
    app.include_router(auth.router)
    app.include_router(accounts.router)
    app.include_router(audit.router)
    app.include_router(business_settings.router)
    app.include_router(services.router)
    app.include_router(members.router)
    app.include_router(invites.router)
    app.include_router(schedule.router)
    app.include_router(time_off.router)

    @app.middleware("http")
    async def json_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # CSRF defence: browsers send JSON cross-origin only after a CORS preflight, never granted.
        # Parse the media type: "text/plain; application/json" is still a plain form type.
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if request.method not in ("GET", "HEAD", "OPTIONS") and media_type != "application/json":
            response: Response = JSONResponse({"code": "unsupported_media_type"}, status_code=415)
        else:
            response = await call_next(request)
        # No API response is a page to link from.
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Registered last, so outermost: it logs json_only's 415s too.
        given = request.headers.get("x-request-id", "")
        request_id = given if REQUEST_ID.fullmatch(given) else uuid.uuid4().hex
        # A fresh context, set and not reset: the 500 handler and uvicorn's error line run
        # after this returns, in this task, and still need the id.
        logs.CONTEXT.set({"request_id": request_id})
        request.state.request_id = request_id
        started = time.perf_counter()
        status = 500  # if call_next raises, the 500 handler answers
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            tenant_id = getattr(request.state, "tenant_id", None)
            # The route template, never the path or query; no headers, cookies or bodies.
            access_logger.log(
                logging.DEBUG if route == "/api/healthz" else logging.INFO,
                "access",
                extra={
                    "method": request.method,
                    "route": route,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "tenant_id": str(tenant_id) if tenant_id else None,
                },
            )

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse({"code": error.code}, status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # A code, not FastAPI's description of the body: errors are codes.
        return JSONResponse({"code": "invalid_request"}, status_code=422)

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
app = create_app()

if __name__ == "__main__":
    print(openapi_document(), end="")
