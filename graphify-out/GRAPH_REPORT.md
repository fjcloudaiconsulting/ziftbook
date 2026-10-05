# Graph Report - ziftbook  (2026-10-04)

## Corpus Check
- 118 files · ~290,505 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 4095 nodes · 14739 edges · 192 communities (120 shown, 72 thin omitted)
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 1045 edges (avg confidence: 0.93)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Accounts & Booking Page Backend
- Calendar API Client
- Account UI & API Client
- Public Bookings API Tests
- Booking Links API Tests
- Structured Logging & OTLP
- Booking Approval Tests
- Job Queue
- Booking Email Rendering
- Time Off & Access Log Tests
- Team & Invites UI
- Mail Outbox Delivery
- Merchant Booking Tests
- Console Session UI
- Week Hours Editor
- Bookings Backend
- Services UI
- Availability Intervals
- DB Tracing & Test Fixtures
- Availability API Tests
- Clients & Consents
- App Configuration
- Members API Tests
- Worker & Mail Settings
- Booking Links Backend
- Invites API Tests
- Settings API Tests
- Working Hours Tests
- FastAPI App Factory
- Service Workers Tests
- Time Off Backend
- Opening Hours Tests
- Availability Engine
- Availability API Helpers
- Bookings Range Tests
- New Booking UI Client
- Invites DB Tests
- Sign-Up Auth
- Booking Page API Tests
- Clients DB Tests
- Password Auth DB Tests
- Sign-Up Tests
- Time Off UI
- Settings UI
- Services Tests
- Cancellation Policy
- Display Names Tests
- Audit Recording Tests
- Clients API Tests
- Session API Tests
- Sign-In Tests
- Manage Booking UI
- Frontend Logging & Instrumentation
- Decline Message Tests
- Public Booking Page Client
- Tenants Tests
- Migration Tests
- Turnstile Verification
- Bookings DB Tests
- Frontend Tooling Config
- Weekly Schedule
- Services Backend
- Password Reset Tests
- Tenant Isolation Migrations
- Rate Limits
- Members Backend
- Booking Page Component
- Audit DB Tests
- Audit Events API
- Client IP Tests
- Parallel Test Safety
- Invites Backend
- OTel Tracing Setup
- Keep-An-Owner Tests
- Frontend Proxy Tracing
- TS Compiler Config
- Account Recovery Backend
- Jobs & Sessions Migrations
- Locale Layout
- Foundation Migrations
- Booking Link Helpers
- Day Shifts
- Audit Events API Tests
- Landing Worker & OTLP Gate
- Proxy Tests
- Booking Sweep
- Business Settings
- Sessions Tests
- Booking Page Upstream Load
- Session Guards
- Availability Schemas
- Tenant Isolation Tests
- Client Cancel Emails
- Invite & Reset Emails
- OpenAPI Tests
- DB Roles Tests
- Frontend Dependencies
- Tenant Schema Tests
- ICS Calendar Export
- Received & Reminder Emails
- JSON-Only Tests
- Env Name Checks
- Cancelled & Declined Emails
- Confirmed & New Booking Emails
- Frontend Dev Dependencies
- Landing Package
- Reserved Routes Check
- i18n Catalog Check
- Settings Test Helpers
- Compose & Release Docs
- Env Doc Check
- Bookings Migration
- Landing Build
- README Make Targets
- Renovate Config
- Audit Events Migration
- Business Country Migration
- Invites Migration
- Services Migration
- Drop Old Sign-Up Migration
- Cancellation Rules Migration
- Tenant Slug Migration
- User Name Migration
- Fonts & Landing Template
- Booking PATCH Rules
- Decline & Move Emails
- Hello Test Email
- Client Cleanup Fixture
- Commit-Msg Hook Test
- Release-Please Config
- Verified Account
- Contributing & PR Titles
- Brand Icons
- Schibsted Grotesk License
- Commit-Msg Hook
- Post-Checkout Hook
- Post-Commit Hook
- Port Check Script
- Isolated: Action
- Isolated: post
- Isolated: post
- Isolated: Booked
- Isolated: Interval
- Isolated: Row
- Isolated: post
- Isolated: Row
- Isolated: NamedTuple
- Isolated: patch
- Isolated: post
- Isolated: patch
- Isolated: post
- Isolated: Row
- Isolated: post
- Isolated: patch
- Isolated: Row
- Isolated: patch
- Isolated: post
- Isolated: patch
- Isolated: post
- SecLists License
- Isolated: LogRecord
- Isolated: LogCaptureFixture
- Isolated: People
- Isolated: People
- Isolated: Lines
- Isolated: Config
- Isolated: CurrentSession
- Isolated: fixture
- pnpm Workspace
- Isolated: ge
- Landing Deploy Workflow
- Isolated: Instant
- Isolated: le
- Isolated: max_length
- Isolated: model_validator
- Isolated: patch
- Backend Package
- Isolated: post
- Isolated: Purpose
- Isolated: Query
- Isolated: Row
- Isolated: Session
- Isolated: TestClient

## God Nodes (most connected - your core abstractions)
1. `People` - 813 edges
2. `signed_in()` - 308 edges
3. `new_client()` - 251 edges
4. `member_id()` - 240 edges
5. `fresh_email()` - 151 edges
6. `at()` - 128 edges
7. `patch()` - 110 edges
8. `create_app()` - 109 edges
9. `events()` - 95 edges
10. `make_pending()` - 89 edges

