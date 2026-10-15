# Graph Report - ziftbook  (2026-10-15)

## Corpus Check
- 94 files · ~357,367 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 5182 nodes · 18732 edges · 241 communities (138 shown, 103 thin omitted)
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 1310 edges (avg confidence: 0.93)
- Token cost: 110,759 input · 0 output

## Community Hubs (Navigation)
- Calendar Agenda Client
- Service Workers Tests
- Database Engine And Sessions
- Bookings API Tests
- Admin Sign-in UI
- Team Console Client
- Availability API Tests
- Test Fixtures Conftest
- Password Hashing Core
- Booking Holds Client
- Time Off Tests
- Booking Links API Tests
- Booking Approval Tests
- Bookings Agenda Backend
- Booking Email Tests
- Merchant Booking Tests
- Job Runner Tests
- Log Output Tests
- Public Booking Page API
- Mail Delivery
- Week Editor UI
- Accept Invite UI
- Booking Holds Tests
- Services Console UI
- Pending Booking Jobs
- Admin Sessions And Auth
- Deployment Config
- Members API Tests
- Booking Links Backend
- Clients Backend
- App Shell And Header
- Second Factor DB Tests
- Working Hours Tests
- Invites API Tests
- Admin Second Factor Tests
- Console Calendar Logic
- Admin CLI
- TOTP Secret Encryption
- Time Off Backend
- Landing And Mail Templates
- Manage Booking Link UI
- Booking Page API Tests
- Bookings Range Tests
- Migration Tests
- Availability Engine
- Clients API Tests
- Sign In Tests
- Logging Backend
- First-run Setup Tests
- Sign Up Tests
- Opening Hours Tests
- Password Auth DB Tests
- Cancellation Mail Templates
- Session Auth Core
- Members Backend
- Turnstile Tests
- Bookings DB Tests
- Display Names Tests
- Cancellation Policy
- Services Backend
- Admin Sessions Tests
- Session Cookie API Tests
- Availability Slot Compute
- Schedule Backend
- Settings Page UI
- Tenant Tests
- Web Tracing
- Decline Message Tests
- Tracing Tests
- Booking Mail Templates
- Password Reset Tests
- Invites DB Tests
- FastAPI App Factory
- Settings API Tests
- Web Logging Instrumentation
- Body Size Cap Tests
- Account Lifecycle Backend
- Backend Tracing Setup
- Audit DB Tests
- Web Proxy Middleware
- Seed Bookings
- Parallel Safety Tests
- Booking Holds Backend
- Seed Script
- Client IP Tests
- Database Role Tests
- Frontend TS Config
- Seed Tests
- Seed API Client
- CI Smoke And Release
- API Proxy Upstream
- Booking Sweep Tests
- Settings Audit Tests
- First-run Setup
- Clients DB Tests
- Contributor Docs
- JSON Only Tests
- Tenant Schema Docs
- Audit Events API Tests
- Private Log Tests
- Session API Tests
- Tenant Isolation Tests
- Migration And Tenancy Rules
- Team And Today Console
- Env Doc Check Scripts
- Slot Anchors And Buffers
- Availability Queries
- Accounts Backend
- Worker Schedules
- Observability And Settings Docs
- Seed Test Helpers
- Audit Backend
- Docs Conventions
- Frontend Dependencies
- Blank Names Tests
- Renovate And Commit Hooks
- Mail Rendering
- Tracing Spans
- Admin Route Allowlist
- Seed Setup Stub
- Release 0.25.0 Docs
- Readme Setup Docs
- Frontend Build Deps
- Frontend Test Deps
- Landing Dependencies
- Repo Check Scripts
- DST Day Span
- Booking Setting Fixtures
- Landing Deploy And Client IP
- Project Docs
- Landing Worker
- Admin Sessions Migration
- Turnstile Booking Tests
- Landing Build
- Catalog And Hook Checks
- Community 112
- Reserve Setup Slug Migration
- Admin Second Factor Migration
- App Role Delete Fixtures
- CI Changed-Paths Script
- Community 117
- Dev Mail And Worker
- Community 118
- Commit-msg Hook Test
- Community 119
- Community 115
- Community 120
- Community 121
- Community 122
- Community 126
- Community 145
- Booking Verify Mail Template
- Community 164
- Community 202
- Prod Compose And API Main

## God Nodes (most connected - your core abstractions)
1. `People` - 886 edges
2. `tenant_context()` - 338 edges
3. `signed_in()` - 330 edges
4. `new_client()` - 320 edges
5. `member_id()` - 260 edges
6. `fresh_email()` - 167 edges
7. `at()` - 152 edges
8. `create_app()` - 144 edges
9. `patch()` - 109 edges
10. `email_of()` - 99 edges

## Surprising Connections (you probably didn't know these)
- `Shared workflow pr-title.yml@v1` --references--> `github>fjcloudaiconsulting/.github#v1`  [INFERRED]
  .github/workflows/pr-title.yml → renovate.json
- `Schibsted Grotesk OFL license (frontend)` --semantically_similar_to--> `Schibsted Grotesk OFL license (landing)`  [INFERRED] [semantically similar]
  frontend/licenses/schibsted-grotesk-OFL.txt → landing/licenses/schibsted-grotesk-OFL.txt
- `Young Serif OFL license (frontend)` --semantically_similar_to--> `Young Serif OFL license (landing)`  [INFERRED] [semantically similar]
  frontend/licenses/young-serif-OFL.txt → landing/licenses/young-serif-OFL.txt
- `Landing live-site smoke test` --semantically_similar_to--> `Post-release smoke compose stack`  [INFERRED] [semantically similar]
  .github/workflows/landing.yml → compose.smoke.yaml
- `CI Repo Checks job` --implements--> `Translation catalogs (en source, nl, pt)`  [INFERRED]
  .github/workflows/ci.yml → CONTRIBUTING.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Self-hosted fonts shipped with OFL notices in frontend and landing** — frontend_licenses_schibsted_grotesk_ofl_license, frontend_licenses_young_serif_ofl_license, landing_licenses_schibsted_grotesk_ofl_license, landing_licenses_young_serif_ofl_license, frontend_licenses_young_serif_ofl_sil_open_font_license [EXTRACTED 1.00]
