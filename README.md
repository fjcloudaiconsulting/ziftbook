# ziftbook

Appointment booking for small service businesses.

## Getting started

Prerequisites: `git`, `make`, [uv](https://docs.astral.sh/uv/), Docker.

```sh
make setup   # enable git hooks, install api dependencies
make lint typecheck test
```

Run the dev stack (Postgres 18 and the api, with source changes synced and reloaded):

```sh
make up     # http://localhost:8000/api/healthz
make down
```

Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat(api): add health endpoint`). Releases and the changelog are generated from them.