## Surprising Connections (you probably didn't know these)
- `Young Serif OFL License (frontend)` --semantically_similar_to--> `Young Serif OFL License (landing)`  [INFERRED] [semantically similar]
  frontend/licenses/young-serif-OFL.txt → landing/licenses/young-serif-OFL.txt
- `Ziftbook App Icon (Z logo)` --semantically_similar_to--> `Ziftbook Landing Favicon (Z logo)`  [INFERRED] [semantically similar]
  frontend/app/icon.svg → landing/static/favicon.svg
- `Schibsted Grotesk OFL License (frontend)` --semantically_similar_to--> `Schibsted Grotesk OFL License (landing)`  [INFERRED] [semantically similar]
  frontend/licenses/schibsted-grotesk-OFL.txt → landing/licenses/schibsted-grotesk-OFL.txt
- `Smoke test compose stack` --semantically_similar_to--> `Production compose stack`  [INFERRED] [semantically similar]
  compose.smoke.yaml → docker-compose-prod.yaml
- `Production compose stack` --semantically_similar_to--> `Local dev compose stack`  [INFERRED] [semantically similar]
  docker-compose-prod.yaml → docker-compose.yaml

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Client-facing booking outcome emails** — backend_app_mail_templates_booking_cancelled_concept, backend_app_mail_templates_booking_cancelled_by_client_concept, backend_app_mail_templates_booking_confirmed_concept, backend_app_mail_templates_booking_declined_concept [INFERRED 0.75]
- **Account signup and recovery flow** — backend_app_mail_templates_sign_up_en_email, backend_app_mail_templates_sign_up_registered_en_email, backend_app_mail_templates_password_reset_en_email [INFERRED 0.80]
- **Merchant notifications carrying a client self-service link** — backend_app_mail_templates_booking_client_cancelled_concept, backend_app_mail_templates_booking_client_rescheduled_concept, backend_app_mail_templates_booking_new_concept [INFERRED 0.80]
- **Time-limited single-use link mechanism** — backend_app_mail_templates_invite_en_email, backend_app_mail_templates_password_reset_en_email, backend_app_mail_templates_sign_up_en_email [INFERRED 0.80]
- **Booking notification flow (request -> received -> reminder)** — backend_app_mail_templates_booking_request_en_email, backend_app_mail_templates_booking_received_en_email, backend_app_mail_templates_booking_reminder_en_email [INFERRED 0.85]
- **Compose stacks sharing postgres/db-init/migrate/backend/frontend services** — docker_compose, docker_compose_prod, compose_smoke [INFERRED 0.85]
- **Localized booking notification email templates** — backend_app_mail_templates_booking_declined_message_en, backend_app_mail_templates_booking_moved_team_en, backend_app_mail_templates_booking_moved_team_member_en [INFERRED 0.85]

## Communities (192 total, 72 thin omitted)

### Community 0 - "Accounts & Booking Page Backend"
Cohesion: 0.07
Nodes (77): argon2, argon2_exceptions, printable(), Creating an account: sign up with an email, then complete it from the emailed…, A business's audit log, read by its owner., Server-side sessions: an opaque random token in a cookie, only its hash in the…, When a client can book a service: each worker's free slots, from working hours,…, WorkerOut (+69 more)

### Community 1 - "Calendar API Client"
Cohesion: 0.05
Nodes (95): frontend_api_client_index_agendaout, frontend_api_client_index_bookingapprovalslist, frontend_api_client_index_bookingapprovalsrange, frontend_api_client_index_bookingapprovalsread, frontend_api_client_index_bookingapprovalsupdate, frontend_api_client_index_bookingdetailout, frontend_api_client_index_pendingout, frontend_api_client_index_shift (+87 more)

### Community 2 - "Account UI & API Client"
Cohesion: 0.06
Nodes (77): frontend_api_client_index, frontend_api_client_index_accountcompletepasswordreset, frontend_api_client_index_accountcompletesignup, frontend_api_client_index_accountrequestpasswordreset, frontend_api_client_index_accountsignup, frontend_api_client_index_invitedetails, frontend_api_client_index_invitesaccept, frontend_api_client_index_inviteslookup (+69 more)

### Community 3 - "Public Bookings API Tests"
Cohesion: 0.07
Nodes (82): POST /api/public/businesses/{tenant_id}/services/{service_id}/bookings…, app(), at(), booking_url(), commit_bypassing_the_lock(), owner(), post_booking(), _published() (+74 more)

### Community 4 - "Booking Links API Tests"
Cohesion: 0.11
Nodes (79): app(), booking_jobs(), cancel_body(), confirmed_booking(), counts(), email_of_user(), first_slot_after(), get() (+71 more)

### Community 5 - "Structured Logging & OTLP"
Cohesion: 0.05
Nodes (68): bound(), configure(), _excepthook(), OtlpHandler, Exports the same allowlisted record as stdout, over OTLP. Never raises: emit()…, Idempotent. Emits nothing: `python -m app.main` prints the OpenAPI document to…, Add fields to every record logged inside, in this context and what it spawns.…, _boom() (+60 more)

### Community 6 - "Booking Approval Tests"
Cohesion: 0.10
Nodes (75): ago(), app(), confirmed_in_the_past(), events_of(), expire(), expire_in_a_second(), hold_the_tenant_lock(), make_pending() (+67 more)

### Community 7 - "Job Queue"
Cohesion: 0.05
Nodes (66): _claim(), enqueue(), JobKind, Any, datetime, Session, timedelta, UUID (+58 more)

