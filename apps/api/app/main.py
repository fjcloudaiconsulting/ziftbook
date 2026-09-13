from fastapi import APIRouter, FastAPI

from app.config import Settings


def create_app() -> FastAPI:
    settings = Settings()
    router = APIRouter(prefix="/api")

    @router.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": settings.app_version}

    app = FastAPI(title="ziftbook", version=settings.app_version)
    app.include_router(router)
    return app


app = create_app()
