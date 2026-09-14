import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import create_engine

from app.config import DatabaseSettings, Settings
from app.db import SessionLocal


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
    engine = create_engine(DatabaseSettings().database_url, pool_pre_ping=True)
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
    return app


def openapi_document() -> str:
    return json.dumps(create_app().openapi(), indent=2, ensure_ascii=False) + "\n"


app = create_app()

if __name__ == "__main__":
    print(openapi_document(), end="")
