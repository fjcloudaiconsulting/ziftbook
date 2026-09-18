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
- Migrations form one chain, and their numbers are identifiers, not an order. Before merging a PR that adds a
  migration, rebase it onto `main` and set its `down_revision` to the current head; `alembic heads` must print one line.
  If both are already merged, add a merge revision (`down_revision = ("a", "b")`, empty upgrade and downgrade)
  rather than editing a merged migration.
- `CREATE INDEX CONCURRENTLY` cannot run inside a transaction; put it in its own migration using Alembic's
  `autocommit_block()`.
- Migrations run as `ziftbook_migrate`, never on app startup. The app connects as `ziftbook_app`, which owns
  nothing: never grant it ownership, `BYPASSRLS` or DDL rights.

## Tenant-owned tables

Tenant isolation is enforced by Postgres row-level security, not by remembering a `WHERE tenant_id = ...`. For
every table that holds a tenant's data:

- Columns `id uuid PRIMARY KEY DEFAULT uuidv7()` and `tenant_id uuid NOT NULL REFERENCES tenants (id)`.
- A pure link table (`service_workers`) has no `id`: its primary key is `(tenant_id, a_id, b_id)`, and it calls
  `enable_tenant_isolation(table, referenced=False)`. Nothing may reference it.
- Call `enable_tenant_isolation("table")` (from `app.db`) in the migration, right after creating the table.
- A reference to another tenant-owned table is a composite foreign key, `(tenant_id, x_id) REFERENCES
  parent (tenant_id, id)`, never a plain one: foreign key checks bypass row-level security. For an optional
  reference use `ON DELETE SET NULL (x_id)`, since plain `SET NULL` would also null `tenant_id`.
- Every `UNIQUE` or `EXCLUDE` constraint includes `tenant_id`; otherwise a violation reveals another tenant's rows.
- Read and write inside `tenant_context(tenant_id)` (jobs, webhooks). A query without a tenant raises instead of
  returning nothing.
- Data migrations: row-level security binds `ziftbook_migrate` too. Loop over `SELECT id FROM tenants` and run
  `set_config('app.tenant_id', :id, true)` before each tenant's statements. Never grant `BYPASSRLS`.
- Money is `<x>_amount_minor integer` and `<x>_currency text`, with `FOREIGN KEY (tenant_id, <x>_currency)
  REFERENCES tenants (id, currency)`: a business's currency, never the client's. The client sends only the amount.
- A row a future feature will reference (a booking, say) is archived (`archived_at timestamptz`), never deleted:
  revoke `DELETE` on the table from the app role in the migration that creates it.
- A test fixture that deletes businesses must delete that tenant's other tenant-owned rows first, as the migrate
  role (the app role may lack `DELETE`); see `delete_services` in `tests/conftest.py`.
- A business with members always keeps an owner (the `keep_an_owner` trigger on `memberships`). Endpoints never
  lock `tenants`: the trigger locks it, so an endpoint that locked it first would take the locks in the opposite
  order and deadlock with another request. A writer that removes or demotes owners first locks the caller's and
  the target's memberships in one `ORDER BY id ... FOR UPDATE OF m` statement, as `app.members.target` does, so
  two owners acting on each other serialize instead of both passing. Changes to memberships run in READ
  COMMITTED: the trigger's re-check after its lock wait needs a fresh snapshot.
