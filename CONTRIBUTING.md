# Contributing

## Commits and pull requests

- Keep pull requests small: one change a reviewer can follow in one sitting.
- PR titles follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat(api): add bookings endpoint`).
  PRs are squash-merged, so the title becomes the commit on `main` and the changelog entry. CI checks it, and
  `make setup` copies the same check into your `.git/hooks` as a local commit hook (re-run it after the hook changes).
  If you set up before ZB-07, re-run `make setup` once after pulling it.
- `make lint typecheck test` runs locally what CI runs.
- Landing: run `npm --prefix landing ci` once before `npm --prefix landing run dev`; wrangler comes from the lockfile.
- CI on a pull request runs only the jobs its changed paths can affect (`scripts/ci-changes.mjs`); a docs-only PR runs
  just the title check and the two aggregate checks, `Backend Checks` and `Frontend Checks`, which pass when every job
  they gate passed or was skipped. A push to `main` runs every job.

## Releases

One version for the whole app: backend, frontend and migrations images are always released together with the same
tag. [release-please](https://github.com/googleapis/release-please) derives the version and `CHANGELOG.md` from the
commits on `main` (`feat` bumps the minor version while we are below 1.0, `fix` the patch; `ci`, `build`, `docs`,
`refactor` and `chore` release nothing on their own).

| Step | You do | What runs | Releases? |
|---|---|---|---|
| 1 | Merge a PR | CI on `main` builds the three images once as `sha-<short>`. If every check passes, release-please opens or updates the Release PR (`chore(main): release X.Y.Z`) | No |
| 2 | Nothing | The Release PR update starts CI on its branch (it is created with the release App's token, so no approval is needed to run) | No |
| 3 | Merge the Release PR | CI on `main`. If every check passes: tag `vX.Y.Z`, GitHub Release, retag that commit's `sha-<short>` images as `vX.Y.Z` (no rebuild), smoke test the published images | **Yes** |

- Step 3 runs every check on `main` before anything is released. A failed check on `main` means no release.
- Every merge refreshes the Release PR, which always contains everything merged so far. Merge it when you want to
  ship.
- Images: `ghcr.io/fjcloudaiconsulting/ziftbook/{backend,frontend,migrations}`, tagged `sha-<short>` on every `main`
  commit and `vX.Y.Z` on releases (the same digest; private packages). No `latest` or `X.Y` tags.
- Promote and smoke run only when release-please created a release in that run. If `main` moved on before the release job ran,
  the next `main` run creates the release (rerun promote if it raced the image builds).
- If promoting or the smoke test fails after the tag exists, rerun the failed jobs of that run (promotion is idempotent).
- The pipeline is the shared workflows in `fjcloudaiconsulting/.github` (`@v1`); the contract is in that repo's
  `RELEASE_CONTRACT.md`. release-please runs as a GitHub App (secrets `RELEASE_APP_ID`, `RELEASE_APP_PRIVATE_KEY`).

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
- A migration statement waits at most 5 seconds for a table or row lock (`lock_timeout`, set in `migrations/env.py`),
  so a migration stuck behind a long app transaction fails with `LockNotAvailable` instead of making every query on
  that table queue behind it. The migrations run exits non-zero and the app does not start on the new version; run
  it again once the lock is free. If it keeps failing, find the transaction holding the lock (`pg_locks` joined to
  `pg_stat_activity`), or split the migration so each step locks less (one table per migration; add a constraint
  `NOT VALID` and validate it in a later one). Waiting for another migration run to finish is not bounded:
  concurrent runs take turns.
- `CREATE INDEX CONCURRENTLY` cannot run inside a transaction; put it in its own migration using Alembic's
  `autocommit_block()`. It also waits for every older transaction in the database, and on the 5-second timeout it
  leaves an invalid index behind. Inside the block, run `DROP INDEX CONCURRENTLY IF EXISTS` first, then
  `SET lock_timeout = 0` before it and `SET lock_timeout = '5s'` after: its wait blocks no app query.
- Migrations run as `ziftbook_migrate`, never on app startup. The API connects as `ziftbook_app`, which owns
  nothing: never grant it ownership, `BYPASSRLS` or DDL rights.
- The worker connects as `ziftbook_worker` (ZB-08): a member of `ziftbook_app`, so it has every privilege the app
  role has, plus what only background jobs may do: `mint_email_token`, which the app role does not have (0051), and
  setting an invite's `token_hash` (0053). A worker still on `ziftbook_app` fails every sign-up, reset and invite
  email. The same never-list applies to it.
- Neither runtime role can create temporary tables, and both search `public` before `pg_temp` (ZB-31;
  `bootstrap.sql`, plus 0050 where `ziftbook_migrate` owns the database; elsewhere 0050 changes nothing and logs
  `temporary tables left to bootstrap.sql` at WARNING). Where the admin role does not own the database either
  (some hosted providers), the revoke in `bootstrap.sql` only warns, so `bootstrap.sql` then stops with an error if
  `has_database_privilege('ziftbook_app', current_database(), 'TEMPORARY')` or the same for `ziftbook_worker` is
  true. When it stops, run the revoke as the database owner, or run `bootstrap.sql` as the owner or a superuser. A pooled engine resets each connection
  on check-in with `app.db.reset_session` (`RESET ALL; DISCARD TEMP`, then open cursors, `LISTEN`s, session-level
  advisory locks and `SET ROLE`), so no session state reaches the next request; a new pooled engine adds the same
  listener. A setting meant for every connection goes in the engine's `connect_args` `options` (`-c lock_timeout=5s`),
  which the reset keeps, never in a `SET` after connecting.
- A function `ziftbook_migrate` creates is executable by no other role (0048's default privileges): the migration
  that creates one the app calls also runs `GRANT EXECUTE ON FUNCTION ... TO ziftbook_app`, or `TO ziftbook_worker`
  when only a job calls it. Calls the app makes indirectly count too: a function in a `CHECK` constraint, a column
  default, a row-level security policy, or the body of a `SECURITY INVOKER` trigger function runs as the role whose
  statement fired it (`slug_ok` in `ck_tenants_slug`, say). Trigger functions themselves need none.
  Nor do functions a `CREATE EXTENSION` adds: `ziftbook_migrate` can create only trusted extensions, whose objects
  belong to the bootstrap superuser, so they keep `PUBLIC`'s `EXECUTE`.

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
- A membership is never deleted by the app. Removing someone sets `role = 'removed'` and `removed_at`
  (ZIF-112); the row stays because bookings and their history reference it. The app role has no `DELETE` on
  `memberships` (0052, ZB-10), no `INSERT` (sign-up and invites create memberships in their definer functions), and
  `UPDATE` of only `role`, `removed_at` and `display_name` (0054): a new column the app sets needs its own column
  `GRANT`. Only `ziftbook_migrate` changes a removed membership's `role` or `removed_at` (a trigger, 0056): bringing
  one back is `accept_invite`'s. A new read of `memberships` decides: active (filter `role <> 'removed'`, inside the
  locking statement when it locks) or history (no filter). History names a member by `memberships.display_name`
  (or a booking's `worker_display_name`); the account's name is only a fallback for a live member, because a
  business reads a person's account row only while they are a live member (the `users` policy, 0055, ZB-32). A
  read reached through a session needs none:
  `ck_sessions_role` and the sessions foreign key keep a session's membership live. Never infer owner from
  `role <> 'worker'`; compare to `'owner'`. A new table referencing memberships with `ON DELETE CASCADE` also needs
  a line in `members.remove`, which deletes what the cascades used to.
- The business's responsible person is derived, not stored: its oldest live owner (`ORDER BY memberships.id`).
  Nothing reads it yet. It is the creator only while they stay an owner: 0045's revive keeps a removed member's old
  `memberships.id`, so a returning creator who is made owner again becomes the oldest owner again.
- A column private to one member, such as `time_off.reason`, is redacted in SQL for every other caller; public or
  customer-facing endpoints never select it.

`tests/test_tenant_schema.py` fails for a table with `tenant_id` that is not isolated (forced row-level security),
and for a foreign key between tenant-owned tables that does not pair `tenant_id`. Exempt on purpose: `jobs` (global,
claimed across tenants), `sessions` (how the tenant is found) and `platform_audit_events` (global; its `tenant_id`
names the business a platform admin acted on).

`tenants` is the root table: it isn't isolated with `enable_tenant_isolation` and has no row-level security of its
own.

- The app role may only `UPDATE (name)`: a business is created by `complete_sign_up`. A migration's `REVOKE` must
  run before its column `GRANT`, or the revoke wipes the grant too.
- With no RLS, any `UPDATE` on `tenants` must filter `WHERE id = current_setting('app.tenant_id')::uuid` itself.
- `country` and `currency` change only through a `SECURITY DEFINER` function; a business is deleted only as
  `ziftbook_migrate`.

`users` is global, with row-level security that is enabled but not forced: the app role sees a user only through a
membership in the current tenant, and only the columns `id`, `email`, `locale` and `name`; it can never insert,
change or delete one (no `INSERT`, `UPDATE` or `DELETE`).

- The app role's `SELECT` on `users` is a column list (0057, ZIF-146): a new column the app reads needs its own
  `GRANT SELECT (column)`. Never grant table-level `SELECT` again, and never grant `phone`: a person's phone reaches
  a business only through their own booking. `phone` is never unique (a duplicate error would tell who holds a
  number). `tests/test_migrations.py` pins the grant.

- Accounts are created only by `complete_sign_up`, `complete_setup`, `accept_invite` and `complete_account_sign_up`
  (ZIF-146: a person's own account, with no business and no membership).
- Never add an INSERT, UPDATE or DELETE policy on `users`; changes to a person's account go through functions owned by
  `ziftbook_migrate`. `ziftbook_migrate` sees every user, including in data migrations.
- A view over `users` needs `WITH (security_invoker = true)`, or it shows every user.
- Create memberships only in trusted flows (sign-up, invites), never from a `user_id` the client sends: the foreign
  key to `users` is checked with row-level security bypassed.

Passwords, sign-in tokens and account sessions live in tables the app role can't read or write
(`password_credentials`, `email_tokens`, `account_sessions`). The app reaches them only through `SECURITY DEFINER`
functions owned by `ziftbook_migrate`:

- Every such function declares `SET search_path = pg_catalog, public, pg_temp` (pg_temp last, or a caller's temporary
  table can stand in for a real one) and is revoked from PUBLIC. `tests/test_password_auth_db.py` checks both.
- None changes a password without a token.
- Minting a token is the worker's alone: `mint_email_token` (the hash an emailed link carries) is granted to
  `ziftbook_worker` (0049) and not to `ziftbook_app` (0051), so a worker that connects as `ziftbook_app` sends no
  sign-up or reset link (ZB-08). Likewise only `ziftbook_worker` may set an invite's `token_hash`; the API may only
  clear it, on a resend (the `invites_token_hash_by_worker` trigger, 0053).
- The `sign_in` policy lets `ziftbook_migrate` read memberships across tenants only while `app.sign_in` is `on`. A
  function that relies on it sets and restores `app.sign_in` itself; any other definer function that reads memberships
  must clear it, because a caller can set it. The same holds for every flag a policy opens to `ziftbook_migrate`
  (`app.audit_review` on `audit_events`, `app.stripe_webhook` on `payment_accounts`): `tests/test_password_auth_db.py`
  lists every definer that reads such a table without its flag, and pins the flags themselves.
- `complete_sign_up`, `accept_invite`, `complete_account_sign_up`, `set_own_name` and `set_own_profile` are the only
  writers of `users.name` (the app role has no UPDATE policy on `users`). `set_own_name` names a person who has none
  (legacy accounts only) and also fills the `display_name` of every business of theirs where it is unset, including
  one an owner had cleared. `set_own_profile` (ZIF-146) changes the session holder's own name and phone and never a
  `display_name`: a business sees a changed account name only where it has no display name of its own.
- An account session (`account_sessions`, 0057, ZIF-146) is a person's, not a membership's: it has no tenant, and no
  console route ever authenticates with it (`auth.resolve` reads only `sessions` and the `__Host-session` cookie). It
  is reached only by hash (`resolve_account_session`, `end_account_session`, `set_own_profile`), never by user id.
  Every sign-in that starts a business session also starts an account session (`auth.start`), except for a platform
  admin, who gets none (granting admin ends the person's account sessions, and no account session answers for an
  admin); every sign-out ends both kinds on that browser; signing out everywhere, a password reset and
  erasure end both kinds of that person. The account session a console sign-in starts is bound to that business
  session (`account_sessions.started_by`): it ends with it, by its sign-out, expiry, the member's removal or a
  role change, so a shop's shared device keeps no member's personal session past their console session. One the
  person starts on the booking page is not bound.
- `start_account_session` trusts the API's password check, as `start_admin_session` does: the database cannot check
  a password. An app role running SQL of its own could therefore:
  - read a hash with `account_by_email`, start an account session for anyone but an admin, and through it read and
    change that person's name and phone;
  - end anyone's account sessions, by passing `end_account_sessions` a business session's hash, which it can read
    from `sessions` (a denial of service, no data).
  - put a booking into anyone's cross-business history, by inserting it with `booked_as` set to them (it could
    already insert bookings in any business; showing one in that person's own history is new).
- Lock order for every writer that touches more than one of these: `password_credentials`, then `sessions`, then
  `account_sessions`, then `clients`. `reset_password` follows it; signing out everywhere locks the person's business
  sessions before it ends their account sessions (`auth.EVERY_SESSION`); a sign-in locks the stored hash
  (`password_unchanged`), then ends this browser's old business session, then writes account sessions; a booking locks its proving session before `find_or_create`. Two
  writers taking two of these in opposite orders deadlock, and the loser answers 500.

## Client records

A business's clients (`clients`) are the business's own copy of a person's name and contact details, not a view of
that person's platform account (ZIF-99).

- The copy is never read live from `users`. No table owned by a business may join to `users`, and `clients.user_id`
  carries no foreign key and no index on purpose: a foreign key check bypasses row-level security, so the constraint
  alone would tell a business whether an account id exists. A pointer left dangling by a platform erasure is correct.
- Name and contact only: never a postal address.
- A client row links to an account (`clients.user_id`) only through that person's own proven booking at that
  business (ZIF-146): `find_or_create`'s REFRESH fills a NULL `user_id`, never relinks, and nothing links by email.
- Marketing consent (`consents`) is per business. Nothing writes a platform-level consent, and one business's consent
  never grants another anything.
- `consents` is append-only: one row per grant or withdrawal per purpose, holding the exact text shown and its policy
  version. The app role has `SELECT` and an `INSERT` limited to a column list, so it can neither edit a row nor
  backdate one. As with `audit_events`, a column added later needs its own `GRANT INSERT (column) ON consents TO
  ziftbook_app`, or every consent write fails, not only writes of the new column.
- The wording and the version stored with a consent come from the server (`app.clients.CONSENT_TEXTS`). An unknown
  version is a 422, and so is a known version that publishes no wording for a purpose the caller named. A caller
  never supplies the text it claims to have shown.
- `app.clients.find_or_create` refreshes the copy from what a booker typed **only when the booker is authenticated as
  that address's own account (`refresh=True`); every other caller fills blanks only**, and its docstring is the one
  home for what that costs and for the duties it puts on the route that calls it. Read it before writing that route.
- A person's history across businesses (`own_bookings`, ZIF-146) lists only bookings their own account placed
  (`bookings.booked_as`), keyed by their account session hash and never reachable with a business session. It walks
  the businesses and reads each under its own tenant isolation: no policy widens what any role can read.
- A business never finds a platform user it has no relationship with: nothing a business's session can reach
  answers by email or name across tenants (`account_by_email` does, but only for sign-in (business, account and
  admin) and invite lookup and acceptance, which need a password or an invite token). `tests/test_directory_ban.py` pins the `users` policies, every SECURITY
  DEFINER function the app role can execute, and the views over `users`, so widening any of them is a deliberate
  test change.

## Bookings

A booking holds one worker for one interval, and **the database is what makes that exclusive** (`ex_bookings_worker_overlap`,
migration 0026), not any code path. A race in a route somebody forgets to guard cannot double-book.

- **Every booking writer takes `SELECT pg_advisory_xact_lock(51, hashtext(current_setting('app.tenant_id')))` as the
  first statement of its transaction, before anything else.** An `EXCLUDE` constraint has no speculative-insertion
  path, so two concurrent inserters of the same interval can each wait on the other: measured at about 9% of losers
  deadlocking rather than conflicting, and a deadlock arrives as `OperationalError`, aborts the whole transaction, and
  cannot be recovered by `ROLLBACK TO SAVEPOINT`. Keyed on the tenant alone — a late-evening booking ends on the next
  local day, and an "anyone" booking has no worker yet. The key space is the ticket number, as `app/schedule.py:229`
  already does with `105`.
- **The API waits at most 5 seconds for any lock** (`lock_timeout` on its engine, `app/main.py`, ZIF-114), then the
  statement fails as `OperationalError` (55P03), which every route answers as `503 busy`. Hold the tenant lock only
  for database work, never across an outbound call, so a queue of bookers clears well inside that. The engine's
  `connect_args` replaces any `options` in `ZIF_DATABASE_URL`. The worker has no `lock_timeout`; its
  `statement_timeout` (30s) bounds its waits.
- **The API has at most 15 database connections for its 40 request threads** (`POOL_SIZE` 5 plus `MAX_OVERFLOW` 10
  in `app/main.py`, ZB-16). A request that finds none free waits at most 5 seconds (`POOL_TIMEOUT`), then is answered
  `503 busy`. The server ends an API or worker transaction left idle for a minute
  (`idle_in_transaction_session_timeout`), so slow work runs outside a transaction: outbound calls, and a service
  photo's decode and store (`app/services.py`). Keep new slow work there too.
  Raising the pool means checking it against Postgres' `max_connections` together with the worker's 21.
- `booking_holds` (migration 0034) is unused since ZIF-158: nothing reads or writes it, and a later owner-gated
  migration drops it.
- The constraint's predicate is the four **occupying** statuses. That set lives in three places — the predicate,
  `app.availability.OCCUPYING`, and `ck_bookings_status` — and `tests/test_bookings_db.py` fences all three against
  each other. Changing it later takes `ACCESS EXCLUSIVE` and a full gist rebuild: there is no
  `ADD CONSTRAINT ... USING INDEX` for an exclusion constraint.
- The predicate cannot mention `now()` (an index predicate must be `IMMUTABLE`). The expiry carve-out therefore lives
  in `app.availability.booked()` alone, and a booking transaction expires stale pendings in one statement before
  inserting; that statement (`EXPIRE`) also writes each booking's `expired` event. The worker runs the same statement
  per tenant, under the tenant lock, every 300 seconds (`bookings.sweep`). A sweeper is housekeeping, never
  correctness.
- **The constraint is only half of it.** It catches an overlap with a live booking and nothing else: not a buffer
  tail, not opening hours, not the worker's hours, not time off, not the slot grid, not `min_notice`, not the
  horizon, not an unassigned worker, not an archived service. A write path re-derives the posted start **through
  `member_slots` itself**, never through a second validator — two implementations of "is this bookable" drift, and
  the drift is the bug.
  The one deliberate exception is the merchant's `override` on `POST /api/bookings` and `/reschedule` (ZIF-57): a
  member may book or move onto a start the grid does not offer (a walk-in, a favour after hours), so those skip
  `member_slots` by design and the exclusion constraint is the only backstop.
- Any transaction that inserts a booking takes its locks in this order: `services ... FOR SHARE` (mandatory —
  archiving takes `FOR NO KEY UPDATE` and the foreign key only `KEY SHARE`, so the key alone lets a booking land on a
  concurrently archived service), then `clients.find_or_create`, then the expiry `UPDATE`, then the savepoint and the
  insert. Postgres `now()` comes from that first row and is the whole transaction's clock.
- `find_or_create` runs **before** the savepoint, once: `ROLLBACK TO SAVEPOINT` releases the row lock that makes
  `max_pending_per_email` exact.
- The evidence snapshot (service name in every language, price, duration, the cancellation policy **text**, source,
  the auto-confirm setting, the worker's display name) is on the booking. **Client personal data is not**: it stays
  in `clients`, reached by `client_id`, so ZIF-58 keeps one tombstone. The one exception is
  `decline_message`, the merchant's free text to a client they declined: ZIF-58 must set it to NULL on erasure (the
  CHECK allows NULL on a declined row). The service's buffer is **not** snapshotted —
  a buffer is a scheduling rule, not evidence. The cancellation policy is snapshotted as text and carries **no
  version**: a version is only useful for re-reading a policy at cancellation time, which ZIF-5 rejects. The
  `policy_version` on `booking_events` is a different key — the consent wording version, into
  `app.clients.CONSENT_TEXTS` — and the two are never unified.
- `booking_events` is append-only by privilege, like `audit_events` and `consents`: a column added later needs its
  own `GRANT INSERT (column) ON booking_events TO ziftbook_app`, or **every** event write fails, not only writes of
  the new column.
- `bookings` grants `INSERT` and `UPDATE` by column (migration 0047): the app role inserts only what `place()` /
  `INSERT_BOOKING` names (the one insert, for public and merchant bookings alike) and updates only what a route
  sets, so the evidence, `id` and `created_at` are write-once. A column added later needs its own
  `GRANT INSERT (column)` if `place()` / `INSERT_BOOKING` writes it, or **every** booking insert fails, and
  `GRANT UPDATE (column)` only if a route changes it. A settled booking never becomes `pending` again, and an
  `UPDATE` never moves a booking into `awaiting_payment`, which only placement (the `INSERT`) sets (the
  `bookings_status_to_pending` trigger).
- `bookings.booked_as` (ZIF-146) is the account whose live session proved the booking, written only by `place()`
  from that session (re-confirmed `FOR KEY SHARE` inside the booking transaction), never from a body; NULL for a guest
  or a merchant. It is personal data (it maps a booking to a person): no merchant route selects it and no response
  schema carries it; only the person's own history (`own_bookings`) reads it. The app role may `INSERT` it, never
  `UPDATE` it. A proven booking whose session ended after step 0 is refused, so nothing is written under an
  identity that is gone.
- The public booking POST writes **no** `audit_events` row: the requester is the data subject, and `auth.record`
  stamps their address into a table with no foreign keys that outlives erased people.

## Audit log

`audit_events` records who signed in, out, or reset a password, when a business was created, and when a person gave
their name (`user_name_set`, never with the name in its details), so after an incident
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
- It is one of two tables with a `tenant_id` that is neither `NOT NULL REFERENCES tenants` nor set up with
  `enable_tenant_isolation` (the other is `platform_audit_events`, below): events outlive removed businesses and
  erased users, and some belong to no business.
- Operators read every event as `ziftbook_migrate`, in a transaction:
  `BEGIN; SET LOCAL app.audit_review = 'on'; SELECT DISTINCT tenant_id FROM audit_events WHERE created_at > ...;`
- The app role can set `app.audit_review` too. A definer function owned by `ziftbook_migrate` that reads
  `audit_events` must clear it first, or it shows the caller every business's events.
- A new column on `audit_events` needs its own `GRANT INSERT (column) ON audit_events TO ziftbook_app`, because the
  table's INSERT grant lists columns. Its migration must be applied **before** the app that writes it: `record`
  names every column, so a new app against the old schema fails on every event, not only the new one.
- Nothing purges events yet. The ZIF-5 sweeper will remove failed sign-ins after 30 days and the rest after a year.

### Platform admin

A platform admin runs the platform across businesses (ZIF-70). `platform_admins` holds one row per admin.

- The app role has no grant at all on `platform_admins`: it can neither read nor change who is an admin. Admins
  are granted and revoked with the CLI, which connects as `ziftbook_migrate` (`ZIF_MIGRATE_DATABASE_URL`):
  `docker compose run --rm --build migrate python -m app.admin grant <email> --reason "..."` (or `revoke`).
  `--reason` is required. Exit code 0 means done or nothing to do, 1 no account has that email (or another error,
  logged CRITICAL), 2 a usage error.
- The app role can start an admin session that has not passed the second factor yet: the database cannot check
  a password (the API verifies it), so `start_admin_session` trusts the API's check. Only the second factor (the
  next bullet) keeps the app role from admin access (ZB-08).
- Admins sign in with a second factor (TOTP, ZIF-71). An admin enrols with a one-hour ticket from
  `python -m app.admin enrol <email> --reason "..."`, which wipes their current factor and recovery codes and ends
  their admin sessions. Like `grant`, it connects as `ziftbook_migrate` (`ZIF_MIGRATE_DATABASE_URL`), so it runs
  from the migrations image, never in the API's container: the API must never hold the migrate role's URL. It
  writes the ticket to `/dev/tty`, never to stdout or a log, so it needs a terminal (`-it`): with none it issues
  nothing and exits 2; exit 1 means no admin has that email. The container log records what a container's main
  process writes to its terminal (`kubectl run -it`, `docker compose run -it`), so in a deployment start a
  short-lived pod from the migrations image with only the migrate role's URL and a command that just sleeps, run
  the CLI in it with `kubectl exec -it`, and delete the pod. For local development
  `docker compose run --rm -it migrate python -m app.admin enrol <email> --reason "..."` is fine, with that
  caveat. Run it after `grant`, and again when an admin loses their authenticator and their recovery codes.
- Never enable statement or parameter logging (`log_statement`, `log_min_duration_statement` with parameters,
  `auto_explain` with parameters) on the app database. Bind values include bearer proofs: a logged TOTP proof,
  recovery-code proof or ticket proof lets anyone who can read the log and run SQL pass an admin's second factor.
  No role setting enforces this: the app role could turn `log_parameter_max_length_on_error` back on, and a role
  setting would miss `ziftbook_migrate`, which sends the CLI's ticket proof.
- What admins do goes to `platform_audit_events`, a global table, not per business: add an event with
  `admin.record(session, request, action, actor_user_id=..., tenant_id=..., target=..., reason=...)` after adding
  the action to `admin.Action` and its kind to `admin.KIND`. The same rules as `auth.record`: never a password,
  token, cookie, email or personal data.
- The app role may only append, naming its columns: never `id` or `created_at`, never read, change or delete. A
  `write` event without a non-blank `reason` is refused by the database. Operators read it as `ziftbook_migrate`.

## Client IP

Rate limits and audit events key on the visitor's address. Each deployment has exactly one trusted source for it:

- The web app (`frontend/proxy.ts`, and the booking page's server loader) drops every forwarding header the
  browser sent. If `ZIF_CLIENT_IP_HEADER` is set, it forwards that header's value, when it is a valid IP
  address, as `X-Forwarded-For`. Set it only where every request reaches the web app through a proxy that
  overwrites that header; otherwise anyone can pick their address.
- The API believes `X-Forwarded-For` only from the addresses in `ZIF_TRUSTED_PROXIES`. Empty trusts nobody.
  The images run uvicorn with `--no-proxy-headers` and `--no-server-header` (the latter keeps the server's name
  and version out of every answer), and so should you when running it outside Docker.
- In app code, read `request.client.host`. Never read `X-Forwarded-For`, `X-Real-IP`, `Forwarded` or
  `CF-Connecting-IP` yourself.

Where each deployment gets it:

- **Production and staging** (k3s; `app.ziftbook.com` and `dev.ziftbook.com`, manifests in cloud-infra
  `clusters/hetzner/ziftbook-{prod,staging}`): Cloudflare to the Cloudflare Tunnel (`cloudflared` pod) to the
  web app to the API. Cloudflare overwrites `CF-Connecting-IP` on every request, and NetworkPolicies admit only
  the `cloudflared` pods to the web app, so no other path reaches it. The web app sets `ZIF_CLIENT_IP_HEADER=cf-connecting-ip`; the API sets
  `ZIF_TRUSTED_PROXIES=10.42.0.0/16` (the pod network), and NetworkPolicies admit only the web app's pods to
  it. Each visitor gets their own per-IP limits.
- **`docker-compose-prod.yaml`** publishes the web app's port 3000 directly (bound to 127.0.0.1, with no Turnstile
  opt-out), so it leaves `ZIF_CLIENT_IP_HEADER` unset (any client could forge the header) and the API trusts only
  the web app's fixed `172.28.0.10`. Every
  visitor then shares that one address and its per-IP limits: after ZIF-54's 10 booking-link exchanges in 15
  minutes, every further one is refused, for every client of every business. Use it to try a release, never
  for public traffic as it stands.
- **Pod-network risk (k3s):** the trusted CIDR is the whole pod network, so anything that reaches the
  backend from inside it is trusted, including traffic SNATed to a node's cluster-internal address
  (`10.42.x.1`): a NodePort or LoadBalancer Service, a hostNetwork pod, or any other node-local caller, and
  any of those could set its own `X-Forwarded-For`. k3s/flannel doesn't SNAT pod-to-ClusterIP traffic, but
  that is not the safeguard: the backend Service must never be exposed via NodePort, LoadBalancer or
  Ingress, and NetworkPolicies must admit only the web app's pods.
- **Worker residual (cloud-infra INFRA-145):** a Cloudflare Worker attached to the `ziftbook.com` zone runs
  inside it, so its code can send its own `CF-Connecting-IP` to the zone's node hostnames, production
  included: it could dodge per-IP limits, lock a chosen address out, and write forged addresses into audit
  events. Only whoever merges to this repo's `main`, or holds the landing Worker's deploy token, can change
  that code.

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
  The key runs once while its job is kept: a finished, skipped or given-up job is deleted 30 days after it finishes
  (`jobs.prune`, on the worker's sweep timer), and then the same key enqueues a new job. Never count on the key to
  stop a repeat further apart than that; gate the handler on current state instead, as booking emails do. A given-up
  job's `last_error` is readable for those 30 days; `email_outbox` keeps the record of what was sent. Build the key as
  `kind:tenant_id:natural id`, adding the due time when the same thing can be rescheduled
  (`booking.reminder:<tenant>:<booking>:<starts_at>`).
- Removing a job kind needs a migration that deletes its rows: the prune only removes finished jobs, and no worker
  finishes a kind it no longer registers.
- Payloads hold ids only; the handler loads what it needs inside `tenant_context(job.tenant_id)`. A job with no tenant
  (such as `email.token`, for sign-up and reset links) touches only global tables, through `SessionLocal.begin()`.
- Handlers may run more than once for a job (a crash after the work but before it is recorded, or a timeout that
  finishes late): make them safe to repeat. Every network or database call in a handler has its own timeout.
- Register a kind as `JobKind(handler, timeout, grace)`: `timeout` in seconds under 60 (the claim lease), `grace`
  longer than the retry backoff (about 15 minutes, five attempts), after which an overdue job is skipped.
- A new kind ships in one release and is enqueued from the next, so workers that don't know it yet never see it.
- Booking emails (`email.booking`) are gated by the booking's status at SEND time, not at enqueue time: the handler
  re-reads the booking and skips silently if its status no longer matches the template (ZIF-53).
- Enqueue a booking email in the same transaction as the status change it announces, so a rollback drops the job with
  it (ZIF-53).
- Client booking emails carrying no manage link reply to the business's `contact_email` (Reply-To); never a token
  email, and never staff, invite, sign-up or reset mail (ZIF-99).

## Logging

Every process (API, worker, migrations) calls `app.logs.configure()` once and logs to stdout, one JSON
object per line when deployed (`ZIF_LOG_FORMAT=json`, the default) and readable text in development.
`ZIF_LOG_LEVEL` is `INFO` by default and `DEBUG` in `docker-compose.yaml`; an invalid value stops the
process at startup. The web server (`frontend/lib/log.ts`) follows the same rules, with its own
loggers (`web.*`); Next's own stderr output (banners, SSR error traces) is not in this format.

- Levels: `DEBUG` diagnostic detail; `INFO` lifecycle and business events (one access line per request);
  `WARNING` recovered problems (a job that will be retried); `ERROR` needs a human; `CRITICAL` an uncaught
  error, nothing catches it (a thread's is fatal to that thread alone; the process itself may still
  survive, but the error needs a human just as much).
- Use `logger = logging.getLogger(__name__)`; never `print` (stdout of `python -m app.main` is the
  OpenAPI document) and never configure logging anywhere else.
- Standard fields come for free: `ts`, `level`, `logger`, `msg`, `request_id`, `client_request_id` (the web proxy's forwarded client id, only when valid), `tenant_id`,
  `job_id`, `job_kind`, `exc`. Add others with `extra={...}`; keep `msg` a constant ("access", "job failed") and put
  values in fields.
- Events: `job claimed` (DEBUG), `job done` and `job skipped` (INFO, past its grace), `job failed`
  (WARNING, retried) and `job gave up` (ERROR, last attempt), with `attempts`;
  `bookings expired` (INFO, `count`, `tenant_id`, from the worker's sweep); `jobs pruned` (INFO, `count`, only when
  something was deleted, from the same sweep); `email sent` (INFO) and
  `email failed` (WARNING) with `template` (and `error` on failure) plus the job context, and
  `email skipped` (INFO, `template`, `reason` `recipient_quota`, from the worker's send);
  `client mail over quota` (WARNING, `template`, `source` `merchant` or `public`, when a business's
  hourly client email quota holds an email back at enqueue); never the
  address, subject or body. Each process logs one startup line (`api started`, `worker started`, `migrations started`) with its
  non-secret settings. Among the startup lines that need acting on:
  `turnstile secret unset: bookings, sign-ups, resets and sign-in challenges refused` (ERROR, API);
  `turnstile disabled on a public url` (ERROR, API: `ZIF_TURNSTILE_DISABLED` while `ZIF_APP_URL` is
  not localhost); `rate limit key unset: rate-limit keys are not peppered` (WARNING, API and worker,
  while their `rate_limit_key` is `unset`, `weak` or `dev`); `api database role: not ziftbook_app, ...`
  (ERROR, API, `database_role`: the API's `current_user` is another role, as when the worker's
  connection string sits in the API's secret; never a refusal to start) and
  `worker database role: not ziftbook_worker; ...` (WARNING, worker, `database_role`: sign-up, reset
  and invite emails fail once the contract migration revokes token minting from `ziftbook_app`).
  Both startup lines carry `database_role` (`current_user`, never `session_user`; left out when the
  query failed).
- Abuse events, ids and reasons only: `turnstile mismatch` (WARNING, `reason` `hostname` or `action`:
  a passing token solved on another host or for another form, as when `ZIF_APP_URL` doesn't name the
  site's host); `admin sign-in failures` (WARNING, `target` `user:<id>`, null for an unknown email,
  when an email's wrong passwords on admin sign-in itself reach the admin challenge's threshold (the
  challenge also counts wrong passwords on business sign-in, so it can come first));
  `booking placement refund failed` (WARNING, with `exc`: a booking that was not placed keeps its
  address limits counted); `booking flood`
  (WARNING, `tenant_id`, at most once an hour: a business's unproven guest bookings reached their cap
  and its owners are emailed) and `booking flood alert failed` (WARNING, with `exc`).
- `ZIF_LOG_SQL=true` logs SQL statements, never their values, when `ZIF_LOG_LEVEL` is `DEBUG`; off by
  default.
- Ids only: never a password, token or its hash, cookie, email address, name, business or service name,
  IP address, user agent, request body, query string or header.
- Metric attributes follow the same rule, stricter: never a user, tenant, request, job, booking or
  session id, nor any value a client typed; only a small closed set (a job kind, an email template, an
  outcome, a route template). A new value set needs a code change, never data. Count business events
  with `db.count_after_commit`, never before the commit.
- Errors: log the class (`type(error).__name__`) or pass `exc_info`, never `%r`/`str(error)` and never
  `logger.warning(error)`: an error's text can quote an address or a row. `exc` holds the chain's class
  names, frames (with their source line), and for a database error, its SQLSTATE and table/constraint
  names, never the message.
- `tests/test_logs_private.py` runs the real flows at DEBUG and fails if personal data reaches a log; add
  new flows to it. Build test secrets at runtime, never as literals on the line that raises: `exc` shows
  that line.

## Tracing

Every process (API, worker, migrations) calls `app.tracing.configure(service)` once, right after
`logs.configure()`. Every span is hand-written (`app.tracing.span`), never from a contrib
instrumentation package: those record raw URLs, paths and SQL error messages that can quote an
email or password, and a denylist can't keep up with what a new library version starts recording.
Attributes on every span come from an explicit allowlist instead.

- The SDK's own exception recording is off on every span (`record_exception=False`,
  `set_status_on_exception=False`): on an error, the span status is
  `Status(ERROR, logs.error_summary(e))` plus `error.type`, the same rule this file's Logging
  section already follows for log lines.
- A job's trace context rides in its payload under the reserved key `traceparent` (`app/jobs.py`);
  handlers never read it. It is the job span's **parent**, not a Link: a Link would start a new
  trace.
- Only W3C tracecontext is propagated, never baggage: arbitrary client data must never land in the
  global `jobs` table.
- The web app's `lib/trace.ts` never calls `register()`: with a global tracer provider, Next's own
  built-in spans start recording raw request URLs and paths. `frontend/proxy.ts` also deletes any
  incoming `traceparent`, `tracestate` and `baggage`, so a client never chooses a trace; every web
  span is a root.
- Never inline a value into `text(f"...")`: a manual SQL span's `db.query.text` exports the
  statement text as-is, so an inlined value would export it too.
- The compose files feed the OTel SDK's own `OTEL_*` variables from `ZIF_OTEL_*` ones (owner
  ruling: an operator-facing name carries `ZIF_`). Code itself only ever reads the standard
  `OTEL_*` names; nothing in `app/` or `lib/` reads a `ZIF_OTEL_*` name directly.

## Observability

Every signal leaves the app in a vendor-neutral form: logs as JSON lines on stdout, traces and
metrics over OTLP to one endpoint. This follows the platform's telemetry standard (cloud-infra
`docs/architecture.md`, Telemetry). What sits behind that endpoint (the
Grafana LGTM stack today; Prometheus, fluentd or anything else later) is an OpenTelemetry
Collector's configuration, never app code. So no vendor agent, SDK or protocol belongs in this
repo. Every switch below is an environment variable read at process start, so any deployment
sets it the same way. With `docker-compose-prod.yaml`, set it in `.env` and run
`docker compose -f docker-compose-prod.yaml up -d`, which recreates the changed containers
(`docker compose restart` keeps the old environment and changes nothing). The dev
`docker-compose.yaml` passes only the endpoint, headers and the exporter switches through
from `.env`; it fixes the log settings at `DEBUG`/`text`, passes no `ZIF_LOG_SQL` and no sampler.

| Signal | Leaves the app as | On | Off | Tune |
|---|---|---|---|---|
| Logs | JSON lines on stdout (`ZIF_LOG_FORMAT=json`, the default; `text` in dev). The API, worker, migrations and web app never send logs over OTLP: a log collector reads their stdout (none is set up yet). | always (stdout) | stdout cannot be turned off; raise `ZIF_LOG_LEVEL` to `ERROR` for the least | `ZIF_LOG_LEVEL` (default `INFO`), `ZIF_LOG_SQL` (see Logging) |
| Traces | OTLP/HTTP to `ZIF_OTEL_EXPORTER_OTLP_ENDPOINT` | set the endpoint (plus `ZIF_OTEL_EXPORTER_OTLP_HEADERS` if the collector wants auth) | leave the endpoint unset: no spans are exported and no connection is attempted. `ZIF_OTEL_TRACES_EXPORTER=none` turns traces off with the endpoint still set. In prod you can also set `ZIF_OTEL_TRACES_SAMPLER=always_off` | prod: `ZIF_OTEL_TRACES_SAMPLER` / `_ARG` (default 10% of new traces, following the caller's decision); dev: 100% |
| Metrics | API: `http.server.*`, `db.client.connection.*`, business counters (`ziftbook.sign_up.*`, `ziftbook.invite.*`, `ziftbook.booking.created`); worker: `ziftbook.job.*`, `ziftbook.email.deliveries`, `db.client.connection.*`; web: `http.server.request.duration` for `/api` only, `ziftbook.web.request.errors` for pages; over OTLP/HTTP to `ZIF_OTEL_EXPORTER_OTLP_ENDPOINT` every 60s | set the endpoint | leave the endpoint unset: no reader, no connection. `ZIF_OTEL_METRICS_EXPORTER=none` turns metrics off with the endpoint still set | every 60s (the Python SDK's `OTEL_METRIC_EXPORT_INTERVAL`, which compose does not pass; the web app is fixed at 60s) |

- Next's own startup banner (standalone `server.js`) is plain text and ignores `ZIF_LOG_*`;
  every other web server line follows them.
- Logs go to stdout because that is what every platform collects (the Docker logging driver, a
  Kubernetes node agent, an OpenTelemetry Collector's `filelog` receiver).
- Metric attributes are an allowlist, like the API span's. `http.server.*` keeps `http.request.method`,
  `http.route` (the template, never the path), `http.response.status_code` and `error.type`; the health
  check records nothing. Our own instruments, each with a closed attribute set:

  | Name | Kind | Attributes | Process |
  |---|---|---|---|
  | `ziftbook.job.runs` | counter | `job.kind`, `job.outcome` (done, failed, gave_up, skipped) | worker |
  | `ziftbook.job.attempts` | histogram | `job.kind` (done jobs only) | worker |
  | `ziftbook.job.running` | up-down counter | `job.kind` | worker |
  | `ziftbook.job.queue.size` (observable up-down counter), `ziftbook.job.queue.oldest_age` (observable gauge, s) | | `job.kind` (0 when empty) | worker |
  | `ziftbook.email.deliveries` | counter | `email.template`, `email.outcome` (sent, failed) | worker |
  | `db.client.connection.count` | observable up-down counter | `db.client.connection.state` (used, idle), `db.client.connection.pool.name` | api, worker |
  | `db.client.connection.pending_requests` | observable up-down counter | `db.client.connection.pool.name` | api, worker |
  | `ziftbook.sign_up.completed`, `ziftbook.invite.sent`, `ziftbook.invite.accepted` | counters | none | api |
  | `ziftbook.booking.created` | counter | `booking.source` (booking_page, merchant) | api |
  | `ziftbook.web.request.errors` | counter | `http.route` (the template) | web |

  The queue series come from every worker, so aggregate them with `max`, not `sum`. `job.running`
  excludes handler threads that timed out and are still running.
- Page request rate and duration are not measured in-app: Next would have to be instrumented
  globally, which records raw URLs. Measure them at the edge.
- With a dead collector, each signal's shutdown retries for up to about 7s (a process with
  traces and metrics on can exit about 14s late).
- The sampler variables are the OpenTelemetry standard values (`always_on`, `always_off`,
  `traceidratio`, `parentbased_*`), read by the SDK itself. The dev compose passes neither.
- Service names: `ziftbook-api`, `ziftbook-worker`, `ziftbook-migrations`, `ziftbook-web`
  (`OTEL_SERVICE_NAME` overrides; `OTEL_RESOURCE_ATTRIBUTES` adds e.g. `deployment.environment=prod`).
  Neither compose file passes these two through, so with compose they mean editing the file.
- A log line's `trace_id` is there even when the trace was not sampled: in prod at 10%, most
  logged trace ids are not in the trace store.
- The environment variables table below lists each of these with its defaults.

### Locally

`make seed` fills a running `make up` with synthetic businesses, people and bookings (README, "make seed").

`make observe` is `make up` with traces and metrics exported to one Grafana LGTM stack (Loki, Tempo,
Prometheus and Grafana behind a built-in OpenTelemetry Collector) that runs outside this project
and is shared by every project on the machine. `make up` is unchanged and exports nothing.
`make observe` sets `ZIF_OTEL_EXPORTER_OTLP_ENDPOINT` to `http://host.docker.internal:4318` and
clears `ZIF_OTEL_EXPORTER_OTLP_HEADERS`, overriding a `.env` that sends to a hosted collector. It
needs Docker Desktop (macOS, Windows): on Linux `host.docker.internal` does not reach a stack
published on 127.0.0.1, so nothing arrives, and nothing says so.