- **Booking request decline emails** — backend_app_mail_templates_booking_declined_template, backend_app_mail_templates_booking_declined_message_template, backend_app_mail_templates_decline_message [INFERRED 0.85]
- **Booking request approval flow emails** — mail_template_booking_request, mail_template_booking_received, booking_approval_expiry [INFERRED 0.85]
- **Business-facing client action notifications with link** — backend_app_mail_templates_booking_client_cancelled_template, backend_app_mail_templates_booking_client_rescheduled_template, backend_app_mail_templates_client, backend_app_mail_templates_link [INFERRED 0.85]
- **Client-facing booking notification emails** — backend_app_mail_templates_booking_cancelled_template, backend_app_mail_templates_booking_cancelled_by_client_template, backend_app_mail_templates_booking_confirmed_template, backend_app_mail_templates_booking_declined_template, backend_app_mail_templates_booking_declined_message_template [INFERRED 0.85]
- **Guest-facing booking emails** — mail_template_booking_received, mail_template_booking_reminder, guest_self_service_booking_link [INFERRED 0.85]
- **Emailed single-use link flows proving address ownership** — backend_app_mail_templates_invite_en_template, backend_app_mail_templates_password_reset_en_template, backend_app_mail_templates_sign_up_en_template, backend_app_mail_templates_invite_en_single_use_link [INFERRED 0.85]
- **Business/team-facing booking notifications** — mail_template_booking_moved_team, mail_template_booking_moved_team_member, mail_template_booking_new, mail_template_booking_request [INFERRED 0.85]
- **Build-once, retag-on-release pipeline** — github_workflows_ci_backend_images_job, github_workflows_ci_frontend_image_job, github_workflows_ci_backend_checks, github_workflows_ci_frontend_checks, github_workflows_ci_release_job, compose_smoke_smoke_stack, contributing_release_process [INFERRED 0.85]
- **Booking concurrency safety (exclusion constraint, tenant lock, holds, lock timeout)** — contributing_ex_bookings_worker_overlap, contributing_tenant_advisory_lock, contributing_booking_holds, contributing_api_lock_timeout [EXTRACTED 1.00]
- **Role bootstrap shared by every compose stack and CI** — backend_migrations_bootstrap, docker_compose_dev_stack, docker_compose_prod_stack, compose_smoke_smoke_stack, github_workflows_ci_api_job [EXTRACTED 1.00]

## Communities (241 total, 103 thin omitted)

### Community 0 - "Calendar Agenda Client"
Cohesion: 0.04
Nodes (122): BookingDetail(), actions(), expiryLine(), historyLine(), keepBooking(), keepDecline(), openCancel(), openDecline() (+114 more)

### Community 1 - "Service Workers Tests"
Cohesion: 0.06
Nodes (86): events(), failing(), app(), businesses(), client_at(), complete_reset(), complete_sign_up(), sessions_of() (+78 more)

### Community 2 - "Database Engine And Sessions"
Cohesion: 0.02
Nodes (24): enable_tenant_isolation(), upgrade(), upgrade(), upgrade(), upgrade(), upgrade(), upgrade(), upgrade() (+16 more)

### Community 3 - "Bookings API Tests"
Cohesion: 0.07
Nodes (86): fresh_address(), new_client(), put_settings(), app(), at(), book_twice_under_auto_confirm(), booking_url(), commit_bypassing_the_lock() (+78 more)

### Community 4 - "Admin Sign-in UI"
Cohesion: 0.06
Nodes (80): AdminHome(), leave(), AdminPage(), CodeField(), CopyButton(), EnrolCommand(), SignedInAs(), AdminSignIn() (+72 more)

### Community 5 - "Team Console Client"
Cohesion: 0.04
Nodes (67): ConsoleLayout(), MyHours(), MyHoursPage(), MyHoursTimeOffPage(), PersonPage(), PersonTimeOffPage(), TeamPage(), BackLink() (+59 more)

### Community 6 - "Availability API Tests"
Cohesion: 0.08
Nodes (71): People, app(), assign(), clear_time_off(), get(), insert_day_block(), local(), local_dt() (+63 more)

### Community 7 - "Test Fixtures Conftest"
Cohesion: 0.04
Nodes (30): _after_cursor_execute(), _before_cursor_execute(), _handle_error(), app_engine(), bound(), empty_database(), empty_install(), install() (+22 more)

### Community 8 - "Password Hashing Core"
Cohesion: 0.07
Nodes (17): named(), printable(), CountryDefaults, join_tenant(), Error, InTheWay, check_new_password(), hash_password() (+9 more)

### Community 9 - "Booking Holds Client"
Cohesion: 0.05
Nodes (79): Confirm(), book(), read(), ConfirmBooking(), Page(), store, View, ConfirmBookingPage() (+71 more)

### Community 10 - "Time Off Tests"
Cohesion: 0.09
Nodes (67): app(), block(), block_path(), day_block(), google(), insert_block(), insert_day_block(), iso() (+59 more)

### Community 11 - "Booking Links API Tests"
Cohesion: 0.11
Nodes (60): app(), booking_jobs(), cancel_body(), confirmed_booking(), counts(), email_of_user(), first_slot_after(), get() (+52 more)

### Community 12 - "Booking Approval Tests"
Cohesion: 0.10
Nodes (58): ago(), app(), confirmed_in_the_past(), events_of(), expire(), expire_in_a_second(), hold_the_tenant_lock(), make_pending() (+50 more)

### Community 13 - "Bookings Agenda Backend"
Cohesion: 0.05
Nodes (34): Account, actor_of(), agenda(), AgendaOut, book(), book_online(), BookingDetailOut, BookingIn (+26 more)

### Community 14 - "Booking Email Tests"
Cohesion: 0.09
Nodes (59): tenant_context(), _mark_sent(), send_booking(), add_worker(), app(), clean_outbox(), _client_email(), client_id_of() (+51 more)

