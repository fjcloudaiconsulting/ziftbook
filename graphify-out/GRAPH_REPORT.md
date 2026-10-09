# Graph Report - ziftbook  (2026-10-09)

## Corpus Check
- 345 files · ~322,160 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 4632 nodes · 17334 edges · 255 communities (119 shown, 136 thin omitted)
- Extraction: 92% EXTRACTED · 8% INFERRED · 0% AMBIGUOUS · INFERRED: 1304 edges (avg confidence: 0.93)
- Token cost: 314,885 input · 0 output

## Community Hubs (Navigation)
- Public Booking Page UI
- Service Workers Tests
- Bookings API Tests
- Console Calendar UI
- Availability API Tests
- Account Pages UI
- Time Off Backend
- Booking Approval Tests
- Time Off Tests
- Booking Links API Tests
- Console Shell & Clients UI
- Booking Email Job
- Merchant Booking Tests
- Bookings Backend
- Booking Holds Tests
- Accounts & Passwords
- Job Queue
- Auth & Sign-up Flows
- Booking Links Backend
- App Config & Turnstile
- Bookings DB Tests
- Services UI
- Week Hours Editor
- DB Tracing & Test Fixtures
- Invites Backend
- Account & Invite Emails
- Members API Tests
- Tracing Tests
- Structured Logging Tests
- Guest Verification & Landing
- Clients & Consents
- Schedule Shifts
- Working Hours Tests
- Keep-an-Owner & Tenant Context
- Availability Backend
- Availability Unit Tests
- Mail Rendering & ICS
- Invites API Tests
- Sign-up Tests
- Settings API & Audit Tests
- App Factory & OpenAPI
- Booking Page API Tests
- Bookings Range Tests
- Password Auth DB Tests
- New Booking & Block Forms
- Team UI
- Booking Holds Backend
- Settings UI
- Cancellation Email Templates
- Opening Hours Tests
- Cancellation Rules
- Sign-in Tests
- Display Name Tests
- Tenant Isolation Migrations
- Clients API Tests
- Migration Tests
- Password Reset Tests
- Session API Tests
- Booking Notice Templates
- Locale Layout & Header
- Services Backend
- Tenants Tests
- Decline Message Tests
- Time Off UI
- Mailgun Delivery
- Worker & Mail Settings
- Early Auth Migrations
- Frontend Instrumentation
- Body Cap Tests
- Invites DB Tests
- Seed Script
- Audit DB Tests
- Client IP Tests
- Frontend Proxy & Trace
- OTLP Tracing Setup
- Booking Sweep
- Seed API Client
- Parallel Test Safety
- Frontend Lint Config
- Upstream Body Limit
- Frontend Log Gate Tests
- Frontend TS Config
- Log Configuration
- Seed Tests
- Private Log Tests
- Tenant Isolation Tests
- Session Tests
- Tenant Schema Rules
- Changelog Features
- Log Formatter
- Audit Events API Tests
- CI & Commit Hooks
- Day Span Helpers
- Seed Data Models
- Seed HTTP Stub
- Append-only Audit Tables
- Client IP & Settings Docs
- Audit Events API
- Availability Types
- Bookings Migration Hooks
- Health Checks
- Database Roles Tests
- CI Test Pipeline
- Release 0.23.0
- Booked Slots & Holds Docs
- Observability Stack Docs
- JSON-only API Tests
- Log Privacy Tests
- Frontend Dependencies
- Seed Mailbox
- Frontend Dev Dependencies
- Frontend Scripts
- Landing Package
- Catalog Checks
- Migration Discipline
- Landing Worker
- Env Name Checks
- Env Doc Checks
- Commit-msg Hook Test
- Landing Build
- Access Logging
- Landing Deploy
- Services Migration
- Slot Search Helper
- App Icon
- Landing Favicon
- Smoke Test Script
- Port Check Script
- Isolated: Translation catalogs (en sourc
- Isolated: ziftbook-api

## God Nodes (most connected - your core abstractions)
1. `People` - 877 edges
2. `tenant_context()` - 338 edges
3. `signed_in()` - 327 edges
4. `new_client()` - 291 edges
5. `member_id()` - 260 edges
6. `fresh_email()` - 165 edges
7. `at()` - 152 edges
8. `create_app()` - 131 edges
9. `patch()` - 106 edges
10. `events()` - 95 edges

## Surprising Connections (you probably didn't know these)
- `Tenant context set per transaction (tenant_context)` --references--> `tenant_context()`  [INFERRED]
  CHANGELOG.md → backend/app/db.py
- `Production compose stack` --references--> `TurnstileSettings`  [INFERRED]
  docker-compose-prod.yaml → backend/app/config.py
- `Email through a tenant-isolated outbox` --references--> `_outbox()`  [INFERRED]
  CHANGELOG.md → backend/app/mail.py
- `CI job: post-release smoke` --references--> `healthz()`  [INFERRED]
  .github/workflows/ci.yml → backend/app/main.py
- `Jobs table with idempotent enqueue, backoff, timeouts and staleness skips` --conceptually_related_to--> `main()`  [INFERRED]
  CHANGELOG.md → backend/app/worker.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Booking write-path exclusivity and locking** — contributing_ex_bookings_worker_overlap, contributing_tenant_advisory_lock_51, contributing_booking_holds, contributing_member_slots, contributing_bookings_sweep, contributing_lock_timeout [EXTRACTED 1.00]
- **Append-only tables enforced by column INSERT grants** — contributing_audit_events, contributing_consents_table, contributing_booking_events [EXTRACTED 1.00]
- **Privacy-safe vendor-neutral telemetry** — contributing_logging, contributing_tracing, contributing_observability_otlp, backend_tests_test_logs_private [INFERRED 0.85]
- **Main-branch release pipeline: build sha images, release-please tag, promote, smoke** — github_workflows_ci_image, github_workflows_ci_release, github_workflows_ci_promote, github_workflows_ci_smoke, compose_smoke [EXTRACTED 1.00]
- **Compose stacks bootstrap DB roles via bootstrap.sql before migrations** — docker_compose_db_init, compose_smoke_db_init, docker_compose_prod_migrate, backend_migrations_bootstrap, github_workflows_ci_api [INFERRED 0.85]
- **Reusable workflows from fjcloudaiconsulting/.github@v1** — github_workflows_ci_shared_build_image, github_workflows_ci_shared_promote_release, github_workflows_ci_shared_smoke, github_workflows_pr_title_shared_pr_title, ref_github_fjcloudaiconsulting_github_v1 [EXTRACTED 1.00]
- **Client-facing booking notification emails** — backend_app_mail_templates_booking_cancelled_template, backend_app_mail_templates_booking_cancelled_by_client_template, backend_app_mail_templates_booking_confirmed_template, backend_app_mail_templates_booking_declined_template, backend_app_mail_templates_booking_declined_message_template [INFERRED 0.85]
- **Business-facing client action notifications with link** — backend_app_mail_templates_booking_client_cancelled_template, backend_app_mail_templates_booking_client_rescheduled_template, backend_app_mail_templates_client, backend_app_mail_templates_link [INFERRED 0.85]
- **Booking request decline emails** — backend_app_mail_templates_booking_declined_template, backend_app_mail_templates_booking_declined_message_template, backend_app_mail_templates_decline_message [INFERRED 0.85]
- **Booking request approval flow emails** — mail_template_booking_request, mail_template_booking_request_team_member, mail_template_booking_received, booking_approval_expiry [INFERRED 0.85]
- **Guest-facing booking emails** — mail_template_booking_received, mail_template_booking_reminder, guest_self_service_booking_link [INFERRED 0.85]
- **Business/team-facing booking notifications** — mail_template_booking_moved_team, mail_template_booking_moved_team_member, mail_template_booking_new, mail_template_booking_request, mail_template_booking_request_team_member [INFERRED 0.85]
- **Emailed single-use link flows proving address ownership** — backend_app_mail_templates_booking_verify_en_template, backend_app_mail_templates_invite_en_template, backend_app_mail_templates_password_reset_en_template, backend_app_mail_templates_sign_up_en_template, backend_app_mail_templates_invite_en_single_use_link [INFERRED 0.85]
- **Guest booking verification flow (ZIF-117)** — backend_app_mail_templates_booking_verify_en_guest_email_verification, backend_app_mail_templates_booking_verify_en_slot_hold, backend_app_mail_templates_booking_verify_en_unverified_address_deletion, backend_app_mail_templates_booking_verify_en_template [EXTRACTED 1.00]
- **Self-hosted fonts shipped with OFL notices in frontend and landing** — frontend_licenses_schibsted_grotesk_ofl_license, frontend_licenses_young_serif_ofl_license, landing_licenses_schibsted_grotesk_ofl_license, landing_licenses_young_serif_ofl_license, frontend_licenses_young_serif_ofl_sil_open_font_license [EXTRACTED 1.00]

## Communities (255 total, 136 thin omitted)

### Community 0 - "Public Booking Page UI"
Cohesion: 0.05
Nodes (109): Confirm(), book(), read(), ConfirmBooking(), Page(), store, View, ConfirmBookingPage() (+101 more)

### Community 1 - "Service Workers Tests"
Cohesion: 0.06
Nodes (85): events(), failing(), app(), businesses(), client_at(), complete_reset(), complete_sign_up(), sessions_of() (+77 more)

### Community 2 - "Bookings API Tests"
Cohesion: 0.07
Nodes (86): delete_bookings(), new_client(), put_settings(), app(), at(), book_twice_under_auto_confirm(), booking_url(), commit_bypassing_the_lock() (+78 more)

### Community 3 - "Console Calendar UI"
Cohesion: 0.05
Nodes (99): BookingDetail(), actions(), expiryLine(), historyLine(), keepBooking(), keepDecline(), openCancel(), openDecline() (+91 more)

### Community 4 - "Availability API Tests"
Cohesion: 0.07
Nodes (69): app(), assign(), clear_time_off(), get(), insert_day_block(), local(), local_dt(), new_service() (+61 more)

### Community 5 - "Account Pages UI"
Cohesion: 0.06
Nodes (66): ForgotPasswordPage(), AcceptInvite(), Invite(), onSubmit(), Message, Stage, useInviteLink(), AcceptInvitePage() (+58 more)

### Community 6 - "Time Off Backend"
Cohesion: 0.05
Nodes (37): named(), owner(), SignedIn, may_book_for(), ApiError, change_role(), DisplayNameChange, DisplayNameOut (+29 more)

### Community 7 - "Booking Approval Tests"
Cohesion: 0.09
Nodes (60): ago(), app(), confirmed_in_the_past(), events_of(), expire(), expire_in_a_second(), hold_the_tenant_lock(), make_pending() (+52 more)

### Community 8 - "Time Off Tests"
Cohesion: 0.09
Nodes (67): app(), block(), block_path(), day_block(), google(), insert_block(), insert_day_block(), iso() (+59 more)

### Community 9 - "Booking Links API Tests"
Cohesion: 0.12
Nodes (61): app(), booking_jobs(), cancel_body(), confirmed_booking(), counts(), email_of_user(), get(), linked() (+53 more)

### Community 10 - "Console Shell & Clients UI"
Cohesion: 0.06
Nodes (57): ClientsPage(), ConsoleLayout(), MyHours(), MyHoursPage(), MyHoursTimeOffPage(), Clients(), PersonPage(), PersonTimeOffPage() (+49 more)

### Community 11 - "Booking Email Job"
Cohesion: 0.09
Nodes (57): send_booking(), add_worker(), app(), clean_outbox(), clear_jobs(), _client_email(), client_id_of(), email_of() (+49 more)

### Community 12 - "Merchant Booking Tests"
Cohesion: 0.13
Nodes (58): People, ana(), ana_id(), app(), availability(), book(), boss(), clean_outbox() (+50 more)

### Community 13 - "Bookings Backend"
Cohesion: 0.05
Nodes (30): Account, actor_of(), agenda(), AgendaOut, book(), book_online(), BookingDetailOut, BookingIn (+22 more)

### Community 14 - "Booking Holds Tests"
Cohesion: 0.10
Nodes (46): save_setting(), sent_to(), token_in(), seed_booking(), age(), app(), as_operator(), confirm() (+38 more)

### Community 15 - "Accounts & Passwords"
Cohesion: 0.07
Nodes (21): named(), printable(), Rule, CountryDefaults, Error, InTheWay, hash_password(), normalise_email() (+13 more)

### Community 16 - "Job Queue"
Cohesion: 0.06
Nodes (28): _claim(), enqueue(), JobKind, _record(), _run(), run_once(), work(), run_until_idle() (+20 more)

### Community 17 - "Auth & Sign-up Flows"
Cohesion: 0.07
Nodes (29): complete_password_reset(), complete_sign_up(), CompleteReset, CompleteSignUp, give_name(), LinkRequest, live_token(), NameIn (+21 more)

### Community 18 - "Booking Links Backend"
Cohesion: 0.07
Nodes (27): AvailabilityOut, BookingOut, cancel(), CancelIn, confirm_consents(), ConsentsIn, _decide(), EngineOut (+19 more)

### Community 19 - "App Config & Turnstile"
Cohesion: 0.06
Nodes (26): DatabaseSettings, LogSettings, Settings, TurnstileSettings, _causes(), _chain(), error_summary(), _escape() (+18 more)

### Community 20 - "Bookings DB Tests"
Cohesion: 0.10
Nodes (39): fresh_email(), book(), load_migration(), seed_service(), statuses_in(), test_a_booking_cannot_point_at_another_businesses_client_worker_or_service(), test_a_cancelled_booking_frees_its_slot(), test_a_pending_booking_must_carry_an_expiry_and_an_expired_one_keeps_it() (+31 more)

### Community 21 - "Services UI"
Cohesion: 0.09
Nodes (47): Summary(), NewServicePage(), ServicesPage(), EditServicePage(), ArchivedRow(), ArchiveZone(), BackLink(), EditService() (+39 more)

### Community 22 - "Week Hours Editor"
Cohesion: 0.09
Nodes (51): HoursSection(), load(), reloadEnvelope(), HoursSectionProps, OpeningHours(), load(), OpeningHoursPage(), ApiShift (+43 more)

### Community 23 - "DB Tracing & Test Fixtures"
Cohesion: 0.05
Nodes (15): iso_text(), _after_cursor_execute(), _before_cursor_execute(), _handle_error(), bound(), delete_clients(), _keep_pytest_thread_hook(), _mailgun_post() (+7 more)

### Community 24 - "Invites Backend"
Cohesion: 0.07
Nodes (24): accept(), AcceptInvite, create(), find(), Found, InviteDetails, InviteOut, InviteToken (+16 more)

### Community 25 - "Account & Invite Emails"
Cohesion: 0.12
Nodes (32): Job, send_invite(), send_token(), bound(), inbox(), link_in(), message(), request() (+24 more)

### Community 26 - "Members API Tests"
Cohesion: 0.12
Nodes (39): member_id(), set_role(), app(), seed_booking_for(), stored_name(), test_a_change_whose_event_fails_leaves_nothing_changed(), test_a_display_name_must_be_one_to_sixty_printable_characters(), test_a_malformed_role_change_is_refused() (+31 more)

### Community 27 - "Tracing Tests"
Cohesion: 0.08
Nodes (27): _add_context(), span(), _attrs(), _MetricsReceiver, _one(), _points(), _readers(), test_a_request_exports_one_allowlisted_server_span_and_no_query() (+19 more)

### Community 28 - "Structured Logging Tests"
Cohesion: 0.07
Nodes (31): bound(), _boom(), _raised(), raw_request(), _raw_request(), _run_uncaught_script(), test_a_json_record_has_exactly_the_expected_keys_in_order(), test_a_non_string_message_logs_the_class_not_str() (+23 more)

### Community 29 - "Guest Verification & Landing"
Cohesion: 0.09
Nodes (45): Guest email verification before booking, Booking verification email (English), $-placeholder template variables (string.Template style), Booking verification email (Dutch), Booking verification email (Portuguese), Mail delivery check, Hello test email (English), Email localization (en, nl, pt) (+37 more)

### Community 30 - "Clients & Consents"
Cohesion: 0.07
Nodes (18): add_consents(), as_clients(), ClientIn, ClientOut, ConsentIn, ConsentOut, create_client(), current_consents() (+10 more)

### Community 31 - "Schedule Shifts"
Cohesion: 0.08
Nodes (16): by_weekday(), day_shifts(), envelope(), lock_week(), opening_week(), overlapping(), read_opening_hours(), read_week() (+8 more)

### Community 32 - "Working Hours Tests"
Cohesion: 0.11
Nodes (33): app(), hours_path(), shift(), stored(), test_a_failed_recording_leaves_the_week_unchanged(), test_a_malformed_body_is_refused(), test_a_member_outside_the_business_is_not_found(), test_a_non_uuid_path_is_refused() (+25 more)

### Community 33 - "Keep-an-Owner & Tenant Context"
Cohesion: 0.07
Nodes (31): tenant_context(), add_membership(), add_user(), holder(), test_a_booking_with_no_email_creates_a_new_client_every_time(), test_find_or_create_survives_two_bookings_of_the_same_address_at_once(), thread_a(), thread_b() (+23 more)

### Community 34 - "Availability Backend"
Cohesion: 0.09
Nodes (21): anchor(), AvailabilityOut, booked(), buffer_for(), candidates(), clip(), compute(), member_slots() (+13 more)

### Community 35 - "Availability Unit Tests"
Cohesion: 0.14
Nodes (38): at(), local(), rows(), slots(), test_a_booking_blocks_its_own_override_buffer(), test_a_booking_without_an_override_blocks_a_percentage_of_its_length(), test_a_lunch_closure_splits_the_day_and_sells_nothing_in_the_gap(), test_a_new_slot_needs_its_own_buffer_before_the_next_booking() (+30 more)

### Community 36 - "Mail Rendering & ICS"
Cohesion: 0.06
Nodes (20): ics(), _local_text(), MailNotConfigured, _mark_sent(), _outbox(), _quoted(), render(), send() (+12 more)

### Community 37 - "Invites API Tests"
Cohesion: 0.16
Nodes (32): add_password(), app(), invite_jobs(), invite_row_count(), mint(), spy(), test_a_failed_audit_write_leaves_no_invite_or_job(), test_a_failed_sign_in_leaves_the_invite_unused_and_no_account() (+24 more)

### Community 38 - "Sign-up Tests"
Cohesion: 0.17
Nodes (28): issue_link(), jobs_for(), live(), app(), businesses(), client(), complete(), created() (+20 more)

### Community 39 - "Settings API & Audit Tests"
Cohesion: 0.13
Nodes (25): saved_settings(), signed_in(), app(), test_a_change_leaves_the_other_settings_as_they_were(), test_a_saved_setting_can_be_changed_and_set_back_to_its_default(), test_a_setting_of_the_wrong_kind_is_refused(), test_a_stored_setting_no_longer_in_the_registry_is_ignored(), test_a_stored_setting_that_no_longer_fits_fails_loudly() (+17 more)

### Community 40 - "App Factory & OpenAPI"
Cohesion: 0.07
Nodes (24): create_app(), openapi_document(), operation_id(), test_the_api_is_bound_to_its_database_while_it_runs(), test_the_contract_builds_without_a_database_url(), test_the_worker_still_requires_a_database_url(), test_the_contract_declares_413_on_every_write_route_and_no_get_route(), test_importing_the_engine_drags_in_no_other_app_module() (+16 more)

### Community 41 - "Booking Page API Tests"
Cohesion: 0.14
Nodes (29): add_worker(), app(), hits(), owner(), page(), _published(), slug(), slug_of() (+21 more)

### Community 42 - "Bookings Range Tests"
Cohesion: 0.16
Nodes (30): app(), history(), hour(), ids(), insert_event(), iso(), owner(), put() (+22 more)

### Community 43 - "Password Auth DB Tests"
Cohesion: 0.14
Nodes (30): delete_services(), complete_sign_up(), Created, digest(), email_of(), issue(), test_a_link_is_live_only_unexpired_and_for_its_purpose(), test_a_password_reset_ends_every_session_and_every_other_reset_link() (+22 more)

### Community 44 - "New Booking & Block Forms"
Cohesion: 0.14
Nodes (33): BlockPanel(), BookingForm(), Done, Fields(), clearTime(), submit(), Props, PanelFrame() (+25 more)

### Community 45 - "Team UI"
Cohesion: 0.10
Nodes (34): act(), read(), bringBack(), archive(), load(), load(), onBroughtBack(), onTogglePublish() (+26 more)

### Community 46 - "Booking Holds Backend"
Cohesion: 0.10
Nodes (18): WorkerOut, BookingPageOut, CancellationOut, PublicServiceOut, read(), current_policy_version(), join_tenant(), confirm() (+10 more)

### Community 47 - "Settings UI"
Cohesion: 0.13
Nodes (32): SettingsPage(), Field(), Settings(), onSubmit(), undo(), FooterPortal(), Chevron(), SaveBar() (+24 more)

### Community 48 - "Cancellation Email Templates"
Cohesion: 0.12
Nodes (35): booking_cancelled_by_client.en.txt (English), booking_cancelled_by_client.nl.txt (Dutch), booking_cancelled_by_client.pt.txt (Portuguese), Booking Cancelled by Client confirmation email (to client), booking_cancelled.en.txt (English), booking_cancelled.nl.txt (Dutch), booking_cancelled.pt.txt (Portuguese), Booking Cancelled by Business email (to client) (+27 more)

### Community 49 - "Opening Hours Tests"
Cohesion: 0.12
Nodes (24): _another_owner_replaces(), app(), seed_opening(), stored_opening(), test_a_change_to_the_week_is_recorded_once_with_no_details(), test_a_malformed_body_is_refused(), test_a_saved_week_is_read_back(), test_a_shift_spanning_two_touching_opening_rows_is_accepted() (+16 more)

### Community 50 - "Cancellation Rules"
Cohesion: 0.13
Nodes (20): decide(), Decision, Policy, test_a_booking_an_hour_away_across_the_fold_can_still_be_cancelled(), test_a_booking_moved_out_after_its_original_start_passed_can_still_be_cancelled(), test_a_booking_under_way_across_the_fold_has_started(), test_a_naive_datetime_is_refused(), test_a_negative_max_reschedules_is_refused() (+12 more)

### Community 51 - "Sign-in Tests"
Cohesion: 0.14
Nodes (22): email_of(), app(), client(), sign_in(), stranger(), test_a_malformed_request_gets_a_code_not_a_description(), test_a_member_of_several_businesses_lands_in_the_oldest_membership(), test_a_password_is_matched_exactly_up_to_unicode_composition() (+14 more)

### Community 52 - "Display Name Tests"
Cohesion: 0.17
Nodes (28): app(), display_name(), invite_token(), routes_behind_signed_in(), uses(), set_user_name(), test_a_blank_name_does_not_use_up_the_invite(), test_a_nameless_account_joining_another_business_stays_nameless() (+20 more)

### Community 53 - "Tenant Isolation Migrations"
Cohesion: 0.09
Nodes (11): enable_tenant_isolation(), upgrade(), upgrade(), upgrade(), upgrade(), upgrade(), upgrade(), upgrade() (+3 more)

### Community 54 - "Clients API Tests"
Cohesion: 0.14
Nodes (23): add_client(), app(), test_a_client_answer_carries_the_current_consent_per_purpose(), test_a_client_of_another_business_is_not_found(), test_a_consent_call_must_name_at_least_one_purpose(), test_a_merchant_consent_stores_the_connection_address_not_a_forwarded_header(), test_a_non_uuid_client_id_is_refused(), test_a_search_for_a_wildcard_matches_it_literally() (+15 more)

### Community 55 - "Migration Tests"
Cohesion: 0.13
Nodes (21): insert_booking(), booked(), downgrade_outcome(), lock_timeout_config(), plain_insert(), test_0027_refuses_to_backfill_an_existing_booking(), test_0028_does_not_backfill_existing_partial_blocks(), test_0028_downgrade_refuses_while_a_whole_day_row_exists() (+13 more)

### Community 56 - "Password Reset Tests"
Cohesion: 0.14
Nodes (25): app_engine(), mailed(), run_until_idle(), app(), client(), complete(), request_reset(), sessions_of() (+17 more)

### Community 57 - "Session API Tests"
Cohesion: 0.16
Nodes (23): age(), app(), cleared(), client(), deferred_table(), last_seen(), sign_in(), test_a_cross_site_form_cannot_sign_anyone_out() (+15 more)

### Community 59 - "Booking Notice Templates"
Cohesion: 0.17
Nodes (31): booking_moved_team.en.txt (English: "Booking moved"), booking_moved_team_member.en.txt (English: "Booking moved"), booking_moved_team_member.nl.txt (Dutch: "Boeking verzet"), booking_moved_team_member.pt.txt (Portuguese: "Agendamento remarcado"), booking_moved_team.nl.txt (Dutch: "Boeking verzet"), booking_moved_team.pt.txt (Portuguese: "Agendamento remarcado"), booking_new.en.txt (English: "New booking"), booking_new.nl.txt (Dutch: "Nieuwe boeking") (+23 more)

### Community 60 - "Locale Layout & Header"
Cohesion: 0.09
Nodes (21): body, display, generateMetadata(), load, Loaded, PublicBookingPage(), Flag(), FLAGS (+13 more)

### Community 61 - "Services Backend"
Cohesion: 0.10
Nodes (12): assign_workers(), create_service(), found(), list_services(), lock_members(), PriceIn, read_service(), replace_workers() (+4 more)

### Community 62 - "Tenants Tests"
Cohesion: 0.15
Nodes (25): concurrent(), b(), drop_tenants(), routes(), slug_of(), tag(), test_a_business_country_must_be_two_uppercase_letters(), test_a_business_currency_must_be_three_uppercase_letters() (+17 more)

### Community 63 - "Decline Message Tests"
Cohesion: 0.20
Nodes (21): app(), body_of(), clean_outbox(), decline(), message_of(), owner(), _published(), test_a_bad_message_is_refused_and_nothing_is_written() (+13 more)

### Community 64 - "Time Off UI"
Cohesion: 0.15
Nodes (28): blockMeta(), blockTitle(), dayLabel(), blank(), BlockedTime(), load(), BlockedTimeForm(), onRemove() (+20 more)

### Community 65 - "Mailgun Delivery"
Cohesion: 0.12
Nodes (17): deliver(), clean_outbox(), email_events(), send_hello(), run_until_idle(), subjects_sent_to(), test_a_dropped_connection_is_not_a_sent_email(), test_a_failed_email_logs_the_error_class_and_the_job_retries() (+9 more)

### Community 66 - "Worker & Mail Settings"
Cohesion: 0.12
Nodes (14): MailSettings, WorkerSettings, transport(), main(), ping(), serve(), test_a_real_value_still_overrides_the_default(), test_empty_env_var_falls_back_to_the_default() (+6 more)

### Community 67 - "Early Auth Migrations"
Cohesion: 0.11
Nodes (8): upgrade(), upgrade(), upgrade(), upgrade(), upgrade(), upgrade(), upgrade(), upgrade()

### Community 68 - "Frontend Instrumentation"
Cohesion: 0.14
Nodes (21): onRequestError(), register(), requestLog, startupLog, ErrorContext, errorLink(), ErrorRequest, errorType() (+13 more)

### Community 69 - "Body Cap Tests"
Cohesion: 0.15
Nodes (14): app(), client(), limit(), padded(), test_a_body_of_exactly_the_cap_reaches_validation(), test_a_chunked_oversize_body_is_a_413(), chunks(), test_a_declared_oversize_body_is_a_413_with_the_standard_headers() (+6 more)

### Community 70 - "Invites DB Tests"
Cohesion: 0.19
Nodes (15): call(), digest(), _insert(), minted(), new_accounts(), test_accept_binds_to_its_own_tenant(), test_accept_creates_an_account_and_a_membership(), test_accept_for_an_already_member_leaves_the_role_unchanged() (+7 more)

### Community 71 - "Seed Script"
Cohesion: 0.11
Nodes (8): bucket(), check_local(), LocalRedirects, main(), on_grid(), opener(), Plan, test_guard_accepts_only_local_urls()

### Community 72 - "Audit DB Tests"
Cohesion: 0.19
Nodes (13): events(), in_business(), record(), reviewed(), target(), test_a_business_sees_only_its_own_events(), test_a_reused_connection_records_an_event_for_no_business(), test_an_event_cannot_be_written_for_another_business() (+5 more)

### Community 73 - "Client IP Tests"
Cohesion: 0.24
Nodes (14): fresh_address(), app_trusting(), seen(), test_a_trusted_peers_forwarded_address_is_believed(), test_addresses_and_networks_together_are_accepted(), test_an_invalid_trusted_proxies_entry_stops_startup(), test_an_ipv6_forwarded_address_is_kept_whole(), test_an_unparseable_forwarded_value_is_stored_as_no_ip() (+6 more)

### Community 74 - "Frontend Proxy & Trace"
Cohesion: 0.14
Nodes (21): RFC-7230, errorFields(), requestId(), enabled(), endProxySpan(), provider(), startProxySpan(), traceparent() (+13 more)

### Community 75 - "OTLP Tracing Setup"
Cohesion: 0.14
Nodes (11): configure(), _enabled(), _meter_provider(), _provider(), _resource(), test_service_version_defaults_to_the_app_version(), test_t11_provider_names_the_service(), test_t11_worker_and_migrations_configure_their_own_service_name() (+3 more)

### Community 76 - "Booking Sweep"
Cohesion: 0.22
Nodes (13): sweep(), wait_until_blocked(), due(), events_of(), seed(), status_of(), test_a_due_awaiting_payment_expires_as_well(), test_a_live_pending_and_a_confirmed_booking_are_left_alone() (+5 more)

### Community 77 - "Seed API Client"
Cohesion: 0.23
Nodes (3): Api, email_of(), Seed

### Community 78 - "Parallel Test Safety"
Cohesion: 0.16
Nodes (9): worker_url(), blocked_elsewhere(), blocked_on(), elsewhere(), fence_row(), test_a_serial_run_targets_the_shared_database(), test_each_xdist_worker_targets_its_own_database(), test_wait_until_blocked_sees_a_waiter_in_this_database() (+1 more)

### Community 79 - "Frontend Lint Config"
Cohesion: 0.10
Nodes (16): eslintConfig, engines, node, name, packageManager, private, eslint, eslint-config-next (+8 more)

### Community 80 - "Upstream Body Limit"
Cohesion: 0.11
Nodes (5): LOCALE_MATCHER, MAX_BODY_BYTES, localeRegex, parsedLogLines(), waitForLog()

### Community 81 - "Frontend Log Gate Tests"
Cohesion: 0.16
Nodes (3): reservedWords(), topLevelRoutes(), unreservedRoutes()

### Community 82 - "Frontend TS Config"
Cohesion: 0.10
Nodes (19): compilerOptions, allowImportingTsExtensions, allowJs, esModuleInterop, incremental, isolatedModules, jsx, lib (+11 more)

### Community 83 - "Log Configuration"
Cohesion: 0.13
Nodes (12): configure(), _excepthook(), _thread_excepthook(), _unraisable_hook(), test_a_malformed_otlp_headers_entry_never_reaches_the_log(), test_an_invalid_log_setting_exits_cleanly_and_never_echoes_the_value(), test_configure_is_idempotent_and_polite_to_other_handlers(), test_configure_writes_nothing_and_python_dash_m_app_main_is_still_json() (+4 more)

### Community 84 - "Seed Tests"
Cohesion: 0.22
Nodes (10): edges(), local(), local_date(), nows(), past_ends(), test_bucket_checks_in_order(), test_near_role_is_ahead_but_under_a_day(), test_roles_hold_across_clock_changes() (+2 more)

### Community 85 - "Private Log Tests"
Cohesion: 0.18
Nodes (7): app(), clean_outbox(), client_for(), test_no_personal_data_ever_reaches_a_log(), cookie_secret(), secret(), token_secret()

### Community 86 - "Tenant Isolation Tests"
Cohesion: 0.25
Nodes (9): add_parent(), Base, Child, Parent, tenants(), test_a_query_without_tenant_context_raises(), test_a_tenant_cannot_read_another_tenants_rows(), test_a_tenant_cannot_reference_another_tenants_rows() (+1 more)

### Community 87 - "Session Tests"
Cohesion: 0.32
Nodes (10): start(), stored(), test_a_role_change_requires_ending_the_sessions_first(), test_a_session_cannot_start_for_someone_outside_the_tenant(), test_a_session_needs_the_membership_and_role_it_claims(), test_only_the_hash_of_a_random_token_is_stored(), test_removing_a_membership_ends_only_its_sessions(), test_starting_a_session_purges_expired_ones() (+2 more)

### Community 88 - "Tenant Schema Rules"
Cohesion: 0.17
Nodes (10): broken_tables(), test_schema_has_no_tenant_isolation_violations(), test_the_check_catches_plain_foreign_keys_and_unisolated_tables(), violations(), clients (business-owned client records), enable_tenant_isolation (app.db), keep_an_owner trigger, members.member_user(lock=True) (+2 more)

### Community 89 - "Changelog Features"
Cohesion: 0.20
Nodes (12): CHANGELOG, BREAKING: business country required at sign-up (ZIF-96), Traces from web app through API, database, jobs and email (ZIF-87), Jobs table with idempotent enqueue, backoff, timeouts and staleness skips, Committed OpenAPI contract with stable operation ids, Count rate limits in Postgres (ZIF-25), Rate limits and audit events see the visitor's address (ZIF-82), Tenant context set per transaction (tenant_context) (+4 more)

### Community 90 - "Log Formatter"
Cohesion: 0.13
Nodes (10): Formatter, stream_handler(), log_lines(), _fields(), logged(), test_migrations_log_json_lines(), test_text_format_escapes_every_control_character(), test_text_format_is_one_line_with_control_characters_escaped() (+2 more)

### Community 91 - "Audit Events API Tests"
Cohesion: 0.28
Nodes (8): add_event(), app(), test_a_page_holds_one_to_a_hundred_events(), test_an_owner_reads_only_their_business_newest_first(), test_an_owner_sees_the_address_and_browser_only_of_their_own_events(), test_only_an_owner_reads_the_log(), test_the_log_comes_in_pages_before_an_event(), test_the_log_needs_a_session()

### Community 92 - "CI & Commit Hooks"
Cohesion: 0.15
Nodes (11): CI job: promote release images, Shared workflow build-image.yml@v1, Shared workflow promote-release.yml@v1, Shared workflow smoke.yml@v1, CI job: post-release smoke, PR title workflow, Shared workflow pr-title.yml@v1, github>fjcloudaiconsulting/.github#v1 (+3 more)

### Community 93 - "Day Span Helpers"
Cohesion: 0.15
Nodes (9): day_span(), spans(), test_clip_cuts_a_day_s_shifts_to_that_day_s_envelope(), test_day_span_at_a_repeated_midnight_starts_at_the_first_occurrence(), test_day_span_at_a_skipped_midnight_starts_at_the_day_s_first_instant(), test_day_span_is_23_or_25_hours_around_a_clock_change(), test_day_span_is_not_a_bare_subtraction_of_two_aware_datetimes(), test_day_span_of_a_run_spans_all_of_it() (+1 more)

### Community 95 - "Seed HTTP Stub"
Cohesion: 0.23
Nodes (5): Counter, stub(), stubs(), test_requests_ignore_proxy_environment(), test_requests_refuse_redirects_off_the_machine()

### Community 96 - "Append-only Audit Tables"
Cohesion: 0.18
Nodes (12): audit_events audit log, auth.record, booking_events append-only table, app.clients.CONSENT_TEXTS, consents append-only table, JobKind(handler, timeout, grace), jobs table (app/jobs.py), join_tenant(session, tenant_id) (+4 more)

### Community 97 - "Client IP & Settings Docs"
Cohesion: 0.18
Nodes (10): BusinessSettings (app/business_settings.py), Production path Cloudflare -> Traefik -> web -> API (k3s), Conventional Commits PR titles, GHCR images backend/frontend/migrations, Shared release workflows (fjcloudaiconsulting/.github RELEASE_CONTRACT.md), release-please, Release PR (chore(main): release X.Y.Z), ZIF_CLIENT_IP_HEADER (+2 more)

### Community 100 - "Bookings Migration Hooks"
Cohesion: 0.20
Nodes (4): _set_tenant(), languages(), quoted(), upgrade()

### Community 101 - "Health Checks"
Cohesion: 0.20
Nodes (9): dependencies(), healthz(), Dependencies, Health, Smoke test compose stack, smoke db-init service (profile migrate), smoke migrations service (run twice, second a no-op), db-init service (bootstrap.sql role creation, idempotent) (+1 more)

### Community 102 - "Database Roles Tests"
Cohesion: 0.29
Nodes (7): probe_table(), test_app_role_cannot_change_the_schema(), test_app_role_gets_row_access_to_new_tables(), test_app_role_has_no_elevated_attributes(), test_app_role_owns_nothing(), test_btree_gist_is_installed(), test_migrate_role_owns_the_alembic_version_table()

### Community 103 - "CI Test Pipeline"
Cohesion: 0.27
Nodes (9): Test suite runs in parallel with one database per worker (ZIF-111), frontend/pnpm-workspace.yaml (pnpm settings, no workspace packages), CI workflow, CI job: API (ruff, mypy, pytest against Postgres service), CI aggregate: Backend Checks, CI aggregate: Frontend Checks, CI job: image build matrix (backend, migrations, frontend) as sha-<7>, CI job: Landing (npm test) (+1 more)

### Community 104 - "Release 0.23.0"
Cohesion: 0.20
Nodes (9): Hold a guest's time until they confirm the emailed link (ZIF-117), Release 0.23.0 (2026-10-08), Cap request bodies at 64 KiB (ZIF-83), Stalled lock holder costs a 503, not a hang for every tenant (ZIF-114), Availability says which workers can take each slot (ZIF-100), CI job: Release PR and tag (release-please), packages, $schema (+1 more)

### Community 105 - "Booked Slots & Holds Docs"
Cohesion: 0.24
Nodes (9): app.availability.BOOKED / booked() / OCCUPYING, email.booking job (ZIF-53), booking_holds (guest held times), bookings.sweep worker expiry, ex_bookings_worker_overlap exclusion constraint, app.clients.find_or_create, API lock_timeout 5s (503 busy), Mailgun mail delivery (+1 more)

### Community 106 - "Observability Stack Docs"
Cohesion: 0.24
Nodes (9): Shared local Grafana LGTM stack, app.logs.configure() structured logging, app.tracing hand-written spans, Mailpit local mail, make observe, make reset, make seed synthetic data, make up local stack (+1 more)

### Community 107 - "JSON-only API Tests"
Cohesion: 0.39
Nodes (5): client(), test_cross_origin_requests_are_never_allowed(), test_json_with_parameters_is_accepted(), test_safe_methods_need_no_content_type(), test_state_changing_requests_that_are_not_json_are_rejected()

### Community 108 - "Log Privacy Tests"
Cohesion: 0.25
Nodes (5): test_a_database_error_never_quotes_the_row(), test_job_context_matches_its_own_job_and_a_failure_never_leaks(), fail(), test_job_events_have_their_level_and_fields(), by_id()

### Community 109 - "Frontend Dependencies"
Cohesion: 0.22
Nodes (9): dependencies, next, next-intl, @opentelemetry/api, @opentelemetry/exporter-trace-otlp-http, @opentelemetry/resources, @opentelemetry/sdk-trace-base, react (+1 more)

### Community 111 - "Frontend Dev Dependencies"
Cohesion: 0.25
Nodes (8): devDependencies, eslint, eslint-config-next, @hey-api/openapi-ts, @types/node, @types/react, @types/react-dom, typescript

### Community 112 - "Frontend Scripts"
Cohesion: 0.25
Nodes (8): scripts, build, dev, generate, lint, start, test, typecheck

### Community 113 - "Landing Package"
Cohesion: 0.25
Nodes (7): name, private, scripts, build, dev, test, type

### Community 114 - "Catalog Checks"
Cohesion: 0.39
Nodes (6): blankBranches(), CATALOG_SETS, compareCatalogs(), kind(), placeholders(), en

### Community 115 - "Migration Discipline"
Cohesion: 0.38
Nodes (6): Alembic single-head migration chain, SECURITY DEFINER credential functions, users global table, ziftbook_migrate role, migrations init container (alembic upgrade head), Parallel per-worker test databases

### Community 116 - "Landing Worker"
Cohesion: 0.43
Nodes (5): fetch(), pickLanguage(), redirect(), SUPPORTED, assets

### Community 117 - "Env Name Checks"
Cohesion: 0.33
Nodes (5): ALLOWED, BUILD_ARGS, languageOf(), READERS, unprefixedNames()

### Community 118 - "Env Doc Checks"
Cohesion: 0.43
Nodes (5): documentedNames(), pydanticFieldNames(), SCAN_PATHS, SKIP, usedNames()

### Community 119 - "Commit-msg Hook Test"
Cohesion: 0.40
Nodes (4): Web forwards /api to the API at runtime, check(), commit-msg.test.sh script, CI job: Repo checks

### Community 120 - "Landing Build"
Cohesion: 0.33
Nodes (4): alternates, LOCALES, strings, template

### Community 122 - "Landing Deploy"
Cohesion: 0.40
Nodes (4): Landing on Cloudflare; www redirect moves to zone rule (INFRA-131), Deploy landing page workflow, Landing deploy job (wrangler deploy to ziftbook-landing Worker), Smoke test the live site (HSTS and redirects)

### Community 129 - "App Icon"
Cohesion: 0.67
Nodes (3): Ziftbook App Icon (favicon), Brand Red #b31513, Z Logo Mark (white stroked Z glyph)

### Community 130 - "Landing Favicon"
Cohesion: 0.67
Nodes (3): Brand Color #b31513 (Ziftbook red), Landing Favicon (red rounded square with white Z), Z Monogram Logo Mark

## Ambiguous Edges - Review These
- `Sign out on every device after password reset` → `SecLists`  [AMBIGUOUS]
  backend/licenses/seclists-MIT.txt · relation: conceptually_related_to
- `Schibsted Grotesk font (landing)` → `Landing page HTML template`  [AMBIGUOUS]
  landing/template.html · relation: references

## Knowledge Gaps
- **40 isolated node(s):** `ziftbook-api`, `@types/node`, `@types/react`, `@types/react-dom`, `typescript` (+35 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 1209 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **136 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `app.tracing hand-written spans` connect `Observability Stack Docs` to `Append-only Audit Tables`, `Frontend Proxy & Trace`?**
  _High betweenness centrality (0.228) - this node is a cross-community bridge._
- **Are the 820 inferred relationships involving `People` (e.g. with `test_a_failed_send_keeps_the_request_for_the_retry()` and `test_a_reset_link_uses_the_users_own_language()`) actually correct?**
  _`People` has 820 INFERRED edges - model-reasoned connections that need verification._
- **What connects `ziftbook-api`, `@types/node`, `@types/react` to the rest of the system?**
  _40 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Public Booking Page UI` be split into smaller, more focused modules?**
  _Cohesion score 0.045806451612903226 - nodes in this community are weakly interconnected._
- **Why does `app.logs.configure() structured logging` connect `Observability Stack Docs` to `Private Log Tests`?**
  _High betweenness centrality (0.225) - this node is a cross-community bridge._

### Low-confidence Hints
_AMBIGUOUS edges — the extractor was unsure. Verify before acting on these._

- **What is the exact relationship between `Sign out on every device after password reset` and `SecLists`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `Schibsted Grotesk font (landing)` and `Landing page HTML template`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._