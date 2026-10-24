# Security findings tracker: assessment of 2026-10-23

This file tracks the 36 findings (`ZB-01` … `ZB-36`) of the static security assessment run on 2026-10-23,
and the work that fixes them. It is the hand-off point for whoever records the findings in Jira.

The full assessment, with code locations and exploit narratives, is kept out of this public repository
until the fixes are released. The repository owner holds it. Titles here describe the area and the
weakness at the level of detail the fix PRs already disclose.

## For the agent that records these in Jira

1. Use one Jira issue per finding ID; findings that share a PR still get their own issue. Search for an
   existing issue first (the ID `ZB-NN` in the summary or a label) and update it instead of creating a
   duplicate.
2. Suggested fields:
   - summary `ZB-NN: <title from the table>`;
   - labels `security` and `security-assessment-2026-10-23`;
   - priority from the severity (Critical → Highest, Medium → High, Low → Medium, Info → Low);
   - description: the title, area, status, PR link and the notes column.
3. Link the PR (GitHub URL) on the issue. A finding whose fix is split over several PRs links all of them.
4. Status mapping: `Fixed in PR` → In Review; `Merged` → Done (pending release); `Released` → Done;
   `Needs owner action` → Blocked (assign to the repository owner); `Accepted` / `Deferred` → Won't Do or
   Backlog, with the reason from the notes.
5. Write the Jira key back into the `Jira` column of the table below in a small PR titled
   `docs(security): record Jira keys for the 2026-10-23 findings`, so the two stay reconciled.
6. Do not copy exploit details into Jira comments beyond what is here. Ask the repository owner for the
   full assessment if a ticket needs it.

## Status legend

| Status | Meaning |
|---|---|
| Fixed in PR | Implemented, reviewed (task review plus whole-branch review) and open as a PR |
| In progress | Implemented on a branch, still in review, not yet pushed |
| Merged | On `main`, not yet in a release |
| Released | In a deployed release |
| Needs owner action | Code is done or not possible in this repo; a person must act (listed below) |
| Accepted | Risk accepted with a mitigating control; reason in the notes |
| Deferred | Planned for later; reason in the notes |

## PRs and merge order

The security PRs form one stack. Merge them in this order; each is based on the previous one. The repo
only allows squash merges, so after a parent is merged, rebase its child onto `main` before merging the
child: `git rebase --onto origin/main <merged-parent-branch> <child-branch>`, then force-push the child.