### Community 15 - "Merchant Booking Tests"
Cohesion: 0.11
Nodes (58): ana(), ana_id(), app(), availability(), book(), boss(), clean_outbox(), clear_jobs() (+50 more)

### Community 16 - "Job Runner Tests"
Cohesion: 0.05
Nodes (43): _connections(), _pending(), _pool(), _claim(), JobKind, queue(), _run(), run_once() (+35 more)

### Community 17 - "Log Output Tests"
Cohesion: 0.05
Nodes (46): _boom(), _fields(), _raised(), raw_request(), _raw_request(), _run_uncaught_script(), test_a_database_error_never_quotes_the_row(), test_a_json_record_has_exactly_the_expected_keys_in_order() (+38 more)

### Community 18 - "Public Booking Page API"
Cohesion: 0.04
Nodes (36): WorkerOut, BookingPageOut, CancellationOut, PublicServiceOut, read(), _count(), count_after_commit(), _forget() (+28 more)

### Community 19 - "Mail Delivery"
Cohesion: 0.05
Nodes (35): MailSettings, deliver(), _local_text(), MailNotConfigured, _outbox(), _quoted(), render(), send() (+27 more)

### Community 20 - "Week Editor UI"
Cohesion: 0.08
Nodes (56): act(), read(), HoursSection(), load(), reloadEnvelope(), HoursSectionProps, OpeningHours(), load() (+48 more)

### Community 21 - "Accept Invite UI"
Cohesion: 0.05
Nodes (36): AcceptInvite(), Message, Stage, store, useInviteLink(), AcceptInvitePage(), CheckInboxProps, ICONS (+28 more)

### Community 22 - "Booking Holds Tests"
Cohesion: 0.11
Nodes (43): token_in(), age(), app(), as_operator(), confirm(), counts(), first_after_tenant(), held() (+35 more)

### Community 23 - "Services Console UI"
Cohesion: 0.07
Nodes (42): NewServicePage(), ServicesPage(), EditServicePage(), ArchivedRow(), bringBack(), ArchiveZone(), archive(), BackLink() (+34 more)

### Community 24 - "Pending Booking Jobs"
Cohesion: 0.09
Nodes (32): pending(), enqueue(), Job, _record(), send_invite(), send_token(), bound(), inbox() (+24 more)

### Community 25 - "Admin Sessions And Auth"
Cohesion: 0.08
Nodes (24): admin_recently_verified(), admin_session(), admin_signed_in(), AdminSession, begin_enrolment(), business(), clear_admin_cookie(), CodeIn (+16 more)

### Community 26 - "Deployment Config"
Cohesion: 0.06
Nodes (19): DatabaseSettings, Settings, WorkerSettings, main(), ping(), _queue_gauge(), serve(), work() (+11 more)

### Community 27 - "Members API Tests"
Cohesion: 0.12
Nodes (39): member_id(), set_role(), app(), seed_booking_for(), stored_name(), test_a_change_whose_event_fails_leaves_nothing_changed(), test_a_display_name_must_be_one_to_sixty_printable_characters(), test_a_malformed_role_change_is_refused() (+31 more)

### Community 28 - "Booking Links Backend"
Cohesion: 0.10
Nodes (24): AvailabilityOut, BookingOut, cancel(), CancelIn, confirm_consents(), ConsentsIn, _decide(), EngineOut (+16 more)

### Community 29 - "Clients Backend"
Cohesion: 0.08
Nodes (20): add_consents(), as_clients(), ClientIn, ClientOut, ConsentIn, ConsentOut, create_client(), current_consents() (+12 more)

### Community 30 - "App Shell And Header"
Cohesion: 0.07
Nodes (27): ClientsPage(), Clients(), body, display, SignUpPage(), generateMetadata(), load, Loaded (+19 more)

### Community 31 - "Second Factor DB Tests"
Cohesion: 0.17
Nodes (34): new_secret(), proof(), test_enrol_ticket_lasts_one_hour(), test_enrol_wipes_the_factor_and_ends_sessions(), begin(), call(), code_proofs(), Enrolled (+26 more)

### Community 32 - "Working Hours Tests"
Cohesion: 0.11
Nodes (34): app(), hours_path(), shift(), stored(), test_a_failed_recording_leaves_the_week_unchanged(), test_a_malformed_body_is_refused(), test_a_member_outside_the_business_is_not_found(), test_a_non_uuid_path_is_refused() (+26 more)

### Community 33 - "Invites API Tests"
Cohesion: 0.15
Nodes (35): add_password(), fresh_email(), test_a_client_of_another_business_is_invisible(), test_a_consent_cannot_point_at_another_businesses_client(), app(), invite_jobs(), invite_row_count(), mint() (+27 more)

### Community 34 - "Admin Second Factor Tests"
Cohesion: 0.15
Nodes (26): code(), an_admin(), answer(), app(), audit(), failed_attempts(), probe(), sign_in() (+18 more)

### Community 35 - "Console Calendar Logic"
Cohesion: 0.11
Nodes (32): BlockPanel(), Done, Fields(), clearTime(), submit(), Props, openSpot(), PanelFrame() (+24 more)

### Community 36 - "Admin CLI"
Cohesion: 0.10
Nodes (26): main(), _reason(), lock_timed_out(), add_user(), audit_rows(), is_admin(), sqlstate(), test_app_role_can_only_append_platform_audit() (+18 more)

### Community 37 - "TOTP Secret Encryption"
Cohesion: 0.08
Nodes (18): SecondFactorSettings, _aead(), decrypt(), encrypt(), key(), key_state(), matching_step(), new_code() (+10 more)

### Community 38 - "Time Off Backend"
Cohesion: 0.09
Nodes (17): ApiError, blocks_overlapping(), checked(), checked_days(), create_time_off(), delete_time_off(), fields(), list_time_off() (+9 more)

### Community 39 - "Landing And Mail Templates"
Cohesion: 0.08
Nodes (42): Booking verification email (Dutch), Booking verification email (Portuguese), Mail delivery check, Hello test email (English), Email localization (en, nl, pt), Hello test email (Dutch), Hello test email (Portuguese), Single-use time-limited email link (+34 more)

