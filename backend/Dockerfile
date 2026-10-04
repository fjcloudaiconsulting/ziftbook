# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM python:3.14-slim@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151 AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /uvx /bin/
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    PYTHONUNBUFFERED=1
WORKDIR /app

# Local development: dev dependencies, reload on source changes synced by `docker compose up --watch`.
FROM base AS dev
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project
COPY . .
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "app", "--no-proxy-headers"]

FROM base AS build
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Release images: runtime dependencies and app code only, non-root.
FROM python:3.14-slim@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151 AS runtime
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY app ./app
COPY licenses ./licenses
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER app

# Migrations image: the init container (Kubernetes, Compose) that must succeed before the backend starts.
# `alembic upgrade head` is a no-op at head, and env.py's advisory lock makes concurrent runs wait their turn.
FROM runtime AS migrations
COPY pyproject.toml ./
COPY migrations ./migrations
CMD ["alembic", "upgrade", "head"]

# Backend image. The worker reuses it with another command.
FROM runtime AS prod
ARG APP_VERSION=dev
ARG APP_REVISION=dev
ENV ZIF_APP_VERSION=$APP_VERSION \
    ZIF_APP_REVISION=$APP_REVISION
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