### Community 8 - "Booking Email Rendering"
Cohesion: 0.09
Nodes (71): _local_text(), _quoted(), Every line prefixed "> " (a blank one is ">"), a run of blank lines collapsed…, value[locale], else the first present of LOCALES., The email.booking job: one client or merchant booking email, or the 24h-ahead…, send_booking(), add_worker(), app() (+63 more)

### Community 9 - "Time Off & Access Log Tests"
Cohesion: 0.11
Nodes (69): _access_fields(), access_log(), run(), Any, Request, member_id(), app(), block() (+61 more)

### Community 10 - "Team & Invites UI"
Cohesion: 0.05
Nodes (57): frontend_api_client_index_inviteout, frontend_api_client_index_invitescreate, frontend_api_client_index_invitesdelete, frontend_api_client_index_inviteslist, frontend_api_client_index_memberslist, frontend_api_client_index_memberssetdisplayname, frontend_api_client_index_workinghoursread, act() (+49 more)

### Community 11 - "Mail Outbox Delivery"
Cohesion: 0.07
Nodes (65): Job, deliver(), _mark_sent(), _outbox(), Job, Session, UUID, Email over SMTP: Mailpit in development, Mailgun's EU endpoint when deployed.… (+57 more)

### Community 12 - "Merchant Booking Tests"
Cohesion: 0.12
Nodes (68): add_membership(), ana(), ana_id(), app(), availability(), book(), boss(), clean_outbox() (+60 more)

### Community 13 - "Console Session UI"
Cohesion: 0.06
Nodes (52): frontend_api_client_index_accountgivename, frontend_api_client_index_businesssettingsoutput, frontend_api_client_index_sessionout, frontend_api_client_index_sessionsignout, frontend_api_client_index_sessionsignouteverywhere, frontend_api_client_index_settingsread, ClientsPage(), ConsoleLayout() (+44 more)

### Community 14 - "Week Hours Editor"
Cohesion: 0.08
Nodes (53): frontend_api_client_index_openinghoursread, frontend_api_client_index_openinghoursreplace, frontend_api_client_index_workinghoursreplace, HoursSection(), load(), reloadEnvelope(), HoursSectionProps, OpeningHours() (+45 more)

### Community 15 - "Bookings Backend"
Cohesion: 0.06
Nodes (60): actor_of(), agenda(), AgendaOut, book(), BookingDetailOut, BookingIn, BookingOut, BookingRow (+52 more)

### Community 16 - "Services UI"
Cohesion: 0.08
Nodes (45): frontend_api_client_index_servicescreate, frontend_api_client_index_servicesread, frontend_api_client_index_servicesreplaceworkers, frontend_api_client_index_servicesupdate, NewServicePage(), ServicesPage(), EditServicePage(), ArchivedRow() (+37 more)

### Community 17 - "Availability Intervals"
Cohesion: 0.11
Nodes (54): day_span(), merged(), Whole local days first..last as UTC [midnight(first), midnight(last + 1)). Each…, Sorted, with overlapping or touching intervals joined., at(), local(), Booked, date (+46 more)