### Community 40 - "Manage Booking Link UI"
Cohesion: 0.08
Nodes (31): Manage(), act(), afterWrite(), cancel(), confirmConsents(), loadWeek(), move(), open() (+23 more)

### Community 41 - "Booking Page API Tests"
Cohesion: 0.13
Nodes (31): add_membership(), add_worker(), app(), hits(), owner(), page(), _published(), slug() (+23 more)

### Community 42 - "Bookings Range Tests"
Cohesion: 0.15
Nodes (31): app(), history(), hour(), ids(), insert_event(), iso(), owner(), put() (+23 more)

### Community 43 - "Migration Tests"
Cohesion: 0.11
Nodes (29): delete_bookings(), booked(), downgrade_outcome(), lock_timeout_config(), migration(), plain_insert(), test_0027_refuses_to_backfill_an_existing_booking(), test_0028_does_not_backfill_existing_partial_blocks() (+21 more)

### Community 44 - "Availability Engine"
Cohesion: 0.16
Nodes (36): at(), local(), rows(), slots(), test_a_booking_blocks_its_own_override_buffer(), test_a_booking_without_an_override_blocks_a_percentage_of_its_length(), test_a_lunch_closure_splits_the_day_and_sells_nothing_in_the_gap(), test_a_new_slot_needs_its_own_buffer_before_the_next_booking() (+28 more)

### Community 45 - "Clients API Tests"
Cohesion: 0.11
Nodes (27): add_client(), app(), test_a_client_answer_carries_the_current_consent_per_purpose(), test_a_client_of_another_business_is_not_found(), test_a_consent_call_must_name_at_least_one_purpose(), test_a_merchant_consent_stores_the_connection_address_not_a_forwarded_header(), test_a_non_uuid_client_id_is_refused(), test_a_search_for_a_wildcard_matches_it_literally() (+19 more)

### Community 46 - "Sign In Tests"
Cohesion: 0.12
Nodes (24): email_of(), holder(), app(), client(), sign_in(), stranger(), test_a_malformed_request_gets_a_code_not_a_description(), test_a_member_of_several_businesses_lands_in_the_oldest_membership() (+16 more)

### Community 47 - "Logging Backend"
Cohesion: 0.09
Nodes (15): LogSettings, _add_context(), bound(), _causes(), _chain(), configure(), error_summary(), _escape() (+7 more)

### Community 48 - "First-run Setup Tests"
Cohesion: 0.14
Nodes (25): EmptyInstall, app(), body(), complete_sign_up(), configured(), count(), needs_setup(), password_hash() (+17 more)

### Community 49 - "Sign Up Tests"
Cohesion: 0.19
Nodes (27): issue_link(), live(), app(), businesses(), client(), complete(), created(), test_a_business_created_with_a_country_reports_its_currency() (+19 more)

### Community 50 - "Opening Hours Tests"
Cohesion: 0.12
Nodes (25): _another_owner_replaces(), app(), seed_opening(), stored_opening(), test_a_change_to_the_week_is_recorded_once_with_no_details(), test_a_malformed_body_is_refused(), test_a_saved_week_is_read_back(), test_a_shift_spanning_two_touching_opening_rows_is_accepted() (+17 more)

### Community 51 - "Password Auth DB Tests"
Cohesion: 0.15
Nodes (30): complete_sign_up(), Created, digest(), email_of(), issue(), test_a_link_is_live_only_unexpired_and_for_its_purpose(), test_a_password_reset_ends_every_session_and_every_other_reset_link(), test_a_reset_link_can_not_be_forged_through_a_temporary_table() (+22 more)

### Community 52 - "Cancellation Mail Templates"
Cohesion: 0.12
Nodes (35): booking_cancelled_by_client.en.txt (English), booking_cancelled_by_client.nl.txt (Dutch), booking_cancelled_by_client.pt.txt (Portuguese), Booking Cancelled by Client confirmation email (to client), booking_cancelled.en.txt (English), booking_cancelled.nl.txt (Dutch), booking_cancelled.pt.txt (Portuguese), Booking Cancelled by Business email (to client) (+27 more)

### Community 53 - "Session Auth Core"
Cohesion: 0.10
Nodes (19): clear_cookie(), create(), Credentials, describe(), hash_token(), _inet(), named(), origin() (+11 more)

### Community 54 - "Members Backend"
Cohesion: 0.11
Nodes (15): owner(), SignedIn, change_role(), DisplayNameChange, DisplayNameOut, guarded(), is_owner(), list_members() (+7 more)

### Community 55 - "Turnstile Tests"
Cohesion: 0.12
Nodes (20): TurnstileSettings, lifespan(), needed_at_startup(), enabled(), verify(), json_answer(), never_called(), _no_secret_by_default() (+12 more)

### Community 56 - "Bookings DB Tests"
Cohesion: 0.13
Nodes (23): book(), insert_booking(), load_migration(), seed_service(), statuses_in(), test_a_booking_cannot_point_at_another_businesses_client_worker_or_service(), test_a_cancelled_booking_frees_its_slot(), test_a_pending_booking_must_carry_an_expiry_and_an_expired_one_keeps_it() (+15 more)

### Community 57 - "Display Names Tests"
Cohesion: 0.17
Nodes (28): app(), display_name(), invite_token(), routes_behind_signed_in(), uses(), set_user_name(), test_a_blank_name_does_not_use_up_the_invite(), test_a_nameless_account_joining_another_business_stays_nameless() (+20 more)

### Community 58 - "Cancellation Policy"
Cohesion: 0.13
Nodes (20): decide(), Decision, Policy, test_a_booking_an_hour_away_across_the_fold_can_still_be_cancelled(), test_a_booking_moved_out_after_its_original_start_passed_can_still_be_cancelled(), test_a_booking_under_way_across_the_fold_has_started(), test_a_naive_datetime_is_refused(), test_a_negative_max_reschedules_is_refused() (+12 more)