- An endpoint that writes a member's data first calls `members.member_user(current, id, lock=True)` (`NO KEY
  UPDATE` on the membership), and never locks `tenants` beyond the `KEY SHARE` its foreign keys take: `keep_an_owner`
  locks memberships, then tenants.
- A column private to one member, such as `time_off.reason`, is redacted in SQL for every other caller; public or
  customer-facing endpoints never select it.

### Client records

A business's clients (`clients`) are the business's own copy of a person's name and contact
details, not a view of that person's platform account (ZIF-99).

- The copy is refreshed on each booking and never read live from `users`. No table owned by a
  business may join to `users`, and `clients.user_id` carries no foreign key and no index on
  purpose: a foreign key check bypasses row-level security, so the constraint alone would tell a
  business whether an account id exists. A pointer left dangling by a platform erasure is
  correct.
- Name and contact only: never a postal address.
- Marketing consent (`consents`) is per business. Nothing writes a platform-level consent, and one
  business's consent never grants another anything.
- `consents` is append-only: one row per grant or withdrawal per purpose, holding the exact text
  shown and its policy version. The app role has `SELECT` and an `INSERT` limited to a column
  list, so it can neither edit a row nor backdate one. As with `audit_events`, a column added
  later needs its own `GRANT INSERT (column) ON consents TO ziftbook_app`, or every consent write
  fails, not only writes of the new column.
- The wording and the version stored with a consent come from the server
  (`app.clients.CONSENT_TEXTS`); an unknown version is a 422. A caller never supplies the text it claims
  to have shown.
- The refresh has two consequences on the public booking page, both deliberate. The name on the booking
  overwrites the business's record of that person's name, so anyone who guesses a client's address can
  change it — the booking POST is rate limited per IP for that reason. And `find_or_create` returns the
  phone and locale as *stored*, not as sent, so a public answer must never echo them back: that would
  hand an unauthenticated stranger a third party's phone number.

`tests/test_tenant_schema.py` fails for a table with `tenant_id` that is not isolated (forced row-level security),
and for a foreign key between tenant-owned tables that does not pair `tenant_id`. `jobs` is exempt on purpose: it
is global and claimed across tenants.

`tenants` is the root table: it isn't isolated with `enable_tenant_isolation` and has no row-level security of its
own.

- The app role may only `INSERT` and `UPDATE (name)`; a migration's `REVOKE UPDATE, DELETE` must run before its
  column `GRANT`, or the revoke wipes the grant too.
- With no RLS, any `UPDATE` on `tenants` must filter `WHERE id = current_setting('app.tenant_id')::uuid` itself.
- `country` and `currency` change only through a `SECURITY DEFINER` function; a business is deleted only as
  `ziftbook_migrate`.

`users` is global, with row-level security that is enabled but not forced: the app role sees a user only through a
membership in the current tenant, may insert users, and can never change or delete one.

- Insert users with an id from `uuid.uuid7()` and no `RETURNING`: a new user isn't visible to its own policy yet.
- Never add an UPDATE or DELETE policy on `users`; changes to a person's account go through functions owned by
  `ziftbook_migrate`. `ziftbook_migrate` sees every user, including in data migrations.
- A view over `users` needs `WITH (security_invoker = true)`, or it shows every user.
- Create memberships only in trusted flows (sign-up, invites), never from a `user_id` the client sends: the foreign
  key to `users` is checked with row-level security bypassed.

Passwords and sign-in tokens live in tables the app role can't read or write (`password_credentials`,
`email_tokens`). The app reaches them only through `SECURITY DEFINER` functions owned by `ziftbook_migrate`:

- Every such function declares `SET search_path = pg_catalog, public, pg_temp` (pg_temp last, or a caller's temporary
  table can stand in for a real one) and is revoked from PUBLIC. `tests/test_password_auth_db.py` checks both.
- None changes a password without a token.
- The `sign_in` policy lets `ziftbook_migrate` read memberships across tenants only while `app.sign_in` is `on`. A
  function that relies on it sets and restores `app.sign_in` itself; any other definer function that reads memberships
  must clear it, because a caller can set it.

## Audit log

`audit_events` records who signed in, out, or reset a password, and when a business was created, so after an incident
we can show which businesses were not affected.

- The app role can add events and read its own business's. It can never change or delete one, or set `id` or
  `created_at`.
- An event belongs to the business of the transaction it is written in. The database refuses any other `tenant_id`.
  Events written outside a business (failed sign-ins, password resets) belong to none, and the app can't read them.
- Add an event with `auth.record(session, request, action, actor_user_id=..., target=..., details=...)` in the
  transaction of what it records, after adding the action to `auth.Action`. `details` holds what changed. When the business is only found inside that
  transaction (a session's, a new one), call `join_tenant(session, tenant_id)` first.
- Never write passwords, tokens, token hashes, cookie values or emails. `details` holds what an event changed
  (a setting's old and new value), which the business's owner reads, so never personal data either. `target` names what the event is about
  (`user:<id>`).
- It is the one table with a `tenant_id` that is neither `NOT NULL REFERENCES tenants` nor set up with
  `enable_tenant_isolation`: events outlive removed businesses and erased users, and some belong to no business.
- Operators read every event as `ziftbook_migrate`, in a transaction:
  `BEGIN; SET LOCAL app.audit_review = 'on'; SELECT DISTINCT tenant_id FROM audit_events WHERE created_at > ...;`
- The app role can set `app.audit_review` too. A definer function owned by `ziftbook_migrate` that reads
  `audit_events` must clear it first, or it shows the caller every business's events.
- A new column on `audit_events` needs its own `GRANT INSERT (column) ON audit_events TO ziftbook_app`, because the
  table's INSERT grant lists columns. Its migration must be applied **before** the app that writes it: `record`
  names every column, so a new app against the old schema fails on every event, not only the new one.
- Nothing purges events yet. The ZIF-5 sweeper will remove failed sign-ins after 30 days and the rest after a year.

## Client IP

Rate limits and audit events key on the visitor's address. Each deployment has exactly one trusted source for it:

- The web app (`frontend/proxy.ts`) drops every forwarding header the browser sent. If `ZIF_CLIENT_IP_HEADER`
  is set (staging: `cf-connecting-ip`), it forwards that header's value, when it is a valid IP address, as
  `X-Forwarded-For`. Set it only where every request reaches the web app through a proxy that overwrites that
  header; otherwise anyone can pick their address.
- The API believes `X-Forwarded-For` only from the addresses in `ZIF_TRUSTED_PROXIES` (compose: the frontend's
  fixed `172.28.0.10`; staging: the pod network, with NetworkPolicies that only let the web app reach the API).
  Empty trusts nobody. The images run uvicorn with `--no-proxy-headers`, and so should you when running it
  outside Docker.
- In app code, read `request.client.host`. Never read `X-Forwarded-For`, `X-Real-IP`, `Forwarded` or
  `CF-Connecting-IP` yourself.
- **Staging risk:** the trusted CIDR is the whole pod network (`10.42.0.0/16`), so anything that reaches the
  backend from inside it is trusted, including traffic SNATed to a node's cluster-internal address
  (`10.42.x.1`): a NodePort or LoadBalancer Service, a hostNetwork pod, or any other node-local caller, and
  any of those could set its own `X-Forwarded-For`. k3s/flannel doesn't SNAT pod-to-ClusterIP traffic, but
  that is not the safeguard: the backend Service must never be exposed via NodePort, LoadBalancer or
  Ingress, and NetworkPolicies must admit only the web app's pods.

## Business settings

A business's settings are typed and defaulted in one model, `BusinessSettings` in `app/business_settings.py`; the
`settings` table keeps only the values an owner saved. Read them with `business_settings.read(session)`. If changing a
value needs a redeploy it is an environment variable; otherwise it is a setting.

- **Add a key:** a field with a default and a JSON-native type (str, bool, int, `Literal`). The model is strict, so a
  `time`, `UUID` or `Enum` field needs a per-field lax override. No migration.
- **Remove a key:** first the web app stops sending it; in a later release the field goes (`read` ignores saved keys
  it doesn't know); then a data migration deletes its rows, looping over tenants. Removing the field in the same
  release makes every save from an old tab a 422.
- **Rename a key:** add the new key with a migration that copies the rows, then remove the old key as above. A save
  from an instance still on the old key during the rollout is lost.
- **Retype a key** (say bool to int) only with a data migration that rewrites saved rows: `1` and `True` compare
  equal in Python, so a saved `True` changed to `1` would otherwise save with no audit event.
- **Tighten a type or range:** ship a data migration that fixes saved rows. A saved value that no longer validates
  makes reads fail (500) on purpose, rather than quietly using the default.
- A saved value equal to the default stays saved: changing a default later only reaches businesses that never saved
  that key.
- Every member can read settings, so never make a secret or personal data a setting.
- Convert a business's local times with `zoneinfo`, never with SQL `AT TIME ZONE`: Postgres doesn't know every name
  the `tzdata` package accepts (`US/Pacific`, `Asia/Calcutta`).

## Background jobs

Deferred and scheduled work goes through the `jobs` table (`app/jobs.py`); there is no broker or scheduler.

- Enqueue with `enqueue(session, kind, dedupe_key, payload)` in the same transaction as the change that needs it.
  The key runs once, ever: build it as `kind:tenant_id:natural id`, adding the due time when the same thing can be
  rescheduled (`booking.reminder:<tenant>:<booking>:<starts_at>`).
- Payloads hold ids only; the handler loads what it needs inside `tenant_context(job.tenant_id)`. A job with no tenant
  (such as `email.token`, for sign-up and reset links) touches only global tables, through `SessionLocal.begin()`.
- Handlers may run more than once for a job (a crash after the work but before it is recorded, or a timeout that
  finishes late): make them safe to repeat. Every network or database call in a handler has its own timeout.
- Register a kind as `JobKind(handler, timeout, grace)`: `timeout` in seconds under 60 (the claim lease), `grace`
  longer than the retry backoff (about 15 minutes, five attempts), after which an overdue job is skipped.
- A new kind ships in one release and is enqueued from the next, so workers that don't know it yet never see it.

## Logging

Every process (API, worker, migrations) calls `app.logs.configure()` once and logs to stdout, one JSON
object per line when deployed (`ZIF_LOG_FORMAT=json`, the default) and readable text in development.
`ZIF_LOG_LEVEL` is `INFO` by default and `DEBUG` in `docker-compose.yaml`; an invalid value stops the
process at startup.

- Levels: `DEBUG` diagnostic detail; `INFO` lifecycle and business events (one access line per request);
  `WARNING` recovered problems (a job that will be retried); `ERROR` needs a human; `CRITICAL` an uncaught
  error, nothing catches it (a thread's is fatal to that thread alone; the process itself may still
  survive, but the error needs a human just as much).
- Use `logger = logging.getLogger(__name__)`; never `print` (stdout of `python -m app.main` is the
  OpenAPI document) and never configure logging anywhere else.
- Standard fields come for free: `ts`, `level`, `logger`, `msg`, `request_id`, `tenant_id`, `job_id`,
  `job_kind`, `exc`. Add others with `extra={...}`; keep `msg` a constant ("access", "job failed") and put
  values in fields.
- Events: `job claimed` (DEBUG), `job done` and `job skipped` (INFO, past its grace), `job failed`
  (WARNING, retried) and `job gave up` (ERROR, last attempt), with `attempts`; `email sent` (INFO) and
  `email failed` (WARNING) with `template` (and `error` on failure) plus the job context; never the
  address, subject or body. Each process logs one startup line (`api started`, `worker started`, `migrations started`) with its
  non-secret settings.
- `ZIF_LOG_SQL=true` logs SQL statements, never their values, when `ZIF_LOG_LEVEL` is `DEBUG`; off by
  default.
- Ids only: never a password, token or its hash, cookie, email address, name, business or service name,
  IP address, user agent, request body, query string or header.
- Errors: log the class (`type(error).__name__`) or pass `exc_info`, never `%r`/`str(error)` and never
  `logger.warning(error)`: an error's text can quote an address or a row. `exc` holds the chain's class
  names, frames (with their source line), and for a database error, its SQLSTATE and table/constraint
  names, never the message.
- `tests/test_logs_private.py` runs the real flows at DEBUG and fails if personal data reaches a log; add
  new flows to it. Build test secrets at runtime, never as literals on the line that raises: `exc` shows
  that line.

## API contract

`backend/openapi.json` is committed and the web client is generated from it. After changing an API route or
schema, run `make openapi` and commit the result; a test fails if it is stale. Never hand-write API types in the
web app.

- Routes a client uses without signing in, for a business named in the path, live under `/api/public` (the
  availability route is the first). They open `tenant_context` from the path's tenant id, are rate limited per IP,
  answer 404 for anything the business doesn't own, and return no personal data beyond what a client must see.

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
bakes into the build. Configuration that code already covers stays in code (`landing/wrangler.jsonc`, the compose
files); managing hosting infrastructure with Terraform is deferred until the platform is chosen (ZIF-20).