| Order | PR | Branch | Findings |
|---|---|---|---|
| 1 | [#329](https://github.com/fjcloudaiconsulting/ziftbook/pull/329) | `security/deps` | ZB-33, ZB-34, ZB-35, ZB-36, ZB-06 |
| 2 | [#331](https://github.com/fjcloudaiconsulting/ziftbook/pull/331) | `security/booking-hardening` | ZB-02, ZB-03, ZB-13, ZB-27, ZB-14, ZB-16 |
| 3 | [#332](https://github.com/fjcloudaiconsulting/ziftbook/pull/332) | `security/supply-chain` | ZB-07, ZB-22, ZB-23, ZB-24, ZB-26 |
| 4 | [#333](https://github.com/fjcloudaiconsulting/ziftbook/pull/333) | `security/auth-abuse` | ZB-20, ZB-04, ZB-01, ZB-17, ZB-18, ZB-19, ZB-05, ZB-25 |
| 5 | [#334](https://github.com/fjcloudaiconsulting/ziftbook/pull/334) | `security/db-privileges` | ZB-15, ZB-11, ZB-09, ZB-10, ZB-08 (part 1), ZB-31, ZB-16 (part 2) |
| 6 | [#335](https://github.com/fjcloudaiconsulting/ziftbook/pull/335) | `security/web-headers` | ZB-28, ZB-29, ZB-30, ZB-12 |
| 7 (draft) | pending | `security/db-privileges-contract` | ZB-08 (part 2), ZB-15 (contract) |

Related, independent of the stack:
- [#326](https://github.com/fjcloudaiconsulting/ziftbook/pull/326) Python 3.15 compatibility, which
  unblocks Renovate #284.
- [#327](https://github.com/fjcloudaiconsulting/ziftbook/pull/327) frontend OpenTelemetry v3, from the
  Renovate dashboard.

## Findings

| ID | Title | Severity | Area | Status | PR | Jira | Notes |
|---|---|---|---|---|---|---|---|
| ZB-33 | Image library (Pillow 12.3.0) decoder advisories reachable from photo uploads | Critical | Dependency / backend | Fixed in PR | #329 | | Pillow 13.0.0, plus a version-independent container and EXIF guard. Containment until deployed: see owner actions. |
| ZB-34 | Next.js 16.4.0 and sharp 0.35.4 advisories (assessed not reachable) | Medium | Dependency / frontend | Fixed in PR | #329 | | next 16.4.2; image optimizer off |
| ZB-01 | Sign-in rate limits keyed on the email alone allow account lockout | Medium | Backend | Fixed in PR | #333 | | Per-address limits plus a challenge after repeated failures; web form handles the challenge |
| ZB-02 | Workers could cancel paid or started bookings (refund-bearing) | Medium | Backend / web | Fixed in PR | #331 | | Owner-only for started or paid bookings |
| ZB-03 | No quotas on client-facing booking email | Medium | Backend | Fixed in PR | #331 | | Per-business caps (business-initiated and public separately) and per-recipient caps |
| ZB-04 | Password-hashing concurrency and IPv6 rate-limit granularity | Medium | Backend | Fixed in PR | #333 | | IPv6 /48 for IP-only auth buckets; per-account keys stay /64. Edge rate rules: owner action. |
| ZB-05 | Unverified guest bookings per business are uncapped | Medium | Backend | Fixed in PR | #333 | | Per-business cap on unproven bookings plus an owner alert |
| ZB-06 | Dependency updates taken within hours of publication | Medium | Supply chain | Fixed in PR | #329 | | Renovate release-age cooldown; security updates skip it |
| ZB-07 | `make setup` trusted a tracked hooks directory | Low | Supply chain | Fixed in PR | #332 | | Installs only the commit-msg hook |
| ZB-08 | App database role can mint email tokens | Low | Database | Fixed in PR (part 1) | #334 | | Part 1 (#334): the worker connects as its own role, which mints email tokens. Part 2 (contract PR, one release later): the app role can no longer mint tokens or set an invite token. |
| ZB-09 | Booking evidence columns writable by the app role | Low | Database | Fixed in PR | #334 | | Column grants plus a status-transition trigger |
| ZB-10 | App role keeps unused write privileges | Low | Database | Fixed in PR (part 1) | #334 | | Unused write grants revoked. DELETE on memberships is revoked by the contract PR, once no deployed release deletes a membership. |
| ZB-11 | Test suite could run destructive migrations against a non-local database | Low | Tooling | Fixed in PR | #334 | | |
| ZB-12 | Missing web security headers (CSP, HSTS, nosniff) | Low | Frontend | Fixed in PR | #335 | | Static headers apply on deploy. The nonce CSP is report-only until `ZIF_CSP_ENFORCE=true`: owner action. |
| ZB-13 | Public health endpoint probes the database without limits | Low | Backend | Fixed in PR | #331 | | Cached, single-flight, time-bounded. Edge rate rule: owner action. |
| ZB-14 | Workers could read client internal notes | Low | Backend | Fixed in PR | #331 | | Owner-only read and write |
| ZB-15 | Public payment routes relied on the booking id alone | Low | Backend / web | Fixed in PR | #334 | | Per-booking checkout secret cookie. The contract PR stops accepting bookings placed before the secret existed. Staging 3-D Secure check: owner action. |
| ZB-16 | Photo save held a DB transaction while decoding | Low | Backend | Fixed in PR | #331, #334 | | Part 1 (#331): decode outside the transaction. Part 2 (#334): explicit pool sizes, a fast busy answer when the pool is full, and an idle-in-transaction timeout. |
| ZB-17 | Sign-up and reset requests had no bot check | Low | Backend / web | Fixed in PR | #333 | | |
| ZB-18 | Admin sign-in had no per-account cap across addresses | Low | Backend / web | Fixed in PR | #333 | | Challenge after repeated failures plus an alert |
| ZB-19 | Public booking address limits: lockout and alias bypass | Low | Backend | Fixed in PR | #333 | | |
| ZB-20 | Bot-check result validated only `success`; opt-out logged at INFO | Low | Backend / web | Fixed in PR | #333 | | Hostname and action checked; deploy web and API together |
| ZB-21 | Org workflows referenced by a movable tag with write permissions | Low | CI | Accepted | | | Mitigated by the org's `v*` tag-protection ruleset. Full fix needs the org `.github` repo and deploy verification to accept SHA-pinned signer identities: owner action. |
| ZB-22 | Landing deploy installed wrangler without a lockfile | Low | CI | Fixed in PR | #332 | | |
| ZB-23 | Stripe SDK request logs not silenced | Low | Logging | Fixed in PR | #332 | | |
| ZB-24 | Admin enrolment ticket printed to the log stream | Low | Logging | Fixed in PR | #332 | | |
| ZB-25 | Rate-limit keys stored unsalted hashes and raw addresses | Low | Database | Fixed in PR | #333 | | |
| ZB-26 | Production compose and development values lacked safety rails | Low | Config | Fixed in PR | #332 | | |
| ZB-27 | Legacy consent wording accepted on new bookings | Info | Backend | Fixed in PR | #331 | | |
| ZB-28 | Proxy passed upstream response headers through unfiltered | Info | Frontend | Fixed in PR | #335 | | |
| ZB-29 | Client-chosen request ids trusted end to end | Info | Frontend | Fixed in PR | #335 | | The proxy mints the request id; a client-sent id is kept, validated, in its own log field |
| ZB-30 | Interactive API docs routes enabled | Info | Backend | Fixed in PR | #335 | | |
| ZB-31 | Pooled DB connections could carry temp tables or settings | Info | Database | Fixed in PR | #334 | | Connections are reset on check-in. TEMPORARY is revoked by bootstrap.sql, and by the migration only where the migrate role owns the database: check production (owner action). |
| ZB-32 | A business keeps read access to a removed member's account row | Info | Database | Deferred | | | Needs a product decision on what history keeps |
| ZB-35 | CA bundle (certifi) trusted distrusted roots | Low | Dependency | Fixed in PR | #329 | | |
| ZB-36 | Unreachable or dev-only dependency advisories | Info | Dependency | Fixed in PR | #329 | | mako upgraded; dev-only npm advisories follow Renovate |

## Owner actions (need a person; not possible from this repo)

| # | Action | Findings | Urgency |
|---|---|---|---|
| 1 | Merge and release PR #329. Until the API runs Pillow 13, turn photo processing off by unsetting a `ZIF_S3_*` variable on the API (photo routes then answer 503; existing photos still load). | ZB-33 | Today |
| 2 | Review and merge the security PRs in stack order, then cut releases (release-please). | all | This week |
| 3 | Before deploying #333: set `ZIF_RATE_LIMIT_KEY` (32+ characters) for both the API and the worker (counters restart once when it is first set); deploy the web app with or before the API; check that the host in `ZIF_APP_URL` is the host the challenge widget runs on. | ZB-25, ZB-20 | With the auth release |
| 4 | Confirm the production and staging settings: Turnstile secret set, opt-out unset; site key set for the web app (sign-in, admin sign-in, sign-up and reset need it); client-IP header and trusted proxies; setup token removed; log level; NetworkPolicies; no NodePort/LoadBalancer. | ZB-20, ZB-01, ZB-17, ZB-18 | This week |
| 5 | With Cloudflare's test site key, check that the challenge widget appears and works on sign-in, admin sign-in and the invite page. | ZB-01, ZB-18 | Before the auth release |
| 6 | Cloudflare rate-limit rules on the sign-in, admin sign-in, sign-up, password-reset and invite-accept routes, and on the dependencies health check. | ZB-04, ZB-13, ZB-17 | This week |
| 7 | Worker database role (#334): the init job must run this release's `bootstrap.sql` before migrations (it creates `ziftbook_worker`; without it the migration stops the rollout); the admin role that runs it needs ADMIN on `ziftbook_app`; set `ZIF_WORKER_PASSWORD`, give the worker deployment its own secret and connection URL, and confirm the worker connects as `ziftbook_worker`. | ZB-08 | With the db-privileges release |
| 8 | Staging check: a card payment with 3-D Secure (and iDEAL) returns to the page and reaches `confirmed`. | ZB-15 | Before releasing #334 |
| 9 | After the db-privileges release: check who owns the production database and that PUBLIC no longer has TEMPORARY on it; if the migrate role does not own it, run the revoke as the database admin. | ZB-31 | After the db-privileges release |
| 10 | Contract PR (draft, merge one release after #334) only when all hold: the worker connects as `ziftbook_worker` in every environment; `ZIF_WORKER_PASSWORD` is set and non-empty everywhere; every deployed release is v0.31.0 or later (none deletes memberships); and 30 minutes have passed since the last API instance of the release before #334 stopped (the PR body has the check query). | ZB-08, ZB-10, ZB-15 | One release after #334 |
| 11 | Web-headers release (#335): give the web app `ZIF_MEDIA_URL` and `ZIF_S3_ENDPOINT` (same values as the API). On the first staging deploy, with the report-only policy, check bookings with card, Link, Apple Pay or Google Pay and 3-D Secure, the challenge widget, admin sign-in and a photo upload. Add a CSP reporting endpoint, then set `ZIF_CSP_ENFORCE=true` in staging, then in production. | ZB-12 | After the web-headers release |
| 12 | Confirm no HTTP-only subdomain exists under the app and staging hosts (HSTS `includeSubDomains`). | ZB-12 | Before the web-headers release |
| 13 | Require a reviewer on the `landing` deployment environment (repository settings). | ZB-22 | This week |
| 14 | Org `.github` repo and cloud-infra: accept SHA-pinned workflow identities, then pin workflows by SHA here. | ZB-21 | Backlog |
| 15 | Decide when to publish the full assessment (repository is public); suggested: after the fixes are released. | all | After release |

## Follow-ups outside the remediation waves

| Item | Origin | Why not now |
|---|---|---|
| Decode photos in a subprocess with a memory limit | ZB-33 fix, item 3 | Architectural change; Pillow 13 plus the guard close the reachable paths |
| Put dependency updates that add new lock packages in their own PRs; check new transitive packages' provenance once | ZB-06 fix | Process change for Renovate grouping |
| Confirm a business's contact email before it becomes Reply-To | ZB-03 | Product flow; own ticket |
| Step-up verification (email confirmation, ZIF-117) for guest bookings above a threshold, instead of pausing a business's unproven guest bookings for the hour | ZB-05 | Product flow; already ticketed as ZIF-117 |
| Count admin second-factor failures per session or with time decay, instead of resetting them on success | ZB-08, ZB-18 | Migration on the second-factor function; own ticket |
| Purge old failed sign-ins, rate-limit rows and stored IPs (sweeper) | ZB-04 | Already a planned ticket |
| A CSP reporting endpoint (`report-to`) | ZB-12 | Needed before enforcing the policy in production (owner action 11); new endpoint |
| Serve the platform admin pages from their own subdomain | ZB-12 | Isolates admin sessions from business pages; deployment change |
| Clear a booking's checkout cookie once the booking settles | ZB-15 | The cookie already expires after an hour and is useless once the booking settles |
| `bootstrap.sql` run by hand with an empty `ZIF_MIGRATE_PASSWORD` or `ZIF_APP_PASSWORD` clears that role's password | Found during ZB-08 | Present on `main` too; the compose files refuse empty values |
| Removed-member account visibility | ZB-32 | Product decision |

## Renovate PRs

| PR | State | Recommendation |
|---|---|---|
| #284 Python runtime | Fails on Python 3.15 | Merge #326 first, then let Renovate rebase it |
| #325 wrangler in the landing workflow | Green | After #332 the landing deploy runs the locked wrangler, so this input is gone; Renovate closes or rebases it, and later wrangler updates come through `landing/package.json` |
| #274 npm non-major, #301 uv | Green | Merge after the stack; rebase if the frontend or backend lockfiles conflict |
| #328 FastAPI 0.162.0 | Green | The release changes only form-field handling, which the API does not use. With the cooldown from #329, merge once it is five days old |
| #327 frontend OpenTelemetry v3 | Green | Independent of the stack |

## Other observations (not security findings)

- Photos from phones that store HDR/gain-map JPEGs (MPO) over 8.3 MP are refused as unreadable, on
  `main` as well. Worth a product bug.
- Two backend tests are sensitive to the time of day around local midnight (booking horizon and
  minimum-notice checks); they pass at other times.
- The Renovate dashboard warning "Failed to look up pypi package boto3-stubs" comes from Renovate's
  side (the package has more than 1,700 releases), not from this repo's config.