### Community 59 - "Services Backend"
Cohesion: 0.12
Nodes (13): assign_workers(), create_service(), found(), list_services(), lock_members(), Price, PriceIn, read_service() (+5 more)

### Community 60 - "Admin Sessions Tests"
Cohesion: 0.17
Nodes (23): a_request(), admin_client(), admin_sign_in(), an_admin(), app(), platform_events(), sessions_of(), start() (+15 more)

### Community 61 - "Session Cookie API Tests"
Cohesion: 0.15
Nodes (23): age(), app(), cleared(), client(), deferred_table(), last_seen(), sign_in(), test_a_cross_site_form_cannot_sign_anyone_out() (+15 more)

### Community 62 - "Availability Slot Compute"
Cohesion: 0.10
Nodes (17): booked(), candidates(), clip(), compute(), member_slots(), merged(), now(), Offered (+9 more)

### Community 63 - "Schedule Backend"
Cohesion: 0.11
Nodes (11): lock_week(), opening_week(), overlapping(), read_opening_hours(), read_week(), replace_opening_hours(), replace_week(), Shift (+3 more)

### Community 64 - "Settings Page UI"
Cohesion: 0.12
Nodes (28): SettingsPage(), Field(), Settings(), onSubmit(), onTogglePublish(), breakMinutes(), changedKeys(), Draft (+20 more)

### Community 65 - "Tenant Tests"
Cohesion: 0.14
Nodes (26): concurrent(), b(), drop_tenants(), routes(), slug_of(), tag(), test_a_business_country_must_be_two_uppercase_letters(), test_a_business_currency_must_be_three_uppercase_letters() (+18 more)

### Community 66 - "Web Tracing"
Cohesion: 0.08
Nodes (27): eslintConfig, onRequestError(), enabled(), meterProvider(), provider(), STANDARD_METHODS, started, startProxySpan() (+19 more)

### Community 67 - "Decline Message Tests"
Cohesion: 0.19
Nodes (22): clear_jobs(), app(), body_of(), clean_outbox(), decline(), message_of(), owner(), _published() (+14 more)

### Community 68 - "Tracing Tests"
Cohesion: 0.13
Nodes (18): _attrs(), _MetricsReceiver, _one(), test_a_request_exports_one_allowlisted_server_span_and_no_query(), test_a_returned_500_still_gets_an_error_span(), test_extract_never_reads_tracestate(), test_http_request_method_attribute_is_bounded(), test_t10_log_lines_carry_the_active_spans_ids() (+10 more)

### Community 69 - "Booking Mail Templates"
Cohesion: 0.17
Nodes (29): booking_moved_team.en.txt (English: "Booking moved"), booking_moved_team_member.en.txt (English: "Booking moved"), booking_moved_team_member.nl.txt (Dutch: "Boeking verzet"), booking_moved_team_member.pt.txt (Portuguese: "Agendamento remarcado"), booking_moved_team.nl.txt (Dutch: "Boeking verzet"), booking_moved_team.pt.txt (Portuguese: "Agendamento remarcado"), booking_new.en.txt (English: "New booking"), booking_new.nl.txt (Dutch: "Nieuwe boeking") (+21 more)

### Community 70 - "Password Reset Tests"
Cohesion: 0.17
Nodes (22): jobs_for(), app(), client(), complete(), request_reset(), sessions_of(), sign_in(), test_a_dead_link_costs_no_password_hash() (+14 more)

### Community 71 - "Invites DB Tests"
Cohesion: 0.17
Nodes (18): call(), digest(), _insert(), minted(), new_accounts(), NewAccount, pending(), test_accept_binds_to_its_own_tenant() (+10 more)

### Community 72 - "FastAPI App Factory"
Cohesion: 0.10
Nodes (18): create_app(), dependencies(), healthz(), Dependencies, Health, openapi_document(), operation_id(), test_the_api_is_bound_to_its_database_while_it_runs() (+10 more)

### Community 73 - "Settings API Tests"
Cohesion: 0.18
Nodes (17): saved_settings(), signed_in(), app(), test_a_change_leaves_the_other_settings_as_they_were(), test_a_saved_setting_can_be_changed_and_set_back_to_its_default(), test_a_setting_of_the_wrong_kind_is_refused(), test_a_stored_setting_no_longer_in_the_registry_is_ignored(), test_a_stored_setting_that_no_longer_fits_fails_loudly() (+9 more)

### Community 74 - "Web Logging Instrumentation"
Cohesion: 0.14
Nodes (21): requestLog, startupLog, ErrorContext, errorFields(), errorLink(), ErrorRequest, errorType(), escape() (+13 more)

### Community 75 - "Body Size Cap Tests"
Cohesion: 0.14
Nodes (15): app(), client(), limit(), padded(), test_a_body_of_exactly_the_cap_reaches_validation(), test_a_chunked_oversize_body_is_a_413(), chunks(), test_a_declared_oversize_body_is_a_413_with_the_standard_headers() (+7 more)

### Community 76 - "Account Lifecycle Backend"
Cohesion: 0.13
Nodes (11): complete_password_reset(), complete_sign_up(), CompleteReset, CompleteSignUp, give_name(), LinkRequest, live_token(), NameIn (+3 more)

### Community 77 - "Backend Tracing Setup"
Cohesion: 0.12
Nodes (13): configure(), _enabled(), _meter_provider(), _provider(), _resource(), _readers(), test_service_version_defaults_to_the_app_version(), test_starting_the_app_adds_no_exporter() (+5 more)

### Community 78 - "Audit DB Tests"
Cohesion: 0.20
Nodes (13): events(), in_business(), record(), reviewed(), target(), test_a_business_sees_only_its_own_events(), test_a_reused_connection_records_an_event_for_no_business(), test_an_event_cannot_be_written_for_another_business() (+5 more)

### Community 79 - "Web Proxy Middleware"
Cohesion: 0.13
Nodes (20): RFC-7230, SetupPage(), readNeedsSetup(), setupGate(), setupRedirect(), endProxySpan(), traceparent(), config (+12 more)