### Community 18 - "DB Tracing & Test Fixtures"
Cohesion: 0.06
Nodes (49): _after_cursor_execute(), _before_cursor_execute(), _handle_error(), Any, listens_for, Manual OpenTelemetry spans: no contrib instrumentation (see…, app_engine(), bound() (+41 more)

### Community 19 - "Availability API Tests"
Cohesion: 0.12
Nodes (50): new_client(), FastAPI, app(), clear_time_off(), get(), insert_day_block(), local(), local_dt() (+42 more)

### Community 20 - "Clients & Consents"
Cohesion: 0.07
Nodes (52): add_consents(), as_clients(), ClientIn, ClientOut, ConsentIn, ConsentOut, create_client(), current_consents() (+44 more)

### Community 21 - "App Configuration"
Cohesion: 0.06
Nodes (42): DatabaseSettings, LogSettings, Configuration of the processes that use the database: the API and the worker., Logging, read by app.logs.configure(). Development runs DEBUG/text (docker-…, Deployment configuration from ZIF_* environment variables. Business settings…, Settings, _add_context(), _causes() (+34 more)

### Community 22 - "Members API Tests"
Cohesion: 0.12
Nodes (48): A client holding a session in that business, as that person., signed_in(), app(), Any, Engine, FastAPI, fixture, MonkeyPatch (+40 more)

### Community 23 - "Worker & Mail Settings"
Cohesion: 0.06
Nodes (39): asyncio, MailSettings, The worker's configuration., SMTP for outgoing email. Deployed: Mailgun EU (smtp.eu.mailgun.org:587,…, WorkerSettings, main(), ping(), Event (+31 more)

### Community 24 - "Booking Links Backend"
Cohesion: 0.09
Nodes (49): ApiError, origin(), The requester's address and browser, from the connection and the header, never…, AvailabilityOut, cancel(), CancelIn, confirm_consents(), ConsentsIn (+41 more)

### Community 25 - "Invites API Tests"
Cohesion: 0.13
Nodes (46): email_of(), The email add_user gives a user., app(), invite_jobs(), invite_row_count(), mint(), spy(), Any (+38 more)

### Community 26 - "Settings API Tests"
Cohesion: 0.10
Nodes (46): lines(), put_settings(), Any, Response, TestClient, The exported record, in the same shape as a parsed JSON line (test 2's parity…, _rebuild(), saved_settings() (+38 more)

### Community 27 - "Working Hours Tests"
Cohesion: 0.11
Nodes (46): app(), hours_path(), Any, date, datetime, Engine, FastAPI, fixture (+38 more)

### Community 28 - "FastAPI App Factory"
Cohesion: 0.07
Nodes (37): APIRoute, create_app(), healthz(), Health, operation_id(), BaseModel, test_the_api_is_bound_to_its_database_while_it_runs(), test_proxy_headers_is_the_outermost_middleware() (+29 more)

### Community 29 - "Service Workers Tests"
Cohesion: 0.16
Nodes (44): app(), b_service(), create(), ids(), owner(), pairs(), put(), Any (+36 more)

### Community 30 - "Time Off Backend"
Cohesion: 0.09
Nodes (41): member_user(), The user behind a member of this business; 404 for any other id. Shared with…, blocks_overlapping(), checked(), checked_days(), checked_year(), create_time_off(), delete_time_off() (+33 more)

### Community 31 - "Opening Hours Tests"
Cohesion: 0.10
Nodes (41): events(), Wait until that many sessions block on a lock in THIS database: the…, Audit events read as an operator, oldest first, matched on the given columns., wait_until_blocked(), Engine, test_the_sweep_waits_for_the_tenant_lock(), _another_owner_replaces(), app() (+33 more)

### Community 32 - "Availability Engine"
Cohesion: 0.08
Nodes (43): anchor(), booked(), buffer_for(), candidates(), clip(), compute(), member_slots(), now() (+35 more)

### Community 33 - "Availability API Helpers"
Cohesion: 0.14
Nodes (41): assign(), new_service(), Any, Engine, TestClient, A 30-minute service performed by the owner, who works 09:00-12:00 every day., A real booking row, inserted directly (the console path, not the public route):…, ready() (+33 more)

### Community 34 - "Bookings Range Tests"
Cohesion: 0.15
Nodes (42): app(), history(), hour(), ids(), insert_event(), iso(), owner(), put() (+34 more)

### Community 35 - "New Booking UI Client"
Cohesion: 0.12
Nodes (37): frontend_api_client_index_availabilitymerchantread, frontend_api_client_index_bookingapprovalscreate, frontend_api_client_index_bookingapprovalsreschedule, frontend_api_client_index_clientout, frontend_api_client_index_clientslist, frontend_api_client_index_memberout, frontend_api_client_index_serviceout, frontend_api_client_index_serviceslist (+29 more)

### Community 36 - "Invites DB Tests"
Cohesion: 0.09
Nodes (38): add_password(), mailed(), Runs the queued jobs, then returns the one message Mailpit got for email., call(), digest(), _insert(), minted(), Any (+30 more)

### Community 37 - "Sign-Up Auth"
Cohesion: 0.09
Nodes (41): complete_sign_up(), live_token(), Create the business and its owner from a sign-up link, and sign the owner in., The token's hash if it can still be used. Checked before any password is…, clear_cookie(), create(), Credentials, describe() (+33 more)

### Community 38 - "Booking Page API Tests"
Cohesion: 0.14
Nodes (40): add_worker(), app(), hits(), owner(), page(), _published(), Engine, FastAPI (+32 more)

### Community 39 - "Clients DB Tests"
Cohesion: 0.07
Nodes (40): find_or_create(), Found, The business's own record for this client, created or refreshed. `email` must…, join_tenant(), Connection, listens_for, Session, UUID (+32 more)

### Community 40 - "Password Auth DB Tests"
Cohesion: 0.14
Nodes (39): delete_services(), The app role can't delete services; fixtures remove them as the migrate role,…, complete_sign_up(), Created, digest(), email_of(), issue(), Engine (+31 more)

### Community 41 - "Sign-Up Tests"
Cohesion: 0.20
Nodes (38): fresh_email(), issue_link(), live(), A request plus its email job: the token a link would carry, or None if nothing…, app(), businesses(), client(), complete() (+30 more)

### Community 42 - "Time Off UI"
Cohesion: 0.11
Nodes (36): frontend_api_client_index_timeoffcreate, frontend_api_client_index_timeoffdelete, frontend_api_client_index_timeofflist, frontend_api_client_index_timeoffout, frontend_api_client_index_timeoffupdate, blockMeta(), blockTitle(), dayLabel() (+28 more)

### Community 43 - "Settings UI"
Cohesion: 0.13
Nodes (33): frontend_api_client_index_settingsupdate, SettingsPage(), Field(), Settings(), onSubmit(), onTogglePublish(), undo(), updateSettings() (+25 more)

### Community 44 - "Services Tests"
Cohesion: 0.17
Nodes (37): app(), extra_field(), owner(), Any, Engine, FastAPI, fixture, MonkeyPatch (+29 more)

### Community 45 - "Cancellation Policy"
Cohesion: 0.12
Nodes (30): decide(), Decision, Policy, datetime, ZIF-55's cancellation rule engine, extended by ZIF-54 for the client's own…, The SNAPSHOT on the booking row, never the live settings object., What a client may still do with a booking that starts at `starts_at`, as of…, datetime (+22 more)

### Community 46 - "Display Names Tests"
Cohesion: 0.17
Nodes (36): app(), display_name(), invite_token(), Any, Engine, FastAPI, fixture, MonkeyPatch (+28 more)

### Community 47 - "Audit Recording Tests"
Cohesion: 0.18
Nodes (34): failing(), Make one function raise, to check what its caller leaves behind., app(), businesses(), client_at(), complete_reset(), complete_sign_up(), Engine (+26 more)

### Community 48 - "Clients API Tests"
Cohesion: 0.16
Nodes (31): People, add_client(), app(), Any, Engine, FastAPI, fixture, TestClient (+23 more)

### Community 49 - "Session API Tests"
Cohesion: 0.15
Nodes (33): age(), app(), cleared(), client(), deferred_table(), last_seen(), datetime, Engine (+25 more)

### Community 50 - "Sign-In Tests"
Cohesion: 0.13
Nodes (30): app(), client(), Engine, FastAPI, fixture, MonkeyPatch, parametrize, Response (+22 more)

### Community 51 - "Manage Booking UI"
Cohesion: 0.10
Nodes (31): frontend_api_client_index_bookinglinkavailability, frontend_api_client_index_bookinglinkcancel, frontend_api_client_index_bookinglinkconsents, frontend_api_client_index_bookinglinkread, frontend_api_client_index_bookinglinkreschedule, frontend_api_client_index_bookinglinksession, frontend_api_client_index_linkedbooking, Manage() (+23 more)

### Community 52 - "Frontend Logging & Instrumentation"
Cohesion: 0.10
Nodes (31): onRequestError(), register(), requestLog, startupLog, enabled(), ErrorContext, errorFields(), errorLink() (+23 more)

### Community 53 - "Decline Message Tests"
Cohesion: 0.18
Nodes (32): clear_jobs(), run_jobs(), app(), body_of(), clean_outbox(), decline(), message_of(), owner() (+24 more)

### Community 54 - "Public Booking Page Client"
Cohesion: 0.09
Nodes (32): frontend_api_client_index_availabilityread, frontend_api_client_index_bookingpageout, frontend_api_client_index_bookingscreate, frontend_api_client_index_sessionread, CacheEntry, ClockIcon(), Done, Flow (+24 more)

### Community 55 - "Tenants Tests"
Cohesion: 0.14
Nodes (33): concurrent(), b(), drop_tenants(), Any, Connection, Engine, FastAPI, parametrize (+25 more)

### Community 56 - "Migration Tests"
Cohesion: 0.14
Nodes (32): alembic_config, delete_bookings(), The app role can delete neither booking_events, booking_links nor bookings;…, booked(), downgrade_outcome(), lock_timeout_config(), plain_insert(), Config (+24 more)

### Community 57 - "Turnstile Verification"
Cohesion: 0.12
Nodes (26): Cloudflare Turnstile. Unset means verification is skipped, so development and…, TurnstileSettings, enabled(), Cloudflare Turnstile verification. No new dependency: stdlib urllib, as…, Whether Cloudflare says this token passed. FAILS CLOSED on any verification…, verify(), FakeAnswer, json_answer() (+18 more)

### Community 58 - "Bookings DB Tests"
Cohesion: 0.11
Nodes (29): book(), insert_booking(), load_migration(), Any, datetime, Engine, parametrize, Session (+21 more)

### Community 59 - "Frontend Tooling Config"
Cohesion: 0.07
Nodes (25): eslintConfig, engines, node, name, packageManager, private, scripts, build (+17 more)

### Community 60 - "Weekly Schedule"
Cohesion: 0.12
Nodes (29): lock_week(), opening_week(), overlapping(), Any, BaseModel, Body, CurrentOwner, CurrentSession (+21 more)

### Community 61 - "Services Backend"
Cohesion: 0.12
Nodes (28): assign_workers(), create_service(), found(), list_services(), PriceIn, BaseModel, Body, CurrentOwner (+20 more)

### Community 62 - "Password Reset Tests"
Cohesion: 0.14
Nodes (28): client(), complete(), Engine, FastAPI, fixture, MonkeyPatch, parametrize, Response (+20 more)

### Community 63 - "Tenant Isolation Migrations"
Cohesion: 0.10
Nodes (10): enable_tenant_isolation(), Isolate a tenant-owned table by tenant. Call it in the migration that creates…, upgrade(), upgrade(), upgrade(), upgrade(), upgrade(), upgrade() (+2 more)

### Community 64 - "Rate Limits"
Cohesion: 0.11
Nodes (23): email_key(), hit(), ip_key(), timedelta, Count one attempt against each key; True if any key is now over its limit.…, The caller normalises the email first. Hashed, so the table holds no addresses., IPv4 per address, IPv6 per /64 (one household or server). Every address that…, action() (+15 more)

### Community 65 - "Members Backend"
Cohesion: 0.13
Nodes (25): change_role(), DisplayNameChange, DisplayNameOut, guarded(), list_members(), MemberOut, BaseModel, CurrentOwner (+17 more)

### Community 66 - "Booking Page Component"
Cohesion: 0.13
Nodes (20): BookingPage(), backToStep1(), book(), cacheKey(), changeWeek(), chooseWorker(), doBook(), ensureWeek() (+12 more)

### Community 67 - "Audit DB Tests"
Cohesion: 0.19
Nodes (23): events(), in_business(), Any, Connection, Engine, fixture, parametrize, UUID (+15 more)

### Community 68 - "Audit Events API"
Cohesion: 0.09
Nodes (22): AuditEventOut, list_events(), BaseModel, CurrentSession, ge, get, le, Query (+14 more)

### Community 69 - "Client IP Tests"
Cohesion: 0.24
Nodes (21): fresh_address(), A new IPv6 /64, so per-IP rate limits never carry over between tests or runs.…, app_trusting(), Engine, FastAPI, MonkeyPatch, parametrize, The visitor's address, as ZIF_TRUSTED_PROXIES and uvicorn's… (+13 more)

### Community 70 - "Parallel Test Safety"
Cohesion: 0.16
Nodes (19): That worker's own database, or the URL untouched when there is no worker. The…, worker_url(), blocked_elsewhere(), blocked_on(), elsewhere(), fence_row(), Engine, fixture (+11 more)

### Community 71 - "Invites Backend"
Cohesion: 0.14
Nodes (20): AcceptInvite, create(), InviteDetails, InviteOut, InviteToken, list_invites(), lookup(), NewInvite (+12 more)

### Community 72 - "OTel Tracing Setup"
Cohesion: 0.14
Nodes (19): configure(), _enabled(), _log_provider(), _provider(), LoggerProvider, The gate: an endpoint (the signal-specific one, or the shared one) non-empty…, Pure, so tests call it directly with a monkeypatched environment., Pure. None unless the logs signal is on: no processor, no atexit hook, no slow… (+11 more)

### Community 73 - "Keep-An-Owner Tests"
Cohesion: 0.17
Nodes (18): add_user(), UUID, set_role(), Engine, parametrize, The keep_an_owner trigger, exercised directly (not through the API): a business…, test_a_non_read_committed_change_is_refused(), test_changing_the_only_owner_is_refused() (+10 more)

### Community 74 - "Frontend Proxy Tracing"
Cohesion: 0.14
Nodes (18): RFC-7230, enabled(), endProxySpan(), provider(), startProxySpan(), traceparent(), config, forwardApi() (+10 more)

### Community 75 - "TS Compiler Config"
Cohesion: 0.10
Nodes (19): compilerOptions, allowImportingTsExtensions, allowJs, esModuleInterop, incremental, isolatedModules, jsx, lib (+11 more)

### Community 76 - "Account Recovery Backend"
Cohesion: 0.15
Nodes (19): complete_password_reset(), CompleteReset, CompleteSignUp, give_name(), LinkRequest, NameIn, AnySession, BaseModel (+11 more)

### Community 77 - "Jobs & Sessions Migrations"
Cohesion: 0.12
Nodes (3): Password credentials, email tokens, and the functions that are the app's only…, upgrade(), sqlalchemy_dialects_postgresql

### Community 78 - "Locale Layout"
Cohesion: 0.15
Nodes (9): body, display, frontend_app_tokens, AppConfig, next-intl, routing, frontend_messages_en, nextConfig (+1 more)

### Community 80 - "Booking Link Helpers"
Cohesion: 0.20
Nodes (13): addDays(), answerScreen(), CONFLICTS, COPY, CopyKey, copyMessage(), firstStage(), localDay() (+5 more)

### Community 81 - "Day Shifts"
Cohesion: 0.15
Nodes (15): by_weekday(), day_shifts(), envelope(), date, datetime, Session, time, Whether a shift fits entirely inside one of a weekday's joined opening shifts. (+7 more)

### Community 82 - "Audit Events API Tests"
Cohesion: 0.28
Nodes (14): add_event(), app(), Engine, FastAPI, fixture, parametrize, UUID, An owner reads their business's audit log: newest first, in pages, without… (+6 more)

### Community 83 - "Landing Worker & OTLP Gate"
Cohesion: 0.19
Nodes (7): fetch(), pickLanguage(), redirect(), SUPPORTED, assets, ref_node_assert, ref_node_test

### Community 84 - "Proxy Tests"
Cohesion: 0.14
Nodes (5): parsedLogLines(), waitForLog(), ref_node_http, ref_node_net, ref_node_zlib

### Community 85 - "Booking Sweep"
Cohesion: 0.29
Nodes (14): Expire every tenant's due pendings; the worker's housekeeping, never…, sweep(), due(), events_of(), Any, datetime, UUID, seed() (+6 more)

### Community 86 - "Business Settings"
Cohesion: 0.15
Nodes (14): BusinessSettings, BaseModel, CurrentOwner, CurrentSession, get, put, Request, Response (+6 more)

### Community 87 - "Sessions Tests"
Cohesion: 0.36
Nodes (13): Engine, parametrize, UUID, start(), stored(), test_a_role_change_requires_ending_the_sessions_first(), test_a_session_cannot_start_for_someone_outside_the_tenant(), test_a_session_needs_the_membership_and_role_it_claims() (+5 more)

### Community 88 - "Booking Page Upstream Load"
Cohesion: 0.23
Nodes (11): frontend_api_client_types_gen, frontend_api_client_types_gen_bookingpageout, generateMetadata(), load, Loaded, PublicBookingPage(), slugLooksValid(), apiUrl() (+3 more)

### Community 89 - "Session Guards"
Cohesion: 0.18
Nodes (13): named(), owner(), CurrentSession, signed_in(), SignedIn, may_book_for(), members.may_manage for a member id: an owner books for anyone (an unknown id…, is_owner() (+5 more)

### Community 90 - "Availability Schemas"
Cohesion: 0.22
Nodes (13): AvailabilityOut, alias, BaseModel, CurrentSession, Day, get, Query, Request (+5 more)

### Community 91 - "Tenant Isolation Tests"
Cohesion: 0.23
Nodes (13): add_parent(), Base, Child, Parent, Engine, fixture, UUID, tenants() (+5 more)

### Community 92 - "Client Cancel Emails"
Cohesion: 0.17
Nodes (12): Client Self-Cancel Confirmation (to client), Booking Cancelled By Client Email (EN), Booking Cancelled By Client Email (NL), Booking Cancelled By Client Email (PT), Client-Initiated Cancellation Alert (to merchant), Client Cancelled Booking Alert Email (EN), Client Cancelled Booking Alert Email (NL), Client Cancelled Booking Alert Email (PT) (+4 more)

### Community 93 - "Invite & Reset Emails"
Cohesion: 0.26
Nodes (12): Business Invite Email (EN), Business Invite Email (NL), Business Invite Email (PT), Password Reset Email (EN), Password Reset Email (NL), Password Reset Email (PT), Sign Up Email (EN), Sign Up Email (NL) (+4 more)

### Community 94 - "OpenAPI Tests"
Cohesion: 0.27
Nodes (10): openapi_document(), Any, Schema names reachable, transitively, from these nodes (a parameter, a…, schemas_reachable_from(), test_committed_openapi_json_matches_the_app(), test_no_public_response_exposes_a_clients_private_field(), test_no_request_schema_accepts_a_user_id(), test_openapi_document_is_stable_json() (+2 more)

### Community 95 - "DB Roles Tests"
Cohesion: 0.29
Nodes (10): probe_table(), Engine, fixture, parametrize, test_app_role_cannot_change_the_schema(), test_app_role_gets_row_access_to_new_tables(), test_app_role_has_no_elevated_attributes(), test_app_role_owns_nothing() (+2 more)

### Community 96 - "Frontend Dependencies"
Cohesion: 0.18
Nodes (11): dependencies, next, next-intl, @opentelemetry/api, @opentelemetry/exporter-logs-otlp-http, @opentelemetry/exporter-trace-otlp-http, @opentelemetry/resources, @opentelemetry/sdk-logs (+3 more)

### Community 97 - "Tenant Schema Tests"
Cohesion: 0.36
Nodes (8): alembic_migration, alembic_operations, broken_tables(), Engine, fixture, test_schema_has_no_tenant_isolation_violations(), test_the_check_catches_plain_foreign_keys_and_unisolated_tables(), violations()

### Community 98 - "ICS Calendar Export"
Cohesion: 0.22
Nodes (6): ics(), datetime, A stdlib-only VCALENDAR/PUBLISH, stable UID per booking. See ZIF-53 SS3.3.…, test_ics_folds_by_octet_and_escapes_text(), test_ics_lines_and_stamps(), test_ics_strips_control_characters_from_summary()

### Community 99 - "Received & Reminder Emails"
Cohesion: 0.33
Nodes (9): Booking Received Email (EN), Booking Received Email (NL), Booking Received Email (PT), Booking Reminder Email (EN), Booking Reminder Email (NL), Booking Reminder Email (PT), Booking Request Email (EN), Booking Request Email (NL) (+1 more)

### Community 100 - "JSON-Only Tests"
Cohesion: 0.39
Nodes (8): client(), fixture, parametrize, TestClient, test_cross_origin_requests_are_never_allowed(), test_json_with_parameters_is_accepted(), test_safe_methods_need_no_content_type(), test_state_changing_requests_that_are_not_json_are_rejected()

### Community 101 - "Env Name Checks"
Cohesion: 0.25
Nodes (6): ref_node_child_process, ALLOWED, BUILD_ARGS, languageOf(), READERS, unprefixedNames()

### Community 102 - "Cancelled & Declined Emails"
Cohesion: 0.25
Nodes (8): Merchant-Cancelled Booking Notification (to client), Booking Cancelled Email (EN), Booking Cancelled Email (NL), Booking Cancelled Email (PT), Booking Request Declined Notification (to client), Booking Declined Email (EN), Booking Declined Email (NL), Booking Declined Email (PT)

### Community 103 - "Confirmed & New Booking Emails"
Cohesion: 0.25
Nodes (8): Booking Confirmed Notification (to client), Booking Confirmed Email (EN), Booking Confirmed Email (NL), Booking Confirmed Email (PT), New Booking Notification (to merchant), New Booking Notification Email (EN), New Booking Notification Email (NL), New Booking Notification Email (PT)

### Community 104 - "Frontend Dev Dependencies"
Cohesion: 0.25
Nodes (8): devDependencies, eslint, eslint-config-next, @hey-api/openapi-ts, @types/node, @types/react, @types/react-dom, typescript

### Community 105 - "Landing Package"
Cohesion: 0.25
Nodes (7): name, private, scripts, build, dev, test, type

### Community 106 - "Reserved Routes Check"
Cohesion: 0.39
Nodes (5): ref_node_fs, ref_node_path, reservedWords(), topLevelRoutes(), unreservedRoutes()

### Community 107 - "i18n Catalog Check"
Cohesion: 0.39
Nodes (6): blankBranches(), CATALOG_SETS, compareCatalogs(), kind(), placeholders(), en

### Community 108 - "Settings Test Helpers"
Cohesion: 0.33
Nodes (7): A saved value straight in the table, including one the registry would refuse…, save_setting(), _published(), ZIF-145: make_pending posts through the public booking route, gated on…, _published(), ZIF-145: post_booking creates every booking this file exercises through the…, _published()

### Community 109 - "Compose & Release Docs"
Cohesion: 0.29
Nodes (7): CHANGELOG, Smoke test compose stack, Release-please release process, Local dev compose stack, Production compose stack, CI workflow, version.txt 0.20.3

### Community 110 - "Env Doc Check"
Cohesion: 0.43
Nodes (5): documentedNames(), pydanticFieldNames(), SCAN_PATHS, SKIP, usedNames()

### Community 111 - "Bookings Migration"
Cohesion: 0.47
Nodes (4): languages(), quoted(), bookings and booking_events: a client holds one worker for one interval, and…, upgrade()

### Community 113 - "Landing Build"
Cohesion: 0.33
Nodes (4): alternates, LOCALES, strings, template

### Community 114 - "README Make Targets"
Cohesion: 0.40
Nodes (4): make migration name=..., make reset, make setup, make up

### Community 115 - "Renovate Config"
Cohesion: 0.40
Nodes (4): github>fjcloudaiconsulting/.github#v1, extends, packageRules, $schema

### Community 124 - "Fonts & Landing Template"
Cohesion: 0.50
Nodes (4): Young Serif OFL License (frontend), Young Serif OFL License (landing), Language switcher popover (i18n: en/nl/pt), landing/template.html (landing page template)

### Community 125 - "Booking PATCH Rules"
Cohesion: 0.67
Nodes (3): One PATCH target: where it may come from, whether the appointment must already…, Rule, NamedTuple

### Community 126 - "Decline & Move Emails"
Cohesion: 0.67
Nodes (3): Booking declined message email (en/nl/pt), Booking moved team email (en/nl/pt), Booking moved team member email (en/nl/pt)

### Community 127 - "Hello Test Email"
Cohesion: 1.00
Nodes (3): Hello Test Email (EN), Hello Test Email (NL), Hello Test Email (PT)

### Community 134 - "Client Cleanup Fixture"
Cohesion: 0.67
Nodes (3): delete_clients(), Connection, The app role can delete neither consents nor clients; fixtures remove them as…

## Knowledge Gaps
- **241 isolated node(s):** `Message`, `Props`, `Errors`, `Props`, `ServiceBodyResult` (+236 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 1129 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **72 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `People` connect `Clients API Tests` to `Accounts & Booking Page Backend`, `Public Bookings API Tests`, `Booking Links API Tests`, `Structured Logging & OTLP`, `Client Cleanup Fixture`, `Booking Approval Tests`, `Booking Email Rendering`, `Job Queue`, `Time Off & Access Log Tests`, `Mail Outbox Delivery`, `Merchant Booking Tests`, `DB Tracing & Test Fixtures`, `Availability API Tests`, `Clients & Consents`, `Members API Tests`, `Invites API Tests`, `Settings API Tests`, `Working Hours Tests`, `FastAPI App Factory`, `Service Workers Tests`, `Opening Hours Tests`, `Availability API Helpers`, `Bookings Range Tests`, `Invites DB Tests`, `Sign-Up Auth`, `Booking Page API Tests`, `Clients DB Tests`, `Password Auth DB Tests`, `Sign-Up Tests`, `Services Tests`, `Display Names Tests`, `Audit Recording Tests`, `Session API Tests`, `Sign-In Tests`, `Decline Message Tests`, `Migration Tests`, `Bookings DB Tests`, `Password Reset Tests`, `Rate Limits`, `Audit DB Tests`, `Keep-An-Owner Tests`, `Audit Events API Tests`, `Booking Sweep`, `Sessions Tests`, `Settings Test Helpers`?**
  _High betweenness centrality (0.176) - this node is a cross-community bridge._
- **Why does `new_client()` connect `Availability API Tests` to `Accounts & Booking Page Backend`, `Public Bookings API Tests`, `Booking Links API Tests`, `Structured Logging & OTLP`, `Booking Approval Tests`, `Job Queue`, `Booking Email Rendering`, `Time Off & Access Log Tests`, `Merchant Booking Tests`, `DB Tracing & Test Fixtures`, `Members API Tests`, `Invites API Tests`, `Settings API Tests`, `Working Hours Tests`, `FastAPI App Factory`, `Service Workers Tests`, `Opening Hours Tests`, `Availability API Helpers`, `Invites DB Tests`, `Sign-Up Auth`, `Booking Page API Tests`, `Sign-Up Tests`, `Services Tests`, `Display Names Tests`, `Audit Recording Tests`, `Clients API Tests`, `Sign-In Tests`, `Turnstile Verification`, `Password Reset Tests`, `Client IP Tests`, `Audit Events API Tests`?**
  _High betweenness centrality (0.027) - this node is a cross-community bridge._
- **Why does `signed_in()` connect `Members API Tests` to `Accounts & Booking Page Backend`, `Public Bookings API Tests`, `Booking Links API Tests`, `Structured Logging & OTLP`, `Booking Approval Tests`, `Job Queue`, `Booking Email Rendering`, `Time Off & Access Log Tests`, `Merchant Booking Tests`, `DB Tracing & Test Fixtures`, `Availability API Tests`, `Invites API Tests`, `Settings API Tests`, `Working Hours Tests`, `FastAPI App Factory`, `Service Workers Tests`, `Opening Hours Tests`, `Bookings Range Tests`, `Invites DB Tests`, `Sign-Up Auth`, `Booking Page API Tests`, `Services Tests`, `Display Names Tests`, `Audit Recording Tests`, `Clients API Tests`, `Session API Tests`, `Decline Message Tests`, `Keep-An-Owner Tests`, `Audit Events API Tests`?**
  _High betweenness centrality (0.020) - this node is a cross-community bridge._
- **Are the 760 inferred relationships involving `People` (e.g. with `test_a_failed_send_keeps_the_request_for_the_retry()` and `test_a_reset_link_uses_the_users_own_language()`) actually correct?**
  _`People` has 760 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Message`, `Props`, `Errors` to the rest of the system?**
  _241 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Accounts & Booking Page Backend` be split into smaller, more focused modules?**
  _Cohesion score 0.06692406692406692 - nodes in this community are weakly interconnected._
- **Should `Calendar API Client` be split into smaller, more focused modules?**
  _Cohesion score 0.04737281067556297 - nodes in this community are weakly interconnected._