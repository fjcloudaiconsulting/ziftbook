# Contributing

## Commits and pull requests

- Keep pull requests small: one change a reviewer can follow in one sitting.
- PR titles follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat(api): add bookings endpoint`).
  PRs are squash-merged, so the title becomes the commit on `main` and the changelog entry. CI checks it, and
  `make setup` installs the same check as a local commit hook.
- `make lint typecheck test` runs locally what CI runs.

## Releases

One version for the whole app: backend, frontend and migrations images are always released together with the same
tag. [release-please](https://github.com/googleapis/release-please) derives the version and `CHANGELOG.md` from the
commits on `main` (`feat` bumps the minor version while we are below 1.0, `fix` the patch; `ci`, `build`, `docs`,
`refactor` and `chore` release nothing on their own).

| Step | You do | What runs | Releases? |
|---|---|---|---|
| 1 | Merge a PR | CI on `main`. If every check passes, release-please opens or updates the Release PR (`chore(main): release X.Y.Z`) | No |
| 2 | Nothing | The Release PR update starts CI on its branch. GitHub holds it for approval (bot-created PR); approving only runs the checks | No |
| 3 | Merge the Release PR | CI on `main`. If every check passes: tag `vX.Y.Z`, GitHub Release, publish the three images to GHCR, smoke test | **Yes** |

- There is no need to approve CI on Release PRs: step 3 runs every check on `main` before anything is released. A
  failed check on `main` means no release.
- Every merge refreshes the Release PR, which always contains everything merged so far. Merge it when you want to
  ship. A superseded, never-approved run on its branch shows as failed with no jobs; that is harmless.
- Images: `ghcr.io/fjcloudaiconsulting/ziftbook/{backend,frontend,migrations}`, tagged `vX.Y.Z`, `X.Y` and
  `sha-<short>` (private packages).
- If publishing fails after the tag exists, rerun the failed jobs, or run the Release workflow manually with that
  version.

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

## Tenant-owned tables

Tenant isolation is enforced by Postgres row-level security, not by remembering a `WHERE tenant_id = ...`. For
every table that holds a tenant's data:

- Columns `id uuid PRIMARY KEY DEFAULT uuidv7()` and `tenant_id uuid NOT NULL REFERENCES tenants (id)`.
- Call `enable_tenant_isolation("table")` (from `app.db`) in the migration, right after creating the table.
- A reference to another tenant-owned table is a composite foreign key, `(tenant_id, x_id) REFERENCES
  parent (tenant_id, id)`, never a plain one: foreign key checks bypass row-level security. For an optional
  reference use `ON DELETE SET NULL (x_id)`, since plain `SET NULL` would also null `tenant_id`.
- Every `UNIQUE` or `EXCLUDE` constraint includes `tenant_id`; otherwise a violation reveals another tenant's rows.
- Read and write inside `tenant_context(tenant_id)` (jobs, webhooks). A query without a tenant raises instead of
  returning nothing.
- Data migrations: row-level security binds `ziftbook_migrate` too. Loop over `SELECT id FROM tenants` and run
  `set_config('app.tenant_id', :id, true)` before each tenant's statements. Never grant `BYPASSRLS`.

`tests/test_tenant_schema.py` fails for a table with `tenant_id` that is not isolated, and for a foreign key
between tenant-owned tables that does not pair `tenant_id`. The few global tables that carry an optional
`tenant_id` on purpose (`jobs`) are listed in its `GLOBAL_TABLES`.

## API contract

`backend/openapi.json` is committed and the web client is generated from it. After changing an API route or
schema, run `make openapi` and commit the result; a test fails if it is stale. Never hand-write API types in the
web app.

## Translations

English (`en`) is the source for every catalog (`frontend/messages`, `landing/strings`). Add a key to English
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