### Community 80 - "Seed Bookings"
Cohesion: 0.21
Nodes (4): Booked, Business, email_of(), Seed

### Community 81 - "Parallel Safety Tests"
Cohesion: 0.15
Nodes (11): wait_until_blocked(), worker_url(), blocked_elsewhere(), blocked_on(), elsewhere(), fence_row(), test_a_serial_run_targets_the_shared_database(), test_each_xdist_worker_targets_its_own_database() (+3 more)

### Community 82 - "Booking Holds Backend"
Cohesion: 0.13
Nodes (11): current_policy_version(), confirm(), ConfirmIn, create(), HoldIn, HoldOut, HoldView, read() (+3 more)

### Community 83 - "Seed Script"
Cohesion: 0.12
Nodes (8): bucket(), check_local(), LocalRedirects, main(), on_grid(), opener(), Plan, test_guard_accepts_only_local_urls()

### Community 84 - "Client IP Tests"
Cohesion: 0.24
Nodes (13): app_trusting(), seen(), test_a_trusted_peers_forwarded_address_is_believed(), test_addresses_and_networks_together_are_accepted(), test_an_invalid_trusted_proxies_entry_stops_startup(), test_an_ipv6_forwarded_address_is_kept_whole(), test_an_unparseable_forwarded_value_is_stored_as_no_ip(), test_an_untrusted_peers_headers_are_ignored() (+5 more)

### Community 85 - "Database Role Tests"
Cohesion: 0.14
Nodes (12): probe_table(), test_app_role_cannot_change_the_schema(), test_app_role_gets_row_access_to_new_tables(), test_app_role_has_no_elevated_attributes(), test_app_role_owns_nothing(), test_btree_gist_is_installed(), test_migrate_role_owns_the_alembic_version_table(), test_a_tenant_cannot_change_or_delete_a_user_it_shares() (+4 more)

### Community 86 - "Frontend TS Config"
Cohesion: 0.10
Nodes (19): compilerOptions, allowImportingTsExtensions, allowJs, esModuleInterop, incremental, isolatedModules, jsx, lib (+11 more)

### Community 87 - "Seed Tests"
Cohesion: 0.22
Nodes (10): edges(), local(), local_date(), nows(), past_ends(), test_bucket_checks_in_order(), test_near_role_is_ahead_but_under_a_day(), test_roles_hold_across_clock_changes() (+2 more)

### Community 89 - "CI Smoke And Release"
Cohesion: 0.13
Nodes (18): Smoke test compose stack, Post-release smoke compose stack, Committed OpenAPI contract (backend/openapi.json), dev backend service, dev frontend service, CI workflow, CI Backend Tests job, Backend Checks aggregate (+10 more)

### Community 90 - "API Proxy Upstream"
Cohesion: 0.12
Nodes (4): LOCALE_MATCHER, MAX_BODY_BYTES, parsedLogLines(), waitForLog()

### Community 91 - "Booking Sweep Tests"
Cohesion: 0.27
Nodes (11): sweep(), due(), events_of(), seed(), status_of(), test_a_due_awaiting_payment_expires_as_well(), test_a_live_pending_and_a_confirmed_booking_are_left_alone(), test_a_second_sweep_expires_nothing_and_writes_no_events() (+3 more)

### Community 92 - "Settings Audit Tests"
Cohesion: 0.25
Nodes (9): app(), changes(), test_a_change_that_fails_after_its_event_records_nothing(), test_a_change_whose_event_fails_is_not_saved(), test_a_changed_setting_is_recorded_with_its_old_and_new_value(), test_a_second_change_records_what_it_replaced(), test_a_setting_saved_as_it_already_was_is_not_recorded(), test_an_event_that_changed_nothing_has_no_details_at_all() (+1 more)

### Community 93 - "First-run Setup"
Cohesion: 0.14
Nodes (7): SetupSettings, complete(), needed(), SetupIn, SetupState, state(), token_state()

### Community 94 - "Clients DB Tests"
Cohesion: 0.19
Nodes (11): row_of(), test_an_imported_consent_is_never_mailable(), test_every_consent_column_but_id_and_created_at_is_insertable(), test_find_or_create_keeps_the_stored_phone_and_locale_when_the_booker_sends_none(), test_find_or_create_never_touches_the_notes_or_the_user_id(), test_the_app_role_cannot_backdate_a_consent(), test_the_app_role_cannot_delete_a_client(), test_the_app_role_cannot_update_or_delete_a_consent() (+3 more)

### Community 95 - "Contributor Docs"
Cohesion: 0.13
Nodes (13): Stalled lock holder costs a 503, not a hang (ZIF-114), Hold a guest's time until they confirm (ZIF-117), app.availability.BOOKED / booked() / OCCUPYING, booking_holds (ZIF-117), bookings.sweep worker expiry, clients (business-owned client records), ex_bookings_worker_overlap exclusion constraint, app.clients.find_or_create (+5 more)

### Community 96 - "JSON Only Tests"
Cohesion: 0.18
Nodes (9): test_dependencies_503_with_a_coarse_code_when_the_database_is_down(), test_dependencies_ok_when_the_database_answers(), test_healthz_defaults_to_dev_when_unset(), test_healthz_reports_ok_and_running_version(), client(), test_cross_origin_requests_are_never_allowed(), test_json_with_parameters_is_accepted(), test_safe_methods_need_no_content_type() (+1 more)

### Community 97 - "Tenant Schema Docs"
Cohesion: 0.19
Nodes (8): broken_tables(), test_schema_has_no_tenant_isolation_violations(), test_the_check_catches_plain_foreign_keys_and_unisolated_tables(), violations(), enable_tenant_isolation (app.db), keep_an_owner trigger, members.member_user(lock=True), tenants root table

### Community 98 - "Audit Events API Tests"
Cohesion: 0.28
Nodes (8): add_event(), app(), test_a_page_holds_one_to_a_hundred_events(), test_an_owner_reads_only_their_business_newest_first(), test_an_owner_sees_the_address_and_browser_only_of_their_own_events(), test_only_an_owner_reads_the_log(), test_the_log_comes_in_pages_before_an_event(), test_the_log_needs_a_session()

