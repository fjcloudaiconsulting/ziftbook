# syntax=mirror.gcr.io/docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# Build with the backend directory as a named context, for the committed OpenAPI contract:
#   docker build --build-context backend=backend frontend
# Local development: `next dev` with hot reload, source synced by `docker compose up --watch`.
FROM mirror.gcr.io/library/node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS dev
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
FROM mirror.gcr.io/library/node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS prod
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
