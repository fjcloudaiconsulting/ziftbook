# syntax=docker/dockerfile:1
# Build with the api directory as a named context, for the committed OpenAPI contract:
#   docker build --build-context api=apps/api apps/web
FROM node:24-alpine AS build
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /src/web
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile
COPY . .
# openapi-ts reads ../api/openapi.json, the same relative path as in the repo.
COPY --from=api openapi.json /src/api/openapi.json
RUN pnpm build && mkdir -p public

# Release image: Next standalone server only, non-root.
FROM node:24-alpine AS prod
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
WORKDIR /app
COPY --from=build --chown=node:node /src/web/.next/standalone ./
COPY --from=build --chown=node:node /src/web/.next/static ./.next/static
COPY --from=build --chown=node:node /src/web/public ./public
COPY --from=build /src/web/licenses ./licenses
USER node
EXPOSE 3000
CMD ["node", "server.js"]
