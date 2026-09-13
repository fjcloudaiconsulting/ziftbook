# Contributing

## Commits and pull requests

- Keep pull requests small: one change a reviewer can follow in one sitting.
- PR titles follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat(api): add bookings endpoint`).
  PRs are squash-merged, so the title becomes the commit on `main` and the changelog entry. CI checks it, and
  `make setup` installs the same check as a local commit hook.
- `make lint typecheck test` runs locally what CI runs.

## Database migrations: expand, then contract

During a deploy, old and new versions of the app run against the same database at the same time. A migration
must never break the version that is still running.

1. **Expand** in one release: add tables, nullable columns, new indexes, new enum values. Ship the migration
   together with, or before, the code that uses it.
2. **Contract** in a later release, once no running version depends on the old shape: drop columns or tables,
   rename (as add + backfill + drop), tighten `NOT NULL`, remove enum values.

Specifics:

- A new `NOT NULL` column needs a default, or is added nullable, backfilled, then tightened in a later release.
- `CREATE INDEX CONCURRENTLY` cannot run inside a transaction; put it in its own migration using Alembic's
  `autocommit_block()`.
- Migrations run as `ziftbook_migrate`, never on app startup. The app connects as `ziftbook_app`, which owns
  nothing: never grant it ownership, `BYPASSRLS` or DDL rights.

## API contract

`apps/api/openapi.json` is committed and the web client is generated from it. After changing an API route or
schema, run `make openapi` and commit the result; a test fails if it is stale. Never hand-write API types in the
web app.

## Translations

English (`en`) is the source for every catalog (`apps/web/messages`, `landing/strings`). Add a key to English
first, then to `nl` and `pt` with the same `{placeholders}`; `node scripts/check-catalogs.mjs` (part of
`make lint`) enforces it.

## Configuration

Every environment variable our own code reads starts with `ZIF_` (`ZIF_API_URL`, `ZIF_APP_VERSION`,
`ZIF_DATABASE_URL`), so it is obvious which variables belong to ziftbook when configuring an environment. The API
sets this once with `env_prefix="ZIF_"` on its settings class. Variables defined by other tools keep their own
names (`NODE_ENV`, `POSTGRES_*`, `PG*`, `CLOUDFLARE_API_TOKEN`). `node scripts/check-env-names.mjs` (part of
`make lint`) enforces it.

Deployment settings are environment variables read at runtime; never add `NEXT_PUBLIC_*` variables, which Next.js
bakes into the build. Infrastructure is code in this repository; only secrets are set by hand.