If the stack isn't running yet, start it once (it comes back with Docker; data survives in the
`lgtm-data` volume):

```sh
docker run -d --name lgtm --restart unless-stopped -v lgtm-data:/data \
  -p 127.0.0.1:3300:3000 -p 127.0.0.1:4317:4317 -p 127.0.0.1:4318:4318 \
  -p 127.0.0.1:3100:3100 -p 127.0.0.1:3200:3200 -p 127.0.0.1:9090:9090 grafana/otel-lgtm:0.33.1
```

To follow a request, open Grafana at http://127.0.0.1:3300, then Explore:

1. Take the id from the response's `X-Request-ID` header and find its log line in the terminal
   (`docker compose logs backend | grep <request id>`). The API's access line carries `trace_id=`.
2. In Tempo, open that trace id: it shows the web span, the API span, its SQL spans, and any job
   and email it queued. Or search Tempo by service name (`{resource.service.name="ziftbook-api"}`).

The API's request metrics are in Prometheus as `http_server_request_duration_seconds` (label
`http_route`). No logs land in Loki: every process, the web app included, logs to the terminal
(`docker compose logs`).

## API contract

`backend/openapi.json` is committed and the web client is generated from it. After changing an API route or
schema, run `make openapi` and commit the result; a test fails if it is stale. Never hand-write API types in the
web app.

