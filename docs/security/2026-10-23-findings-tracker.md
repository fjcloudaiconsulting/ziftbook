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
| 4 | pending | `security/auth-abuse` | ZB-20, ZB-04, ZB-01, ZB-17, ZB-18, ZB-19, ZB-05, ZB-25 |
| 5 | pending | `security/db-privileges` | ZB-15, ZB-11, ZB-09, ZB-10, ZB-08 (part 1), ZB-31, ZB-16 (part 2) |
| 6 | pending | `security/web-headers` | ZB-28, ZB-29, ZB-30, ZB-12 |
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
| ZB-01 | Sign-in rate limits keyed on the email alone allow account lockout | Medium | Backend | In progress | | | Per-address limits plus a challenge after repeated failures; web form handles the challenge |
| ZB-02 | Workers could cancel paid or started bookings (refund-bearing) | Medium | Backend / web | Fixed in PR | #331 | | Owner-only for started or paid bookings |
| ZB-03 | No quotas on client-facing booking email | Medium | Backend | Fixed in PR | #331 | | Per-business caps (business-initiated and public separately) and per-recipient caps |
| ZB-04 | Password-hashing concurrency and IPv6 rate-limit granularity | Medium | Backend | In progress | | | IPv6 /48 for IP-only auth buckets; per-account keys stay /64. Edge rate rules: owner action. |
| ZB-05 | Unverified guest bookings per business are uncapped | Medium | Backend | In progress | | | Per-business cap on unproven bookings plus an owner alert |
| ZB-06 | Dependency updates taken within hours of publication | Medium | Supply chain | Fixed in PR | #329 | | Renovate release-age cooldown; security updates skip it |
| ZB-07 | `make setup` trusted a tracked hooks directory | Low | Supply chain | Fixed in PR | #332 | | Installs only the commit-msg hook |
| ZB-08 | App database role can mint email tokens | Low | Database | In progress | | | Part 1: a worker role holds token minting. Part 2 (revoke from the app role) ships one release later. |
| ZB-09 | Booking evidence columns writable by the app role | Low | Database | In progress | | | Column grants plus status-transition trigger |
| ZB-10 | App role keeps unused write privileges | Low | Database | In progress | | | |
| ZB-11 | Test suite could run destructive migrations against a non-local database | Low | Tooling | In progress | | | |
| ZB-12 | Missing web security headers (CSP, HSTS, nosniff) | Low | Frontend | In progress | | | Static headers done; nonce CSP ships report-only first |
| ZB-13 | Public health endpoint probes the database without limits | Low | Backend | Fixed in PR | #331 | | Cached, single-flight, time-bounded. Edge rate rule: owner action. |
| ZB-14 | Workers could read client internal notes | Low | Backend | Fixed in PR | #331 | | Owner-only read and write |
| ZB-15 | Public payment routes relied on the booking id alone | Low | Backend / web | In progress | | | Per-booking checkout secret cookie; staging 3-D Secure check is an owner action |
| ZB-16 | Photo save held a DB transaction while decoding | Low | Backend | Fixed in PR (part 1) | #331 | | Part 1: decode outside the transaction. Part 2: explicit pool sizes and idle-in-transaction timeout. |
| ZB-17 | Sign-up and reset requests had no bot check | Low | Backend / web | In progress | | | |
| ZB-18 | Admin sign-in had no per-account cap across addresses | Low | Backend / web | In progress | | | Challenge after repeated failures plus an alert |
| ZB-19 | Public booking address limits: lockout and alias bypass | Low | Backend | In progress | | | |
| ZB-20 | Bot-check result validated only `success`; opt-out logged at INFO | Low | Backend / web | In progress | | | Hostname and action checked; deploy web and API together |
| ZB-21 | Org workflows referenced by a movable tag with write permissions | Low | CI | Accepted | | | Mitigated by the org's `v*` tag-protection ruleset. Full fix needs the org `.github` repo and deploy verification to accept SHA-pinned signer identities: owner action. |
| ZB-22 | Landing deploy installed wrangler without a lockfile | Low | CI | Fixed in PR | #332 | | |
| ZB-23 | Stripe SDK request logs not silenced | Low | Logging | Fixed in PR | #332 | | |
| ZB-24 | Admin enrolment ticket printed to the log stream | Low | Logging | Fixed in PR | #332 | | |
| ZB-25 | Rate-limit keys stored unsalted hashes and raw addresses | Low | Database | In progress | | | |
| ZB-26 | Production compose and development values lacked safety rails | Low | Config | Fixed in PR | #332 | | |
| ZB-27 | Legacy consent wording accepted on new bookings | Info | Backend | Fixed in PR | #331 | | |
| ZB-28 | Proxy passed upstream response headers through unfiltered | Info | Frontend | In progress | | | |
| ZB-29 | Client-chosen request ids trusted end to end | Info | Frontend | In progress | | | |
| ZB-30 | Interactive API docs routes enabled | Info | Backend | In progress | | | |
| ZB-31 | Pooled DB connections could carry temp tables or settings | Info | Database | In progress | | | |
| ZB-32 | A business keeps read access to a removed member's account row | Info | Database | Deferred | | | Needs a product decision on what history keeps |
| ZB-35 | CA bundle (certifi) trusted distrusted roots | Low | Dependency | Fixed in PR | #329 | | |
| ZB-36 | Unreachable or dev-only dependency advisories | Info | Dependency | Fixed in PR | #329 | | mako upgraded; dev-only npm advisories follow Renovate |

