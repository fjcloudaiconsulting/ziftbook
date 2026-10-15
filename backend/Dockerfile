# syntax=mirror.gcr.io/docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM mirror.gcr.io/library/python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a /uv /uvx /bin/
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
FROM mirror.gcr.io/library/python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime
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
# service.version on its telemetry, like the backend's (app/tracing.py).
ARG APP_VERSION=dev
ENV ZIF_APP_VERSION=$APP_VERSION
CMD ["alembic", "upgrade", "head"]

# Backend image. The worker reuses it with another command.
FROM runtime AS prod
ARG APP_VERSION=dev
ARG APP_REVISION=dev
ENV ZIF_APP_VERSION=$APP_VERSION \
    ZIF_APP_REVISION=$APP_REVISION
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
