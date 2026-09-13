# ziftbook

Appointment booking for small service businesses.

## Getting started

Prerequisites: `git`, `make`, [uv](https://docs.astral.sh/uv/).

```sh
make setup   # enable git hooks, install api dependencies
make lint typecheck test
```

Run the api locally:

```sh
uv run --directory apps/api uvicorn app.main:app --reload   # http://localhost:8000/api/healthz
```

Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat(api): add health endpoint`). Releases and the changelog are generated from them.