### Community 99 - "Private Log Tests"
Cohesion: 0.17
Nodes (7): app(), clean_outbox(), client_for(), test_no_personal_data_ever_reaches_a_log(), cookie_secret(), secret(), token_secret()

### Community 100 - "Session API Tests"
Cohesion: 0.32
Nodes (10): start(), stored(), test_a_role_change_requires_ending_the_sessions_first(), test_a_session_cannot_start_for_someone_outside_the_tenant(), test_a_session_needs_the_membership_and_role_it_claims(), test_only_the_hash_of_a_random_token_is_stored(), test_removing_a_membership_ends_only_its_sessions(), test_starting_a_session_purges_expired_ones() (+2 more)

### Community 101 - "Tenant Isolation Tests"
Cohesion: 0.31
Nodes (9): add_parent(), Base, Child, Parent, tenants(), test_a_query_without_tenant_context_raises(), test_a_tenant_cannot_read_another_tenants_rows(), test_a_tenant_cannot_reference_another_tenants_rows() (+1 more)

### Community 102 - "Migration And Tenancy Rules"
Cohesion: 0.15
Nodes (10): Migration lock fails in 5s (ZIF-34), Alembic single-head migration chain, ziftbook_app role, ziftbook_migrate role, prod backend service, prod frontend service (172.28.0.10), prod migrate init service, prod worker service (app.worker) (+2 more)

### Community 103 - "Team And Today Console"
Cohesion: 0.23
Nodes (11): TodayPage(), Steps(), Today(), answer(), keepIt(), leave(), loadAgenda(), loadQueue() (+3 more)

### Community 104 - "Env Doc Check Scripts"
Cohesion: 0.19
Nodes (10): documentedNames(), pydanticFieldNames(), SCAN_PATHS, SKIP, usedNames(), ALLOWED, BUILD_ARGS, languageOf() (+2 more)

### Community 105 - "Slot Anchors And Buffers"
Cohesion: 0.17
Nodes (7): anchor(), buffer_for(), spans(), test_a_shift_s_first_slot_is_its_start_rounded_up_to_the_step(), test_clip_cuts_a_day_s_shifts_to_that_day_s_envelope(), test_day_span_tiles_with_no_gap_and_no_overlap(), test_the_buffer_is_the_override_else_a_percentage_rounded_up()

### Community 106 - "Availability Queries"
Cohesion: 0.23
Nodes (3): AvailabilityOut, read_availability(), read_merchant_availability()

### Community 107 - "Accounts Backend"
Cohesion: 0.17
Nodes (3): read_settings(), save(), update_settings()

### Community 108 - "Worker Schedules"
Cohesion: 0.20
Nodes (4): by_weekday(), day_shifts(), envelope(), to_utc()

### Community 109 - "Observability And Settings Docs"
Cohesion: 0.24
Nodes (9): Metrics over OTLP (ZIF-88), BusinessSettings typed settings model, Environment variables table, make observe with shared Grafana LGTM stack, Local development compose stack, Production compose stack (released GHCR images), make reset, make up local stack (+1 more)

### Community 110 - "Seed Test Helpers"
Cohesion: 0.23
Nodes (6): Counter, stub(), stubs(), test_requests_ignore_proxy_environment(), test_requests_refuse_redirects_off_the_machine(), test_seed_does_first_run_setup_only_when_due()

### Community 112 - "Docs Conventions"
Cohesion: 0.22
Nodes (9): jobs table background jobs (no broker), email.booking job (ZIF-53), JobKind(handler, timeout, grace), jobs table (app/jobs.py), Shared local Grafana LGTM stack, app.logs.configure() structured logging, tenant_context(tenant_id), app.tracing hand-written spans (+1 more)

### Community 113 - "Frontend Dependencies"
Cohesion: 0.18
Nodes (11): dependencies, next, next-intl, @opentelemetry/api, @opentelemetry/exporter-metrics-otlp-http, @opentelemetry/exporter-trace-otlp-http, @opentelemetry/resources, @opentelemetry/sdk-metrics (+3 more)

### Community 114 - "Blank Names Tests"
Cohesion: 0.38
Nodes (6): test_a_name_in_any_script_is_accepted(), test_a_name_that_renders_blank_or_has_no_letter_or_digit_is_refused(), test_an_emoji_only_business_name_at_sign_up_is_a_422(), test_free_text_keeps_emoji_punctuation_and_a_blank_glyph_between_words(), test_free_text_that_renders_blank_is_refused(), test_multiline_text_keeps_a_spacer_line_between_real_lines()

### Community 115 - "Renovate And Commit Hooks"
Cohesion: 0.20
Nodes (7): PR title workflow, Shared workflow pr-title.yml@v1, github>fjcloudaiconsulting/.github:app-stack#v1, github>fjcloudaiconsulting/.github#v1, extends, packageRules, $schema

### Community 116 - "Mail Rendering"
Cohesion: 0.22
Nodes (4): ics(), test_ics_folds_by_octet_and_escapes_text(), test_ics_lines_and_stamps(), test_ics_strips_control_characters_from_summary()

### Community 117 - "Tracing Spans"
Cohesion: 0.22
Nodes (5): span(), test_metric_points_carry_no_exemplars(), test_t10_add_context_never_mutates_the_shared_dict(), test_the_health_check_records_no_metric(), test_the_request_duration_metric_has_the_route_and_no_url()

### Community 118 - "Admin Route Allowlist"
Cohesion: 0.36
Nodes (3): admin_routes(), test_admin_routes_are_the_allowlist(), test_owner_never_gets_2xx_on_admin_routes()

### Community 120 - "Release 0.25.0 Docs"
Cohesion: 0.29
Nodes (7): Release 0.25.0 (2026-10-15), First-run setup creates platform admin (ZIF-70), Admins need a second factor (ZIF-71), Platform admin (ZIF-70, TOTP ZIF-71), Mailpit local mail, make seed synthetic data, App version 0.25.0

