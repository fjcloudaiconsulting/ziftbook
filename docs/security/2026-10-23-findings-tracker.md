# Security remediation: findings tracker and hand-off (assessment of 2026-10-23)

This file tracks the 36 findings (`ZB-01` … `ZB-36`) of the static security assessment run on 2026-10-23,
and the work that fixes them. It is the shared, living record between the repository owner and the agents
working on this. The agent that writes the fixes has no Jira access, and the agent with Jira access sees the
work only through this file and the PRs. So this file is where each side reports what it did and what it
needs.

The full assessment, with code locations and exploit narratives, is kept out of this public repository
until the fixes are released. The repository owner holds it, together with a private hand-off for agents: the
current state, the release runbook, the open risks and the decisions taken. Ask the owner for access. Titles here describe the area and the
weakness at the level of detail the fix PRs already disclose.

## How this document works (read this first)

Whoever picks this up, agent or person:

1. Read **Status at a glance** and the newest entries of the **Hand-off log**.
2. Do your part. For the agent with Jira access, that is the section below.
3. Edit this file so it matches what is true now:
   - update statuses;
   - fill in Jira keys;
   - add rows for anything new;
   - remove rows that are wrong or obsolete, and say why in the log.
   You may change any part of it, these instructions included, if that makes the hand-off work better.
4. Add an entry at the top of the **Hand-off log** with:
   - the date (YYYY-MM-DD);
   - who you are (agent or session name, or "owner");
   - what you did;
   - what you need, and from whom.
5. Open a PR with only this file changed:
   - branch `docs/security-tracker-<date>-<topic>`;
   - title `docs(security): <what changed> in the findings tracker`.
   The repository owner merges it. If an earlier tracker PR is still open, build on its branch instead of
   starting a parallel one.

Rules:
- The repository is public. Write no exploit details, no personal data (emails, IP addresses, client
  names) and no secrets.
- Keep one row per finding in the findings table, with all eight columns.
- Do not rewrite another author's log entry. Correct it in a new entry.
- If this file and Jira disagree, the more recent evidence wins. Say in the log which side you corrected.

## Status at a glance

