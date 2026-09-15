import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import create_engine

from app import accounts, auth, business_settings
from app.config import DatabaseSettings, Settings
from app.db import SessionLocal
from app.errors import ApiError


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
    engine = create_engine(
        DatabaseSettings().database_url, pool_pre_ping=True, hide_parameters=True
    )
    SessionLocal.configure(bind=engine)
    try:
        yield
    finally:
        SessionLocal.configure(bind=None)
        engine.dispose()


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
    app.include_router(business_settings.router)

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
        return JSONResponse(
            {"code": "internal"}, status_code=500, headers={"Referrer-Policy": "no-referrer"}
        )

    return app


def openapi_document() -> str:
    return json.dumps(create_app().openapi(), indent=2, ensure_ascii=False) + "\n"


app = create_app()

if __name__ == "__main__":
    print(openapi_document(), end="")
