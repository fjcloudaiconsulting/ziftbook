# ziftbook

Appointment booking for small service businesses.

## Getting started

Prerequisites: `git`, `make`, [uv](https://docs.astral.sh/uv/), Node 24 with [pnpm](https://pnpm.io/), Docker.

```sh
make setup   # enable git hooks, install api and web dependencies
make lint typecheck test
```

Run the whole app locally (Postgres 18, migrations, backend and the frontend in English, Dutch and Portuguese), with
source changes synced and reloaded:

```sh
make up     # http://localhost:3000 (backend directly: http://localhost:8000/api/healthz)
            # emails sent locally land in Mailpit: http://localhost:8025
make down
```

`docker-compose.yaml` is this local stack. `docker-compose-prod.yaml` runs a released version from GHCR
(`ghcr.io/fjcloudaiconsulting/ziftbook/{backend,frontend,migrations}`); its header lists the variables it needs.
The `migrations` image runs `alembic upgrade head` as an init container: the backend starts only if it succeeds, and
rerunning it is a no-op. The same image is meant for the Kubernetes init container.

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

After changing an api route, regenerate the committed contract the web client is built from:

```sh
make openapi
```

Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat(api): add health endpoint`). Releases and the changelog are generated from them.
