# syntax=docker/dockerfile:1
# Build with the backend directory as a named context, for the committed OpenAPI contract:
#   docker build --build-context backend=backend frontend
# Local development: `next dev` with hot reload, source synced by `docker compose up --watch`.
FROM node:24-alpine AS dev
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /src/frontend
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile
COPY . .
# openapi-ts reads ../backend/openapi.json, the same relative path as in the repo.
COPY --from=backend openapi.json /src/backend/openapi.json
CMD ["pnpm", "dev", "--hostname", "0.0.0.0"]

FROM dev AS build
RUN pnpm build && mkdir -p public

# Release image: Next standalone server only, non-root.
FROM node:24-alpine AS prod
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
WORKDIR /app
COPY --from=build --chown=node:node /src/frontend/.next/standalone ./
COPY --from=build --chown=node:node /src/frontend/.next/static ./.next/static
COPY --from=build --chown=node:node /src/frontend/public ./public
COPY --from=build /src/frontend/licenses ./licenses
USER node
EXPOSE 3000
CMD ["node", "server.js"]