- Routes a client uses without signing in, for a business named in the path, live under `/api/public` (the
  availability route is the first). They open `tenant_context` from the path's tenant id, are rate limited per IP,
  answer 404 for anything the business doesn't own, and return no personal data beyond what a client must see.
  A route named by a slug instead (the booking page) resolves the tenant from it first, then calls `join_tenant`
  inside the same `SessionLocal.begin()`, rather than opening `tenant_context` directly.

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

### Environment variables

Local dev (`make up`) needs no `.env`: `docker-compose.yaml` sets every variable itself. `.env.example` at the repo
root is for `docker-compose-prod.yaml` only; copy it to `.env` next to that file and fill in the blanks. A row
marked "secret" is never committed and never put in a shared `.env`: in a deployment it resolves from Secrets
Manager, e.g. `{{resolve:secretsmanager:secret-id:SecretString:json-key}}` (see `TurnstileSettings`' comment and
ZIF-38). An empty variable means the default: `env_ignore_empty=True` on the base `Settings` model makes `VAR=""`
(as compose passes an unset optional variable, `${VAR:-}`) behave the same as `VAR` unset.

| Name | Read by | Default | Required in production? | Secret? | What it does |
|---|---|---|---|---|---|
| **Database** | | | | | |
| `ZIF_DATABASE_URL` | api, worker | none | yes | yes | The API's is `ziftbook_app`'s connection string; the worker's is `ziftbook_worker`'s (ZB-08). Keep the two in separate secrets: an API on the worker's URL could mint email tokens and set invite tokens itself. A worker on `ziftbook_app`'s fails every sign-up, reset and invite email, and logs a WARNING at startup. The API logs an ERROR at startup when its role is not `ziftbook_app`. |
| `ZIF_MIGRATE_DATABASE_URL` | migrations | none | yes | yes | `ziftbook_migrate`'s connection string, used to run Alembic and the admin CLI (`python -m app.admin`). Never give it to the API or the worker: `ziftbook_migrate` owns the schema, so it can change or turn off row-level security. |
| `ZIF_POSTGRES_PASSWORD` | compose | none | yes | yes | Postgres superuser password. |
| `ZIF_MIGRATE_PASSWORD` | compose (`bootstrap.sql`) | none | yes | yes | `ziftbook_migrate` role password; required by `bootstrap.sql`, which checks it before it creates or changes any role and stops non-zero when it is unset or empty (an empty one would clear the role's password). A run it stops changes nothing: set the password and run it again. |
| `ZIF_APP_PASSWORD` | compose (`bootstrap.sql`) | none | yes | yes | `ziftbook_app` role password; required by `bootstrap.sql`, which checks it before it creates or changes any role and stops non-zero when it is unset or empty (an empty one would clear the role's password). A run it stops changes nothing: set the password and run it again. |
| `ZIF_WORKER_PASSWORD` | compose (`bootstrap.sql`), tests | none (tests: `ziftbook_worker`) | yes | yes | `ziftbook_worker` role password (ZB-08); required, by `docker-compose-prod.yaml` and by `bootstrap.sql`, which checks it before it creates or changes any role and stops non-zero when it is unset or empty (an empty one would clear the role's password). A run it stops changes nothing: set the password and run it again. Every environment's database init (whatever runs `bootstrap.sql`) needs it, before the migrations of the release that adds the role. |
| **Mail** | | | | | |
| `ZIF_MAILGUN_API_KEY` | worker | `""` | needed to send mail | yes | Mailgun sending API key. Unset: the worker logs `mail: "off"` and every email job fails (and retries) until it is set. |
| `ZIF_MAILGUN_DOMAIN` | worker | `""` | needed to send mail | no | Mailgun sending domain; mail goes from `no-reply@` it. |
| `ZIF_MAILGUN_REGION` | worker | `eu` | no | no | `eu` or `us`: which Mailgun API host the account lives on. |
| `ZIF_MAILPIT_URL` | worker | none | no (development only) | no | Mailpit's base URL; with no Mailgun key, mail goes to its HTTP send API. Never SMTP. |
| `ZIF_APP_URL` | api, worker | `http://localhost:3000` | yes | no | The public web URL: where links in emails point (worker) and where Stripe sends an owner back after onboarding, and the Apple Pay domain (api). Must be the public web URL in production, on both. |
| **Photo storage** | | | | | |
| `ZIF_S3_ENDPOINT` | api, worker, frontend | `""` | for photos | no | The S3-compatible API (production and staging: the R2 account endpoint, `https://<account>.eu.r2.cloudflarestorage.com`; compose: SeaweedFS). No default in code. While any of the first six variables here is unset the API logs a WARNING at startup (`storage: "off"`) and answers 503 `photos_unavailable` to a photo; startup never fails over it. The web app allows uploads to its origin in the pages' Content-Security-Policy (ZB-12) unless `ZIF_S3_PUBLIC_ENDPOINT` is set: give it the API's value. |
| `ZIF_S3_UPLOADS_BUCKET` | api | `""` | for photos | no | Where the browser PUTs the original (presigned, 5 minutes). Nothing deletes from it: give it a 1-day lifecycle rule and a CORS rule allowing PUT with `content-type` from the web app's origin. |
| `ZIF_S3_MEDIA_BUCKET` | api, worker | `""` | for photos | no | Where the resized variants are written, served publicly from `ZIF_MEDIA_URL`. |
| `ZIF_S3_ACCESS_KEY_ID` | api, worker | `""` | for photos | no | Access key id for both buckets. |
| `ZIF_S3_SECRET_ACCESS_KEY` | api, worker | `""` | for photos | yes | Its secret. |
| `ZIF_MEDIA_URL` | api, frontend | `""` | for photos | no | Where the media bucket is served from (a custom domain origin, no trailing slash needed); photo URLs start with it. Unset: services list no photo. The web app allows images from its origin in the pages' Content-Security-Policy (ZB-12), read per request; give it the API's value. |
| `ZIF_S3_PUBLIC_ENDPOINT` | api, frontend | `""` | no (development only) | no | The host the browser reaches the store on, when `ZIF_S3_ENDPOINT` is a compose service name; presigned upload URLs use it, and the web app's Content-Security-Policy allows uploads to it. Never set in a deployment. |
| **Payments** | | | | | |
| `ZIF_STRIPE_SECRET_KEY` | api, worker | `""` | for payments | yes | Stripe secret key (`sk_...`), for the platform account that owns the Connect accounts. The worker needs only this one (events, and later releases and refunds). Unset (any of the three): payments off, booking pages book without a payment step, WARNING at startup (`payments: "off"`). |
| `ZIF_STRIPE_WEBHOOK_SECRET` | api | `""` | for payments | yes | Signing secret (`whsec_...`) of the Connect webhook endpoint `/api/stripe/webhook`. Unset (any of the three): payments off, booking pages book without a payment step, WARNING at startup (`payments: "off"`). |
| `ZIF_STRIPE_PUBLISHABLE_KEY` | api | `""` | for payments | no | Stripe publishable key (`pk_...`), public by design: the booking page needs it to render the card form. Unset (any of the three): payments off, booking pages book without a payment step, WARNING at startup (`payments: "off"`). |
| **Security** | | | | | |
| `ZIF_TURNSTILE_SECRET` | api | `""` | yes, or `ZIF_TURNSTILE_DISABLED` | yes | Cloudflare Turnstile secret, checked on public bookings, sign-up and password-reset requests, and on a sign-in, invite acceptance or admin sign-in once repeated wrong passwords ask for a challenge (429 `challenge_required`). Unset: every booking, sign-up and reset request is refused (403 `turnstile_failed`), no challenge can be passed, and the API logs an ERROR at startup, unless `ZIF_TURNSTILE_DISABLED=true`. |
| `ZIF_TURNSTILE_DISABLED` | api | `false` | no | no | `true` skips the Turnstile check when no secret is set (development, CI, tests): bookings, sign-up and reset requests need no token, and a sign-in challenge accepts any token. Ignored when a secret is set. Never on a public deployment: the API logs an ERROR at startup when `ZIF_APP_URL` is not localhost. |
| `ZIF_TURNSTILE_SITE_KEY` | frontend | none | no | no | Cloudflare Turnstile site key, rendered on the public booking page, the sign-up and forgot-password forms, and the sign-in, invite and admin sign-in forms when the API asks for a challenge. Empty counts as unset: no widget, and a challenged sign-in says to try later. Set both the site key and the secret, or neither: a site key with no secret shows a widget while the API refuses every booking, sign-up and reset; a secret with no site key never gets a token to check, so those are refused too. Never `NEXT_PUBLIC_*`: read server-side at request time (`process.env`, ZIF-56's page loader), not baked into the client bundle at build time. |
| `ZIF_SETUP_TOKEN` | api | `""` | until setup is done | yes | First-run setup token (ZIF-70): whoever sends it to `/setup` on an install with no account yet becomes the platform admin. At least 32 characters (`openssl rand -base64 32`); unset or shorter refuses setup (503 `setup_not_configured`), and the API logs an ERROR at startup while setup is still to be done. Once anyone has an account setup answers 409 whatever the token, so it can be removed afterwards. The development token from `docker-compose.yaml` counts as unset when `ZIF_APP_URL` is not localhost. |
| `ZIF_TOTP_KEY` | api | `""` | yes, once an admin exists | yes | Encrypts every platform admin's TOTP secret (ZIF-71): base64 of exactly 32 bytes (`openssl rand -base64 32`), AES-256-GCM. Unset or invalid: the API logs an ERROR at startup and admin TOTP answers 503 `second_factor_unavailable`, while recovery codes still work. Back it up with the database credentials: losing or changing it makes every stored secret unreadable, and each admin then re-enrols with `python -m app.admin enrol`. The development key from `docker-compose.yaml` counts as unset when `ZIF_APP_URL` is not localhost. |
| `ZIF_RATE_LIMIT_KEY` | api, worker | `""` | recommended | yes | Pepper for rate-limit keys (ZB-25): the email or IP in every key becomes its HMAC-SHA256 under this key, so `rate_limits` holds no addresses and nothing a guess can be checked against (the worker's per-recipient email limits too). At least 32 characters (`openssl rand -base64 32`). Unset or shorter keeps the plain form and the API and worker log a WARNING at startup. Setting or changing it changes every key, so counters start again once. The development key from `docker-compose.yaml` counts as unset when `ZIF_APP_URL` is not localhost. |
| `ZIF_TESTS_ALLOW_DB` | tests | unset | no | no | Test suite only. The suite refuses a database host other than `localhost`, `127.0.0.1`, `::1` or `postgres`, because it drops and recreates databases; set this to the exact database name to allow one remote database. Never point it at a shared or production database. |
| `ZIF_CSP_ENFORCE` | frontend | `false` | no | no | `true` enforces the pages' Content-Security-Policy (ZB-12: scripts only with the page's nonce). Anything else sends it as `Content-Security-Policy-Report-Only`: browsers report what it would block and block nothing. Read when the web server starts. Turn it on in staging first, once a booking with a card and Turnstile and an admin sign-in show no violations in the browser console, then in production. |
| `ZIF_TRUSTED_PROXIES` | api | `""` | yes | no | Addresses whose `X-Forwarded-For` the API believes (production and staging: the pod network `10.42.0.0/16`; compose: the web app's `172.28.0.10`). Empty trusts nobody. |
| `ZIF_CLIENT_IP_HEADER` | frontend | none | yes, behind a proxy | no | Header the web app trusts for the visitor's address (production and staging: `cf-connecting-ip`). Set only behind a proxy that overwrites it; unset there, every visitor shares the proxy's address and its per-IP limits. |
| **Logging** | | | | | |
| `ZIF_LOG_LEVEL` | api, worker, migrations, frontend | `INFO` | no | no | `DEBUG`/`INFO`/`WARNING`/`ERROR`. |
| `ZIF_LOG_FORMAT` | api, worker, migrations, frontend | `json` | no | no | `json` or `text`. |
| `ZIF_LOG_SQL` | api, worker, migrations | `false` | no | no | Logs SQL statements (never values) when the level is `DEBUG`. |
| **Runtime** | | | | | |
| `ZIF_APP_VERSION` | api, frontend | `dev` | no | no | Baked into the backend and frontend images at build time. |
| `ZIF_APP_REVISION` | api, frontend | `dev` | no | no | Git sha of the build, baked into the backend and frontend images. |
| `ZIF_SHOW_VERSION` | frontend | none | no | no | `true` shows the version line (`v0.27.1 · 315a9c5`) at the bottom of the console sidebar and More sheet and in the booking pages' footer. Only staging sets it. Read server-side per request, never `NEXT_PUBLIC_*`. |
| `ZIF_HEALTHCHECK_URL` | worker | none | no | no | Pinged after every successful loop. |
| `ZIF_IMAGE_TAG` | compose | none | yes | no | Release tag (`vX.Y.Z`) for the three GHCR images. |
| `ZIF_API_URL` | frontend | none | yes | no | Backend base URL the web app proxies `/api` to. |
| **Telemetry** | | | | | |
| `ZIF_OTEL_EXPORTER_OTLP_ENDPOINT` | compose (api, worker, migrations, frontend) | none | no | no | Feeds `OTEL_EXPORTER_OTLP_ENDPOINT`, the OTLP/HTTP collector endpoint. Unset exports nothing (the off switch). `make observe` sets it to the shared local stack; see Observability. |
| `ZIF_OTEL_EXPORTER_OTLP_HEADERS` | compose (api, worker, migrations, frontend) | none | no | yes | Feeds `OTEL_EXPORTER_OTLP_HEADERS`, the collector's auth headers, e.g. `Authorization=Basic%20<base64 instance:token>`. URL-encoded, comma-separated `key=value` pairs. A malformed entry is dropped without a log line (the SDK's warning would quote the token), and export then fails with an auth error. |
| `ZIF_OTEL_TRACES_SAMPLER` | compose, prod only (api, worker, migrations, frontend) | `parentbased_traceidratio` | no | no | Feeds `OTEL_TRACES_SAMPLER`; `always_off` stops traces with the endpoint still set. Dev compose sets neither sampler variable, so dev stays at the SDK's own default, 100% (`parentbased_always_on`). |
| `ZIF_OTEL_TRACES_SAMPLER_ARG` | compose, prod only (api, worker, migrations, frontend) | `0.1` | no | no | Feeds `OTEL_TRACES_SAMPLER_ARG`. |
| `ZIF_OTEL_TRACES_EXPORTER` | compose (api, worker, migrations, frontend) | `otlp` | no | no | Feeds `OTEL_TRACES_EXPORTER`; `none` turns traces off with the endpoint still set. Any other value (`otlp`, unset) means on; only `otlp` is implemented. |
| `ZIF_OTEL_METRICS_EXPORTER` | compose (api, worker, migrations, frontend) | `otlp` | no | no | Feeds `OTEL_METRICS_EXPORTER`; `none` turns metrics off with the endpoint still set. Any other value (`otlp`, unset) means on; only `otlp` is implemented. |
| `OTEL_SERVICE_NAME` | api, worker, migrations, frontend | `ziftbook-api`/`ziftbook-worker`/`ziftbook-migrations`/`ziftbook-web` | no | no | Read by the OTel SDK itself; each process sets its own default if unset. Not set by compose. |
| `OTEL_RESOURCE_ATTRIBUTES` | api, worker, migrations, frontend | none | no | no | Read by the OTel SDK itself; extra resource attributes. Not set by compose. |

`node scripts/check-env-names.mjs` (part of `make lint`) fails if a `ZIF_*` name read in code, or in a compose
file, is missing from this table. Any name beginning with `OTEL_` is allowed everywhere: those are read by the
OpenTelemetry SDK itself, never by our own config classes.