### Community 121 - "Readme Setup Docs"
Cohesion: 0.25
Nodes (8): Conventional Commits PR titles, GHCR images backend/frontend/migrations, Shared release workflows (fjcloudaiconsulting/.github RELEASE_CONTRACT.md), release-please, Release PR (chore(main): release X.Y.Z), Translation catalogs (en source, nl, pt), CI Repo Checks job, docker-compose-prod.yaml

### Community 122 - "Frontend Build Deps"
Cohesion: 0.25
Nodes (8): devDependencies, eslint, eslint-config-next, @hey-api/openapi-ts, @types/node, @types/react, @types/react-dom, typescript

### Community 123 - "Frontend Test Deps"
Cohesion: 0.25
Nodes (8): scripts, build, dev, generate, lint, start, test, typecheck

### Community 124 - "Landing Dependencies"
Cohesion: 0.25
Nodes (7): name, private, scripts, build, dev, test, type

### Community 125 - "Repo Check Scripts"
Cohesion: 0.43
Nodes (4): newestReservedWords(), reservedWords(), topLevelRoutes(), unreservedRoutes()

### Community 126 - "DST Day Span"
Cohesion: 0.29
Nodes (6): day_span(), test_day_span_at_a_repeated_midnight_starts_at_the_first_occurrence(), test_day_span_at_a_skipped_midnight_starts_at_the_day_s_first_instant(), test_day_span_is_23_or_25_hours_around_a_clock_change(), test_day_span_is_not_a_bare_subtraction_of_two_aware_datetimes(), test_day_span_of_a_run_spans_all_of_it()

### Community 127 - "Booking Setting Fixtures"
Cohesion: 0.33
Nodes (4): save_setting(), _published(), _published(), _published()

### Community 128 - "Landing Deploy And Client IP"
Cohesion: 0.33
Nodes (6): Landing deploys with Worker-scoped token (INFRA-113), Production path Cloudflare -> Traefik -> web -> API (k3s), ZIF_CLIENT_IP_HEADER, ZIF_TRUSTED_PROXIES, Deploy Landing job, ziftbook-landing Cloudflare Worker

### Community 129 - "Project Docs"
Cohesion: 0.33
Nodes (7): audit_events audit log, auth.record, booking_events append-only table, app.clients.CONSENT_TEXTS, consents append-only table, join_tenant(session, tenant_id), /api/public routes

### Community 130 - "Landing Worker"
Cohesion: 0.43
Nodes (5): fetch(), pickLanguage(), redirect(), SUPPORTED, assets

### Community 131 - "Admin Sessions Migration"
Cohesion: 0.47
Nodes (3): downgrade(), reset_password(), upgrade()

### Community 133 - "Landing Build"
Cohesion: 0.33
Nodes (4): alternates, LOCALES, strings, template

### Community 134 - "Catalog And Hook Checks"
Cohesion: 0.53
Nodes (5): blankBranches(), CATALOG_SETS, compareCatalogs(), kind(), placeholders()

### Community 136 - "Reserve Setup Slug Migration"
Cohesion: 0.60
Nodes (3): downgrade(), slug_ok(), upgrade()

### Community 137 - "Admin Second Factor Migration"
Cohesion: 0.60
Nodes (3): downgrade(), grant(), upgrade()

### Community 139 - "CI Changed-Paths Script"
Cohesion: 0.50
Nodes (3): affected(), all, none

### Community 140 - "Community 117"
Cohesion: 0.50
Nodes (4): counting(), run_until_idle(), verify(), verify_then_reset()

### Community 142 - "Dev Mail And Worker"
Cohesion: 0.67
Nodes (3): Mail via Mailgun HTTP API, never SMTP (ZIF-151), Mailpit local mail catcher, dev worker service

### Community 143 - "Community 118"
Cohesion: 0.67
Nodes (3): Ziftbook App Icon (favicon), Brand Red #b31513, Z Logo Mark (white stroked Z glyph)

### Community 145 - "Community 119"
Cohesion: 0.67
Nodes (3): Brand Color #b31513 (Ziftbook red), Landing Favicon (red rounded square with white Z), Z Monogram Logo Mark

## Ambiguous Edges - Review These
- `Landing page HTML template` → `Schibsted Grotesk font (landing)`  [AMBIGUOUS]
  landing/template.html · relation: references
- `SecLists` → `Sign out on every device after password reset`  [AMBIGUOUS]
  backend/licenses/seclists-MIT.txt · relation: conceptually_related_to

## Knowledge Gaps
- **57 isolated node(s):** `docker-compose-prod.yaml`, `Shared release workflows (fjcloudaiconsulting/.github RELEASE_CONTRACT.md)`, `make openapi`, `backend/openapi.json committed API contract`, `Release 0.23.0 (2026-10-08)` (+52 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 1383 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **103 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `app.logs.configure() structured logging` connect `Docs Conventions` to `Password Hashing Core`?**
  _High betweenness centrality (0.310) - this node is a cross-community bridge._
- **Are the 827 inferred relationships involving `People` (e.g. with `test_a_failed_send_keeps_the_request_for_the_retry()` and `test_a_reset_link_uses_the_users_own_language()`) actually correct?**
  _`People` has 827 INFERRED edges - model-reasoned connections that need verification._
- **What connects `docker-compose-prod.yaml`, `Shared release workflows (fjcloudaiconsulting/.github RELEASE_CONTRACT.md)`, `make openapi` to the rest of the system?**
  _57 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Calendar Agenda Client` be split into smaller, more focused modules?**
  _Cohesion score 0.035023041474654376 - nodes in this community are weakly interconnected._
- **Why does `app.tracing hand-written spans` connect `Docs Conventions` to `Web Proxy Middleware`?**
  _High betweenness centrality (0.308) - this node is a cross-community bridge._

### Low-confidence Hints
_AMBIGUOUS edges — the extractor was unsure. Verify before acting on these._

- **What is the exact relationship between `Landing page HTML template` and `Schibsted Grotesk font (landing)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **What is the exact relationship between `SecLists` and `Sign out on every device after password reset`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._