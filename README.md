# ziftbook

Appointment booking for small service businesses.

## Getting started

Prerequisites: `git`, `make`, [uv](https://docs.astral.sh/uv/), Node 24 with [pnpm](https://pnpm.io/), Docker.

```sh
make setup   # enable git hooks, install api and web dependencies
make lint typecheck test
```

Run the dev stack (Postgres 18 and the api, with source changes synced and reloaded):

```sh
make up     # postgres, role bootstrap, migrations, api: http://localhost:8000/api/healthz
make down
```

`docker-compose.yaml` is this local stack. `docker-compose-prod.yaml` runs a released version from GHCR
(`ghcr.io/fjcloudaiconsulting/ziftbook/{backend,frontend}`); its header lists the variables it needs:

```sh
docker login ghcr.io
ZIF_IMAGE_TAG=vX.Y.Z docker compose -f docker-compose-prod.yaml up -d   # http://localhost:3000
```

Database migrations (Alembic, in `backend/migrations`). Read the expand/contract rule in CONTRIBUTING.md first:

```sh
make migration name="add bookings"   # new file in backend/migrations/versions
make migrate                         # apply
```

Tests need Docker running: `make test` starts Postgres and bootstraps the roles.

Run the web app (English, Dutch, Portuguese) against that api:

```sh
make web    # http://localhost:3000, /api is forwarded to the api
```

After changing an api route, regenerate the committed contract the web client is built from:

```sh
make openapi
```

Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat(api): add health endpoint`). Releases and the changelog are generated from them.