As of 2026-10-24, after the second merge run:
- **Merged to `main`:**
  - the whole security stack: #329, #331, #332, #333, #334 and #335;
  - this tracker (#330);
  - #326 (Python 3.15) and #327 (frontend OpenTelemetry v3);
  - Renovate's #301, #284 and #274.
  34 of the 36 findings are fixed on `main`. Three of them (ZB-08, ZB-10, ZB-15) also need draft #336.
- **Still open:**
  - Renovate's #328 (FastAPI): waits out the five-day release-age cooldown;
  - draft #336: based on `main`; it merges one release after #334.
- **Accepted and deferred:** ZB-21 is accepted with a mitigating control. ZB-32 is deferred until a product
  decision.
- **Reviews:**
  - Every PR had a task review and a whole-branch review.
  - Two security reviews of the whole stack found no new vulnerability.
- **Release:** nothing is released yet. The release PR (#321, release-please) now carries the whole stack.
  Owner actions 3, 4, 5, 7 and 8 come before merging it, and 9, 11 and 16 after deploying.

## For the agent with Jira access

1. **Project.** This repository's tickets are in the Jira project whose keys appear in its commits
   (`ZIF-NN`, for example ZIF-112 and ZIF-117). Use it unless the owner says otherwise.
2. **One issue per finding.** Search first, by `ZB-NN` in the summary or by label, and update an existing
   issue instead of creating a duplicate. Fields:
   - summary `ZB-NN: <title from the table>`;
   - labels `security` and `security-assessment-2026-10-23`;
   - priority from the severity: Critical → Highest, Medium → High, Low → Medium, Info → Low;
   - description: the title, area, PR links and the notes column.
3. **Record the outcome** as a comment on each issue:
   - the PR or PRs that fix it;
   - once merged, that it is on `main` (the PR page links the commit);
   - once released, the version, also set as the fix version;
   - anything from the notes column that the owner must still do.
4. **When to close:**
   - **Merged** or **Released** → Done, resolution Fixed.
   - **Split fixes** (ZB-08, ZB-10, ZB-15) stay In Progress until draft #336 merges too. Add a comment saying
     part 1 is in.
   - **Accepted** (ZB-21) → close as Won't Do or Accepted Risk, with the reason from the notes. The real fix
     is owner action 14; track it as its own task.
   - **Deferred** (ZB-32) → leave it in Backlog with the reason.
   - **Fixed in PR** → In Review, linked to the PR.
5. **Owner actions:** one task per row of the owner actions table, assigned to the repository owner and
   linked to its findings. Close it when the owner confirms, then mark the row Done here.
6. **Follow-ups:** one backlog issue per row of the follow-ups table, unless the row already names one
   (ZIF-117).
7. **Write back:**
   - put every Jira key in the `Jira` column of its table;
   - add your entry to the hand-off log;
   - open the PR (step 5 above).
8. **Exploit details:** copy none into Jira beyond what is here. If a ticket needs the full assessment, ask
   the owner.

## Hand-off log

Newest first.

### 2026-10-24: repository agent (Claude Code, ziftbook session), after the second merge run
- **Done:**
  - Merged: #330 (this file), #326, #327 and Renovate's #284 and #274 (#274 after Renovate rebased it past a
    lockfile conflict with #327). Every merged commit is authored by the owner's GitHub account.
  - This update is the first tracker change made through its own PR, as "How this document works" describes.
- **Open:**
  - Renovate's #328 (FastAPI), until it is five days old;
  - draft #336, one release after #334;
  - release PR #321.
- **Needs:**
  - **Owner:** merge this PR, then the owner actions before release PR #321.
  - **Jira agent:** the findings marked Merged can close as Done. Nothing else changed for Jira.

### 2026-10-24: repository agent (Claude Code, ziftbook session), after the first merge run
- **Done:**
  - The owner's merge run squash-merged the stack (#329, #331–#335) and #301. `main`'s CI passed on each.
  - Every merged commit is authored by the owner's GitHub account.
  - Draft #336 was rebased onto `main`.
  - Statuses in this file are now Merged, or Merged (part 2 in draft) for ZB-08, ZB-10 and ZB-15.
- **Not merged in that run:**
  - #330 (this file), because its branch was checked out in a local worktree;
  - #326 and #327, which passed their checks but hit a timing bug in the merge script, since fixed;
  - #274 and #284, which conflict on lockfiles.
- **Needs:**
  - **Owner:** run the merge script again for #330, #326 and #327. Then the owner actions before merging release
    PR #321.
  - **Jira agent:** create or update the issues. Findings marked Merged can close as Done (see "When to close").
    ZB-08, ZB-10 and ZB-15 stay open until #336 merges.

### 2026-10-24: repository agent (Claude Code, ziftbook session)
- **Done:**
  - fixes for 34 findings in #329 and #331–#335, plus draft #336 for the contract steps;
  - #326 and #327 to unblock Renovate;
  - this tracker.
- **Reviews:** two security reviews of the whole stack found no new vulnerability. The pre-merge review
  corrected deploy instructions, which are now in the PR descriptions of #334 and #336 and in owner actions
  7, 9, 10 and 16.
- **State:** the owner is merging the stack. #274 and #284 conflict with it on lockfiles and wait for Renovate
  to rebase them.
- **Needs:**
  - **Jira agent:** create or update the issues (section above) and write the keys back here.
  - **Owner:** finish the merge, then the owner actions before releasing.
  - **Repository agent (me):** record the merges here once the merge run ends.

## Status legend

| Status | Meaning |
|---|---|
| Fixed in PR | Implemented, reviewed (task review plus whole-branch review) and open as a PR |
| Fixed in PR (part 2 in draft) | Part 1 open as a PR; the contract part is a draft PR merged one release later |
| Merged (part 2 in draft) | Part 1 on `main`; the contract part is a draft PR (#336) merged one release later |
| In progress | Implemented on a branch, still in review, not yet pushed |
| Merged | On `main`, not yet in a release |
| Released | In a deployed release |
| Needs owner action | Code is done or not possible in this repo; a person must act (listed below) |
| Accepted | Risk accepted with a mitigating control; reason in the notes |
| Deferred | Planned for later; reason in the notes |

## PRs and merge order

The security PRs formed one stack, each based on the previous one, and were squash-merged in this order on
2026-10-24. Draft #336 is now based on `main`.

| Order | PR | Branch | Findings | State |
|---|---|---|---|---|
| 1 | [#329](https://github.com/fjcloudaiconsulting/ziftbook/pull/329) | `security/deps` | ZB-33, ZB-34, ZB-35, ZB-36, ZB-06 | Merged as e9297b0 |
| 2 | [#331](https://github.com/fjcloudaiconsulting/ziftbook/pull/331) | `security/booking-hardening` | ZB-02, ZB-03, ZB-13, ZB-27, ZB-14, ZB-16 | Merged as 7b883ec |
| 3 | [#332](https://github.com/fjcloudaiconsulting/ziftbook/pull/332) | `security/supply-chain` | ZB-07, ZB-22, ZB-23, ZB-24, ZB-26 | Merged as 1db3f2b |
| 4 | [#333](https://github.com/fjcloudaiconsulting/ziftbook/pull/333) | `security/auth-abuse` | ZB-20, ZB-04, ZB-01, ZB-17, ZB-18, ZB-19, ZB-05, ZB-25 | Merged as c376409 |
| 5 | [#334](https://github.com/fjcloudaiconsulting/ziftbook/pull/334) | `security/db-privileges` | ZB-15, ZB-11, ZB-09, ZB-10, ZB-08 (part 1), ZB-31, ZB-16 (part 2) | Merged as 515da36 |
| 6 | [#335](https://github.com/fjcloudaiconsulting/ziftbook/pull/335) | `security/web-headers` | ZB-28, ZB-29, ZB-30, ZB-12 | Merged as 4b29f61 |
| 7 (draft) | [#336](https://github.com/fjcloudaiconsulting/ziftbook/pull/336) | `security/db-privileges-contract` | ZB-08 (part 2), ZB-10 (contract), ZB-15 (contract). Merge one release after #334, when owner action 10 holds. | Draft, rebased onto `main` |

Related, independent of the stack (both merged on 2026-10-24):
- [#326](https://github.com/fjcloudaiconsulting/ziftbook/pull/326) Python 3.15 compatibility, which
  unblocked Renovate #284.
- [#327](https://github.com/fjcloudaiconsulting/ziftbook/pull/327) frontend OpenTelemetry v3, from the
  Renovate dashboard.

## Findings

| ID | Title | Severity | Area | Status | PR | Jira | Notes |
|---|---|---|---|---|---|---|---|
| ZB-33 | Image library (Pillow 12.3.0) decoder advisories reachable from photo uploads | Critical | Dependency / backend | Merged | #329 | | Pillow 13.0.0, plus a version-independent container and EXIF guard. Containment until deployed: see owner actions. |
| ZB-34 | Next.js 16.4.0 and sharp 0.35.4 advisories (assessed not reachable) | Medium | Dependency / frontend | Merged | #329 | | next 16.4.2; image optimizer off |
| ZB-01 | Sign-in rate limits keyed on the email alone allow account lockout | Medium | Backend | Merged | #333 | | Per-address limits plus a challenge after repeated failures; web form handles the challenge |
| ZB-02 | Workers could cancel paid or started bookings (refund-bearing) | Medium | Backend / web | Merged | #331 | | Owner-only for started or paid bookings |
| ZB-03 | No quotas on client-facing booking email | Medium | Backend | Merged | #331 | | Per-business caps (business-initiated and public separately) and per-recipient caps |
| ZB-04 | Password-hashing concurrency and IPv6 rate-limit granularity | Medium | Backend | Merged | #333 | | IPv6 /48 for IP-only auth buckets; per-account keys stay /64. Edge rate rules: owner action. |
| ZB-05 | Unverified guest bookings per business are uncapped | Medium | Backend | Merged | #333 | | Per-business cap on unproven bookings plus an owner alert |
| ZB-06 | Dependency updates taken within hours of publication | Medium | Supply chain | Merged | #329 | | Renovate release-age cooldown; security updates skip it |
| ZB-07 | `make setup` trusted a tracked hooks directory | Low | Supply chain | Merged | #332 | | Installs only the commit-msg hook |
| ZB-08 | App database role can mint email tokens | Low | Database | Merged (part 2 in draft) | #334, #336 | | Part 1 (#334): the worker connects as its own role, which mints email tokens. Part 2 (contract PR #336, one release later): the app role can no longer mint tokens or set an invite token. |
| ZB-09 | Booking evidence columns writable by the app role | Low | Database | Merged | #334 | | Column grants plus a status-transition trigger |
| ZB-10 | App role keeps unused write privileges | Low | Database | Merged (part 2 in draft) | #334, #336 | | Unused write grants revoked. DELETE on memberships is revoked by the contract PR #336, once no deployed release deletes a membership. |
| ZB-11 | Test suite could run destructive migrations against a non-local database | Low | Tooling | Merged | #334 | | |
| ZB-12 | Missing web security headers (CSP, HSTS, nosniff) | Low | Frontend | Merged | #335 | | Static headers apply on deploy. The nonce CSP is report-only until `ZIF_CSP_ENFORCE=true`: owner action. |
| ZB-13 | Public health endpoint probes the database without limits | Low | Backend | Merged | #331 | | Cached, single-flight, time-bounded. Edge rate rule: owner action. |
| ZB-14 | Workers could read client internal notes | Low | Backend | Merged | #331 | | Owner-only read and write |
| ZB-15 | Public payment routes relied on the booking id alone | Low | Backend / web | Merged | #334, #336 | | Per-booking checkout secret cookie. The contract PR #336 stops accepting bookings placed before the secret existed. Staging 3-D Secure check: owner action. |
| ZB-16 | Photo save held a DB transaction while decoding | Low | Backend | Merged | #331, #334 | | Part 1 (#331): decode outside the transaction. Part 2 (#334): explicit pool sizes, a fast busy answer when the pool is full, and an idle-in-transaction timeout. |
| ZB-17 | Sign-up and reset requests had no bot check | Low | Backend / web | Merged | #333 | | |
| ZB-18 | Admin sign-in had no per-account cap across addresses | Low | Backend / web | Merged | #333 | | Challenge after repeated failures plus an alert |
| ZB-19 | Public booking address limits: lockout and alias bypass | Low | Backend | Merged | #333 | | |
| ZB-20 | Bot-check result validated only `success`; opt-out logged at INFO | Low | Backend / web | Merged | #333 | | Hostname and action checked; deploy web and API together |
| ZB-21 | Org workflows referenced by a movable tag with write permissions | Low | CI | Accepted | | | Mitigated by the org's `v*` tag-protection ruleset. Full fix needs the org `.github` repo and deploy verification to accept SHA-pinned signer identities: owner action. |
| ZB-22 | Landing deploy installed wrangler without a lockfile | Low | CI | Merged | #332 | | |
| ZB-23 | Stripe SDK request logs not silenced | Low | Logging | Merged | #332 | | |
| ZB-24 | Admin enrolment ticket printed to the log stream | Low | Logging | Merged | #332 | | |
| ZB-25 | Rate-limit keys stored unsalted hashes and raw addresses | Low | Database | Merged | #333 | | |
| ZB-26 | Production compose and development values lacked safety rails | Low | Config | Merged | #332 | | |
| ZB-27 | Legacy consent wording accepted on new bookings | Info | Backend | Merged | #331 | | |
| ZB-28 | Proxy passed upstream response headers through unfiltered | Info | Frontend | Merged | #335 | | |
| ZB-29 | Client-chosen request ids trusted end to end | Info | Frontend | Merged | #335 | | The proxy mints the request id; a client-sent id is kept, validated, in its own log field |
| ZB-30 | Interactive API docs routes enabled | Info | Backend | Merged | #335 | | |
| ZB-31 | Pooled DB connections could carry temp tables or settings | Info | Database | Merged | #334 | | Connections are reset on check-in. TEMPORARY is revoked by bootstrap.sql, and by the migration only where the migrate role owns the database: check production (owner action). |
| ZB-32 | A business keeps read access to a removed member's account row | Info | Database | Deferred | | | Needs a product decision on what history keeps |
| ZB-35 | CA bundle (certifi) trusted distrusted roots | Low | Dependency | Merged | #329 | | |
| ZB-36 | Unreachable or dev-only dependency advisories | Info | Dependency | Merged | #329 | | mako upgraded; dev-only npm advisories follow Renovate |

## Owner actions (need a person; not possible from this repo)

| # | Action | Findings | Urgency | State | Jira |
|---|---|---|---|---|---|
| 1 | Merge and release PR #329. Until the API runs Pillow 13, turn photo processing off by unsetting a `ZIF_S3_*` variable on the API (photo routes then answer 503; existing photos still load). | ZB-33 | Today | Merged; release pending |  |
| 2 | Review and merge the security PRs in stack order, then cut releases (release-please). | all | This week | Merged; release pending |  |
| 3 | Before deploying #333: set `ZIF_RATE_LIMIT_KEY` (32+ characters) for both the API and the worker (counters restart once when it is first set); deploy the web app with or before the API; check that the host in `ZIF_APP_URL` is the host the challenge widget runs on. | ZB-25, ZB-20 | With the auth release | Open |  |
| 4 | Confirm the production and staging settings: Turnstile secret set, opt-out unset; site key set for the web app (sign-in, admin sign-in, sign-up and reset need it); client-IP header and trusted proxies; setup token removed; log level; NetworkPolicies; no NodePort/LoadBalancer. | ZB-20, ZB-01, ZB-17, ZB-18 | This week | Open |  |
| 5 | With Cloudflare's test site key, check that the challenge widget appears and works on sign-in, admin sign-in and the invite page. | ZB-01, ZB-18 | Before the auth release | Open |  |
| 6 | Cloudflare rate-limit rules on the sign-in, admin sign-in, sign-up, password-reset and invite-accept routes, and on the dependencies health check. | ZB-04, ZB-13, ZB-17 | This week | Open |  |
| 7 | Worker database role (#334). The init job runs this release's `bootstrap.sql` before the migrations, connected to the application's database (`-d`), with `ZIF_WORKER_PASSWORD` set; without the role the migration stops the rollout. The admin role that runs it needs ADMIN on `ziftbook_app`. Give the worker its own secret and connection URL, separate from the API's: an API switched to the worker's URL could mint email tokens itself. Confirm in `pg_stat_activity` that the API connects as `ziftbook_app` and the worker as `ziftbook_worker` (query in #334). | ZB-08 | With the db-privileges release | Open |  |
| 8 | Staging check: a card payment with 3-D Secure (and iDEAL) returns to the page and reaches `confirmed`. | ZB-15 | Before releasing #334 | Open |  |
| 9 | After the db-privileges release: `has_database_privilege('ziftbook_app', current_database(), 'TEMPORARY')` and the same for `ziftbook_worker` must both be false. Where the admin role neither owns the database nor belongs to its owner, the revoke in `bootstrap.sql` only warns and the migration skips too; then run the revoke as the database owner. | ZB-31 | After the db-privileges release | Open |  |
| 10 | Contract PR #336 (draft). Merge it only after a release containing #334 has been cut and deployed everywhere (merged earlier, both halves ship in one release), and only when all of these hold: the worker connects as `ziftbook_worker` in every environment; `ZIF_WORKER_PASSWORD` is set and non-empty everywhere; every deployed release is v0.31.0 or later (none deletes memberships); and 30 minutes have passed since the last API instance of the release before #334 stopped (the PR body has the check query). If emails fail afterwards, move the worker to its own URL; never re-grant `mint_email_token`, run the downgrades or disable the invite-token trigger. | ZB-08, ZB-10, ZB-15 | One release after #334 | Open |  |
| 11 | Web-headers release (#335): give the web app `ZIF_MEDIA_URL` and `ZIF_S3_ENDPOINT` (same values as the API). On the first staging deploy, with the report-only policy, check bookings with card, Link, Apple Pay or Google Pay and 3-D Secure, the challenge widget, admin sign-in and a photo upload. Add a CSP reporting endpoint, then set `ZIF_CSP_ENFORCE=true` in staging, then in production. | ZB-12 | After the web-headers release | Open |  |
| 12 | Confirm no HTTP-only subdomain exists under the app and staging hosts (HSTS `includeSubDomains`). | ZB-12 | Before the web-headers release | Open |  |
| 13 | Require a reviewer on the `landing` deployment environment (repository settings). | ZB-22 | This week | Open |  |
| 14 | Org `.github` repo and cloud-infra: accept SHA-pinned workflow identities, then pin workflows by SHA here. | ZB-21 | Backlog | Open |  |
| 15 | Decide when to publish the full assessment (repository is public); suggested: after the fixes are released. | all | After release | Open |  |
| 16 | Admin enrolment (from #334's docs): never give the API `ZIF_MIGRATE_DATABASE_URL`. Run `python -m app.admin enrol` in a short-lived pod from the migrations image with only the migrate role's URL, through `kubectl exec -it`, then delete the pod. | ZB-24 | Next enrolment | Open |  |

## Follow-ups outside the remediation waves

| Item | Origin | Why not now | Jira |
|---|---|---|---|
| Decode photos in a subprocess with a memory limit | ZB-33 fix, item 3 | Architectural change; Pillow 13 plus the guard close the reachable paths |  |
| Put dependency updates that add new lock packages in their own PRs; check new transitive packages' provenance once | ZB-06 fix | Process change for Renovate grouping |  |
| Confirm a business's contact email before it becomes Reply-To | ZB-03 | Product flow; own ticket |  |
| Step-up verification (email confirmation, ZIF-117) for guest bookings above a threshold, instead of pausing a business's unproven guest bookings for the hour | ZB-05 | Product flow; already ticketed as ZIF-117 | ZIF-117 |
| Count admin second-factor failures per session or with time decay, instead of resetting them on success | ZB-08, ZB-18 | Migration on the second-factor function; own ticket |  |
| Purge old failed sign-ins, rate-limit rows and stored IPs (sweeper) | ZB-04 | Already a planned ticket |  |
| A CSP reporting endpoint (`report-to`) | ZB-12 | Needed before enforcing the policy in production (owner action 11); new endpoint |  |
| Serve the platform admin pages from their own subdomain | ZB-12 | Isolates admin sessions from business pages; deployment change |  |
| Clear a booking's checkout cookie once the booking settles | ZB-15 | The cookie already expires after an hour and is useless once the booking settles |  |
| `bootstrap.sql` run by hand with an empty `ZIF_MIGRATE_PASSWORD` or `ZIF_APP_PASSWORD` clears that role's password | Found during ZB-08 | Present on `main` too; the compose files refuse empty values |  |
| The API checks at startup that it connects as `ziftbook_app` and logs an ERROR otherwise | Review of #334 | Small code change; separate secrets (owner action 7) cover it until then |  |
| `bootstrap.sql` fails when PUBLIC still holds TEMPORARY after its revoke, instead of only warning | Review of #334 | Would stop deploys on hosted databases the admin does not own; owner action 9 covers it until then |  |
| `bootstrap.sql` checks `ZIF_WORKER_PASSWORD` before creating any role | Review of #336 | A stopped run is safe to re-run; documented |  |
| Removed-member account visibility | ZB-32 | Product decision |  |

## Renovate PRs

| PR | State | Recommendation |
|---|---|---|
| #284 Python runtime | Merged | |
| #325 wrangler in the landing workflow | Closed by Renovate | Superseded: since #332 the landing deploy runs the locked wrangler |
| #301 uv | Merged | |
| #274 npm non-major | Merged | |
| #328 FastAPI 0.162.0 | Open | The release changes only form-field handling, which the API does not use. Merge once it is five days old (the cooldown from #329) |
| #327 frontend OpenTelemetry v3 | Merged | |

## Other observations (not security findings)

- Photos from phones that store HDR/gain-map JPEGs (MPO) over 8.3 MP are refused as unreadable, on
  `main` as well. Worth a product bug.
- When an owner lets workers answer booking requests, a worker can decline a request that is already
  paid, and declining refunds it. This was so before the fixes (ZB-02 covers cancelling, not declining)
  and the owner opted in; worth a product decision on whether declining a paid request should be the
  owner's alone.
- Two backend tests are sensitive to the time of day around local midnight (booking horizon and
  minimum-notice checks); they pass at other times.
- The Renovate dashboard warning "Failed to look up pypi package boto3-stubs" comes from Renovate's
  side (the package has more than 1,700 releases), not from this repo's config.