## Owner actions (need a person; not possible from this repo)

| # | Action | Findings | Urgency |
|---|---|---|---|
| 1 | Merge and release PR #329. Until the API runs Pillow 13, turn photo processing off by unsetting a `ZIF_S3_*` variable on the API (photo routes then answer 503; existing photos still load). | ZB-33 | Today |
| 2 | Review and merge the security PRs in stack order, then cut releases (release-please). | all | This week |
| 3 | Cloudflare rate-limit rules on the sign-in, admin sign-in, sign-up, password-reset and invite-accept routes, and on the dependencies health check. | ZB-04, ZB-13, ZB-17 | This week |
| 4 | Confirm the production and staging settings: Turnstile secret set, opt-out unset; site key set for the web app; client-IP header and trusted proxies; setup token removed; log level; NetworkPolicies; no NodePort/LoadBalancer. | ZB-20, ZB-01 | This week |
| 5 | Staging check: a card payment with 3-D Secure (and iDEAL) returns to the page and reaches `confirmed`. | ZB-15 | Before releasing the checkout-secret change |
| 6 | Worker database role: give the worker deployment its own secret and connection URL (cloud-infra) before deploying the worker-role release. Then merge the contract PR (#7 in the stack) one release later. | ZB-08 | With the db-privileges release |
| 7 | CSP: deploy in report-only mode, check bookings (card, Turnstile) and admin sign-in, then set `ZIF_CSP_ENFORCE=true` in staging, then in production. | ZB-12 | After the web-headers release |
| 8 | Org `.github` repo and cloud-infra: accept SHA-pinned workflow identities, then pin workflows by SHA here. | ZB-21 | Backlog |
| 9 | Require a reviewer on the `landing` deployment environment (repository settings). | ZB-22 | This week |
| 10 | Confirm no HTTP-only subdomain exists under the app and staging hosts (HSTS `includeSubDomains`). | ZB-12 | Before the web-headers release |
| 11 | Decide when to publish the full assessment (repository is public); suggested: after the fixes are released. | all | After release |

## Follow-ups outside the remediation waves

| Item | Origin | Why not now |
|---|---|---|
| Decode photos in a subprocess with a memory limit | ZB-33 fix, item 3 | Architectural change; Pillow 13 plus the guard close the reachable paths |
| Put dependency updates that add new lock packages in their own PRs; check new transitive packages' provenance once | ZB-06 fix | Process change for Renovate grouping |
| Confirm a business's contact email before it becomes Reply-To | ZB-03 | Product flow; own ticket |
| Decay admin TOTP failures instead of resetting them on success | ZB-18 | Migration on the second-factor function; own ticket |
| Purge old failed sign-ins, rate-limit rows and stored IPs (sweeper) | ZB-04 | Already a planned ticket |
| Removed-member account visibility | ZB-32 | Product decision |

## Other observations (not security findings)

- Photos from phones that store HDR/gain-map JPEGs (MPO) over 8.3 MP are refused as unreadable, on
  `main` as well. Worth a product bug.
- Two backend tests are sensitive to the time of day around local midnight (booking horizon and
  minimum-notice checks); they pass at other times.
- The Renovate dashboard warning "Failed to look up pypi package boto3-stubs" comes from Renovate's
  side (the package has more than 1,700 releases), not from this repo's config.
