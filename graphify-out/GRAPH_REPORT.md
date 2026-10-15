# Graph Report - ziftbook  (2026-10-09)

## Corpus Check
- 346 files · ~325,507 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 26 file(s) not represented in the graph (top: (none) 12, .woff2 6, .css 4)

## Summary
- 4686 nodes · 17145 edges · 217 communities (118 shown, 99 thin omitted)
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 1257 edges (avg confidence: 0.93)
- Token cost: 66,944 input · 0 output

## Community Hubs (Navigation)
- Shared UI And Manage Booking
- Console Calendar Logic
- Session API Tests
- Seed Data Builders
- Turnstile Booking Tests
- Tracing Tests
- Bookings Backend
- Console Today Data
- Public Booking Page UI
- Availability API Tests
- Worker And Config
- Week Editor UI
- Account Email Tests
- Services Console UI
- Time Off Backend
- Job Runner Tests
- Password Auth DB Tests
- Password Hashing Core
- Booking Links Backend
- Clients Backend
- Time Off Console UI
- Accounts Backend
- Availability Queries
- Members Backend
- Cancellation Policy
- Settings Console UI
- App Shell And Header
- Services Backend
- Team And Today Console
- Schedule Backend
- Logging Backend
- Invites DB Tests
- Web Logging Instrumentation
- Clients DB Tests
- Invites Backend
- Test Fixtures Conftest
- Prod Compose And API Main
- Account Web Client
- Seed Script
- Seed Fixtures
- Booking Holds Backend
- Mail Sending
- Tenant Isolation Tests
- Seed Helpers
- Audit Backend
- Seed Test Helpers
- JSON Only Tests
- Landing Worker
- Env Doc Check
- Time Off Tests
- Landing Build
- Community 112
- Community 117
- Availability Engine
- Booking Links API Tests
- Booking Email Tests
- Logging Tests
- Clients API Tests
- Merchant Booking Tests
- Booking Holds Tests
- Members API Tests
- Mail Tests
- Working Hours Tests
- Bookings Range Tests
- Invites API Tests
- Audit Recording Tests
- Database Engine And Sessions
- Booking And Availability Tests
- Booking Page API Tests
- Sign Up Tests
- Opening Hours Tests
- Sign In Tests
- Migration Tests
- Bookings DB Tests
- Bookings API Tests
- Display Names Tests
- Password Reset Tests
- Decline Message Tests
- Tenant Tests
- Turnstile Tests
- Web Tracing
- Body Size Cap Tests
- Rate Limits
- Owner Keeping Tests
- Audit DB Tests
- Settings API Tests
- Backend Tracing Setup
- API Proxy Upstream
- Booking Approval Tests
- Seed Tests
- Booking Sweep Tests
- Settings Audit Tests
- Repo Check Scripts
- Tenant Schema Docs
- Private Log Tests
- Audit Events API Tests
- Service Workers Tests
- Mail Rendering
- Web Proxy Middleware
- Catalog And Hook Checks
- Blank Names Tests
- Renovate And Commit Hooks
- Readme Setup Docs
- Community 113
- Community 114
- Community 115
- Community 121
- Community 122
- Community 125
- Community 164
- Community 165
- Community 167
- Community 168
- Community 169
- Community 170
- Community 173
- Community 174
- Community 182
- Community 183
- Community 184
- Community 185
- Community 186
- Community 208
- Contributor Docs
- Docs Conventions
- Project Docs
- Frontend Dependencies
- Frontend Build Deps
- Frontend Test Deps
- Landing Dependencies
- Community 120
- Community 123
- Community 124
- Community 126
- Community 127
- Community 128
- Community 129
- Community 130
- Community 131
- Community 132
- Community 133
- Community 134
- Community 135
- Community 136
- Community 137
- Community 138
- Community 139
- Community 140
- Community 141
- Community 142
- Community 143
- Community 144
- Community 146
- Community 147
- Community 148
- Community 149
- Community 150
- Community 151
- Community 152
- Community 154
- Community 155
- Community 156
- Community 157
- Community 158
- Community 159
- Community 160
- Community 161
- Community 162
- Community 163
- Community 166
- Community 171
- Community 172
- Community 175
- Community 176
- Community 181
- Community 187
- Community 188
- Community 189
- Community 190
- Community 191
- Community 192
- Community 193
- Community 194
- Community 201
- Community 202
- Community 203
- Community 204
- Community 205
- Community 206
- Community 207
- Community 210
- Community 211
- Community 215
- Community 216
- Frontend TS Config
- Community 118
- Community 119
- Landing And Mail Templates
- Cancellation Mail Templates
- Booking Mail Templates
- Community 177
- Community 178
- Community 179
- Community 180
- Community 195
- Community 196
- Community 197
- Community 198
- Community 199
- Community 200
- Community 209
- Community 212
- Community 213
- Community 214
- Community 145

## God Nodes (most connected - your core abstractions)
1. `People` - 878 edges
2. `tenant_context()` - 337 edges
3. `signed_in()` - 326 edges
4. `new_client()` - 293 edges
5. `member_id()` - 260 edges
6. `fresh_email()` - 165 edges
7. `at()` - 153 edges
8. `create_app()` - 132 edges
9. `patch()` - 111 edges
10. `events()` - 95 edges

## Surprising Connections (you probably didn't know these)
- `TurnstileSettings` --references--> `Production compose stack`  [INFERRED]
  backend/app/config.py → docker-compose-prod.yaml
- `healthz()` --references--> `CI job: post-release smoke`  [INFERRED]
  backend/app/main.py → .github/workflows/ci.yml
- `MailSettings` --references--> `prod worker service (python -m app.worker, Mailgun)`  [EXTRACTED]
  backend/app/config.py → docker-compose-prod.yaml
- `main()` --calls--> `prod worker service (python -m app.worker, Mailgun)`  [EXTRACTED]
  backend/app/worker.py → docker-compose-prod.yaml
- `main()` --calls--> `dev worker service (python -m app.worker, Mailpit)`  [EXTRACTED]
  backend/app/worker.py → docker-compose.yaml

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Self-hosted fonts shipped with OFL notices in frontend and landing** — frontend_licenses_schibsted_grotesk_ofl_license, frontend_licenses_young_serif_ofl_license, landing_licenses_schibsted_grotesk_ofl_license, landing_licenses_young_serif_ofl_license, frontend_licenses_young_serif_ofl_sil_open_font_license [EXTRACTED 1.00]
- **Booking request decline emails** — backend_app_mail_templates_booking_declined_template, backend_app_mail_templates_booking_declined_message_template, backend_app_mail_templates_decline_message [INFERRED 0.85]
- **Booking request approval flow emails** — mail_template_booking_request, mail_template_booking_received, booking_approval_expiry [INFERRED 0.85]
- **Business-facing client action notifications with link** — backend_app_mail_templates_booking_client_cancelled_template, backend_app_mail_templates_booking_client_rescheduled_template, backend_app_mail_templates_client, backend_app_mail_templates_link [INFERRED 0.85]
- **Client-facing booking notification emails** — backend_app_mail_templates_booking_cancelled_template, backend_app_mail_templates_booking_cancelled_by_client_template, backend_app_mail_templates_booking_confirmed_template, backend_app_mail_templates_booking_declined_template, backend_app_mail_templates_booking_declined_message_template [INFERRED 0.85]
- **Compose stacks running the same three release images** — docker_compose_prod, compose_smoke, docker_compose [INFERRED 0.85]
- **Guest-facing booking emails** — mail_template_booking_received, mail_template_booking_reminder, guest_self_service_booking_link [INFERRED 0.85]
- **Emailed single-use link flows proving address ownership** — backend_app_mail_templates_invite_en_template, backend_app_mail_templates_password_reset_en_template, backend_app_mail_templates_sign_up_en_template, backend_app_mail_templates_invite_en_single_use_link [INFERRED 0.85]
- **Business/team-facing booking notifications** — mail_template_booking_moved_team, mail_template_booking_moved_team_member, mail_template_booking_new, mail_template_booking_request [INFERRED 0.85]

## Communities (217 total, 99 thin omitted)

### Community 0 - "Shared UI And Manage Booking"
Cohesion: 0.05
Nodes (102): Note, Pick, View, Message, Stage, Message, Props, CheckInboxProps (+94 more)

### Community 1 - "Console Calendar Logic"
Cohesion: 0.04
Nodes (112): Load, Done, Props, Cell, Column, Item, Loaded, Popover (+104 more)

### Community 10 - "Session API Tests"
Cohesion: 0.06
Nodes (80): Credentials, SessionOut, clear_cookie(), create(), describe(), hash_token(), read(), record() (+72 more)

### Community 103 - "Seed Data Builders"
Cohesion: 0.32
Nodes (3): Mailbox, Mailpit, read through its API., The first capture of `pattern` in a message to `to` that was not there before…

### Community 14 - "Tracing Tests"
Cohesion: 0.05
Nodes (66): _MetricsReceiver, create_app(), openapi_document(), span(), test_the_api_is_bound_to_its_database_while_it_runs(), test_the_contract_builds_without_a_database_url(), test_the_worker_still_requires_a_database_url(), test_importing_the_engine_drags_in_no_other_app_module() (+58 more)

### Community 15 - "Bookings Backend"
Cohesion: 0.06
Nodes (77): Account, AgendaOut, BookingDetailOut, BookingIn, BookingOut, BookingRow, BookingStatusOut, EventOut (+69 more)

### Community 19 - "Console Today Data"
Cohesion: 0.05
Nodes (66): Row, ConsoleContextValue, GuardedWriteResult, Identity, Nav, ReadOutcome, Role, Section (+58 more)

### Community 2 - "Public Booking Page UI"
Cohesion: 0.04
Nodes (99): View, CacheEntry, Chosen, Done, Flow, Sent, Service, Step2Plus (+91 more)

### Community 20 - "Availability API Tests"
Cohesion: 0.10
Nodes (66): People, app(), clear_time_off(), get(), insert_day_block(), local(), local_dt(), owner() (+58 more)

### Community 23 - "Worker And Config"
Cohesion: 0.05
Nodes (50): DatabaseSettings, LogSettings, MailSettings, Settings, TurnstileSettings, WorkerSettings, settings_fields(), lifespan() (+42 more)

### Community 24 - "Week Editor UI"
Cohesion: 0.09
Nodes (53): HoursSectionProps, ApiShift, Phase, ServerProblem, Status, T, WeekEditorProps, Day (+45 more)

### Community 25 - "Account Email Tests"
Cohesion: 0.09
Nodes (54): Job, pending(), render(), send_invite(), send_token(), bound(), inbox(), link_in() (+46 more)

### Community 26 - "Services Console UI"
Cohesion: 0.08
Nodes (46): Errors, Props, Locale, NameMap, ServiceBody, ServiceBodyResult, ServiceForm, NewServicePage() (+38 more)

### Community 27 - "Time Off Backend"
Cohesion: 0.08
Nodes (50): ApiError, InTheWay, TimeOffChange, TimeOffIn, TimeOffOut, TimeOffRangeOut, blocks_overlapping(), checked() (+42 more)

### Community 28 - "Job Runner Tests"
Cohesion: 0.08
Nodes (51): JobKind, _connections(), _pending(), _pool(), queue(), closed(), points(), test_a_delivery_counts_as_sent_or_failed_by_template() (+43 more)

### Community 29 - "Password Auth DB Tests"
Cohesion: 0.09
Nodes (54): Created, delete_services(), wait_until_blocked(), blocked_elsewhere(), blocked_on(), elsewhere(), fence_row(), test_wait_until_blocked_ignores_another_databases_waiter() (+46 more)

### Community 3 - "Password Hashing Core"
Cohesion: 0.05
Nodes (66): CountryDefaults, Error, named(), printable(), _inet(), operation_id(), check_new_password(), hash_password() (+58 more)

### Community 30 - "Booking Links Backend"
Cohesion: 0.10
Nodes (53): AvailabilityOut, BookingOut, CancelIn, ConsentsIn, EngineOut, LinkedBooking, RescheduleIn, TokenIn (+45 more)

### Community 35 - "Clients Backend"
Cohesion: 0.08
Nodes (45): ClientIn, ClientOut, ConsentIn, ConsentOut, NoteChange, add_consents(), as_clients(), create_client() (+37 more)

### Community 39 - "Time Off Console UI"
Cohesion: 0.11
Nodes (39): FormState, Mode, Block, BlockForm, BlockLabel, Patch, blockMeta(), blockTitle() (+31 more)

### Community 40 - "Accounts Backend"
Cohesion: 0.06
Nodes (42): CompleteReset, CompleteSignUp, LinkRequest, NameIn, BusinessSettings, complete_password_reset(), complete_sign_up(), give_name() (+34 more)

### Community 43 - "Availability Queries"
Cohesion: 0.08
Nodes (40): AvailabilityOut, Offered, WorkerOut, BookingPageOut, CancellationOut, PublicServiceOut, booked(), compute() (+32 more)

### Community 51 - "Members Backend"
Cohesion: 0.09
Nodes (36): SignedIn, DisplayNameChange, DisplayNameOut, MemberOut, RoleChange, named(), owner(), change_role() (+28 more)

### Community 52 - "Cancellation Policy"
Cohesion: 0.13
Nodes (30): Decision, Policy, Unset, decide(), test_a_booking_an_hour_away_across_the_fold_can_still_be_cancelled(), test_a_booking_moved_out_after_its_original_start_passed_can_still_be_cancelled(), test_a_booking_under_way_across_the_fold_has_started(), test_a_naive_datetime_is_refused() (+22 more)

### Community 54 - "Settings Console UI"
Cohesion: 0.14
Nodes (30): Draft, Field, IntKey, Key, Settings, SettingsPage(), Field(), Settings() (+22 more)

### Community 55 - "App Shell And Header"
Cohesion: 0.08
Nodes (26): Loaded, Locale, AppConfig, generateMetadata(), PublicBookingPage(), Flag(), Header(), LanguageLinks() (+18 more)

### Community 56 - "Services Backend"
Cohesion: 0.12
Nodes (33): PriceIn, ServiceChange, ServiceIn, ServiceOut, assign_workers(), create_service(), found(), list_services() (+25 more)

### Community 6 - "Team And Today Console"
Cohesion: 0.05
Nodes (68): HoursSummaries, Invite, act(), read(), MyHours(), MyHoursPage(), MyHoursTimeOffPage(), OpeningHours() (+60 more)

### Community 60 - "Schedule Backend"
Cohesion: 0.11
Nodes (32): Shift, lock_week(), opening_week(), overlapping(), read_opening_hours(), read_week(), replace_opening_hours(), replace_week() (+24 more)

### Community 61 - "Logging Backend"
Cohesion: 0.11
Nodes (25): Formatter, _add_context(), _causes(), _chain(), error_summary(), _escape(), _invalid_env(), stream_handler() (+17 more)

### Community 63 - "Invites DB Tests"
Cohesion: 0.17
Nodes (28): NewAccount, call(), digest(), _insert(), minted(), new_accounts(), pending(), test_accept_binds_to_its_own_tenant() (+20 more)

### Community 65 - "Web Logging Instrumentation"
Cohesion: 0.14
Nodes (24): ErrorContext, ErrorRequest, ExcLink, Format, Level, onRequestError(), register(), errorFields() (+16 more)

### Community 66 - "Clients DB Tests"
Cohesion: 0.14
Nodes (26): Found, find_or_create(), row_of(), seed_client(), test_a_booking_with_no_email_creates_a_new_client_every_time(), test_a_client_of_another_business_is_invisible(), test_a_consent_cannot_point_at_another_businesses_client(), test_every_consent_column_but_id_and_created_at_is_insertable() (+18 more)

### Community 67 - "Invites Backend"
Cohesion: 0.11
Nodes (26): AcceptInvite, Found, InviteDetails, InviteOut, InviteToken, NewInvite, accept(), create() (+18 more)

### Community 7 - "Test Fixtures Conftest"
Cohesion: 0.04
Nodes (76): Spy, _after_cursor_execute(), _before_cursor_execute(), _handle_error(), _queue_gauge(), bound(), _keep_pytest_thread_hook(), log_lines() (+68 more)

### Community 71 - "Prod Compose And API Main"
Cohesion: 0.11
Nodes (24): Dependencies, Health, dependencies(), healthz(), Shared workflow smoke.yml@v1, CI job: post-release smoke, Fixed subnet so backend trusts exactly the frontend (ZIF_TRUSTED_PROXIES=172.28.0.10), ZIF_CLIENT_IP_HEADER deliberately not passed through (+16 more)

### Community 73 - "Account Web Client"
Cohesion: 0.13
Nodes (14): Country, Place, SignUpRequestErrors, byName(), clock(), likelyCountry(), secondsLeft(), signUpRequest() (+6 more)

### Community 74 - "Seed Script"
Cohesion: 0.11
Nodes (21): LocalRedirects, Plan, bucket(), check_local(), main(), on_grid(), opener(), test_bucket_checks_in_order() (+13 more)

### Community 77 - "Seed Fixtures"
Cohesion: 0.22
Nodes (7): Api, Seed, email_of(), Any, Invite and accept, from a cookie-less connection: accepting replaces its…, One person's connection: its own cookies, so its own session., What the owners see for each booking is what was meant to be seeded.

### Community 80 - "Booking Holds Backend"
Cohesion: 0.13
Nodes (19): ConfirmIn, HoldIn, HoldOut, HoldView, ReleaseIn, TokenIn, current_policy_version(), confirm() (+11 more)

### Community 81 - "Mail Sending"
Cohesion: 0.12
Nodes (18): MailNotConfigured, _local_text(), _quoted(), _send(), send_booking_verify(), transport(), RuntimeError, Email through Mailgun's HTTP API when deployed, Mailpit's HTTP API in… (+10 more)

### Community 90 - "Tenant Isolation Tests"
Cohesion: 0.31
Nodes (13): Base, Child, Parent, add_parent(), tenants(), test_a_query_without_tenant_context_raises(), test_a_tenant_cannot_read_another_tenants_rows(), test_a_tenant_cannot_reference_another_tenants_rows() (+5 more)

### Community 95 - "Seed Helpers"
Cohesion: 0.33
Nodes (6): Booked, Business, date, A guest books the first slot offered in first..last, the next if it was just…, The owner books a client in at any start (a walk-in, or one already past)., A guest booking in an auto-confirming business, and a guest connection opened…

### Community 96 - "Audit Backend"
Cohesion: 0.18
Nodes (11): AuditEventOut, list_events(), BaseModel, CurrentSession, ge, get, le, Query (+3 more)

### Community 97 - "Seed Test Helpers"
Cohesion: 0.25
Nodes (9): Counter, stub(), stubs(), test_requests_ignore_proxy_environment(), test_requests_refuse_redirects_off_the_machine(), Any, fixture, MonkeyPatch (+1 more)

### Community 100 - "JSON Only Tests"
Cohesion: 0.39
Nodes (8): client(), test_cross_origin_requests_are_never_allowed(), test_json_with_parameters_is_accepted(), test_safe_methods_need_no_content_type(), test_state_changing_requests_that_are_not_json_are_rejected(), fixture, parametrize, TestClient

### Community 107 - "Landing Worker"
Cohesion: 0.43
Nodes (5): fetch(), pickLanguage(), redirect(), SUPPORTED, assets

### Community 108 - "Env Doc Check"
Cohesion: 0.43
Nodes (5): documentedNames(), pydanticFieldNames(), usedNames(), SCAN_PATHS, SKIP

### Community 11 - "Time Off Tests"
Cohesion: 0.10
Nodes (80): member_id(), app(), block(), block_path(), day_block(), google(), insert_block(), insert_day_block() (+72 more)

### Community 111 - "Landing Build"
Cohesion: 0.33
Nodes (4): alternates, LOCALES, strings, template

### Community 112 - "Community 112"
Cohesion: 0.40
Nodes (4): _access_fields(), access_log(), Any, Request

### Community 117 - "Community 117"
Cohesion: 0.67
Nodes (3): run_until_idle(), verify(), verify_then_reset()

### Community 12 - "Availability Engine"
Cohesion: 0.07
Nodes (81): anchor(), clip(), day_span(), member_slots(), merged(), overlaps(), by_weekday(), day_shifts() (+73 more)

### Community 13 - "Booking Links API Tests"
Cohesion: 0.12
Nodes (80): app(), booking_jobs(), cancel_body(), confirmed_booking(), counts(), email_of_user(), first_slot_after(), get() (+72 more)

### Community 16 - "Booking Email Tests"
Cohesion: 0.09
Nodes (74): tenant_context(), _mark_sent(), send(), send_booking(), add_worker(), app(), clean_outbox(), _client_email() (+66 more)

### Community 17 - "Logging Tests"
Cohesion: 0.05
Nodes (65): bound(), configure(), _excepthook(), _boom(), _fields(), _raised(), raw_request(), _raw_request() (+57 more)

### Community 18 - "Clients API Tests"
Cohesion: 0.06
Nodes (68): fresh_address(), app_trusting(), seen(), test_a_trusted_peers_forwarded_address_is_believed(), test_addresses_and_networks_together_are_accepted(), test_an_invalid_trusted_proxies_entry_stops_startup(), test_an_ipv6_forwarded_address_is_kept_whole(), test_an_unparseable_forwarded_value_is_stored_as_no_ip() (+60 more)

### Community 21 - "Merchant Booking Tests"
Cohesion: 0.12
Nodes (68): ana(), ana_id(), app(), availability(), book(), boss(), clean_outbox(), clear_jobs() (+60 more)

### Community 22 - "Booking Holds Tests"
Cohesion: 0.11
Nodes (61): sent_to(), token_in(), seed_booking(), age(), app(), as_operator(), confirm(), counts() (+53 more)

### Community 31 - "Members API Tests"
Cohesion: 0.11
Nodes (49): signed_in(), app(), seed_booking_for(), stored_name(), test_a_change_whose_event_fails_leaves_nothing_changed(), test_a_display_name_must_be_one_to_sixty_printable_characters(), test_a_malformed_role_change_is_refused(), test_a_member_of_another_business_is_not_found() (+41 more)

### Community 32 - "Mail Tests"
Cohesion: 0.08
Nodes (45): _claim(), enqueue(), _record(), _run(), run_once(), deliver(), run_until_idle(), run_until_idle() (+37 more)

### Community 33 - "Working Hours Tests"
Cohesion: 0.11
Nodes (47): app(), hours_path(), shift(), stored(), test_a_failed_recording_leaves_the_week_unchanged(), test_a_malformed_body_is_refused(), test_a_member_outside_the_business_is_not_found(), test_a_non_uuid_path_is_refused() (+39 more)

### Community 34 - "Bookings Range Tests"
Cohesion: 0.14
Nodes (44): app(), history(), hour(), ids(), insert_event(), iso(), owner(), put() (+36 more)

### Community 36 - "Invites API Tests"
Cohesion: 0.15
Nodes (43): add_password(), app(), invite_jobs(), invite_row_count(), mint(), spy(), test_a_failed_audit_write_leaves_no_invite_or_job(), test_a_failed_sign_in_leaves_the_invite_unused_and_no_account() (+35 more)

### Community 37 - "Audit Recording Tests"
Cohesion: 0.14
Nodes (43): events(), failing(), _mailgun_post(), app(), businesses(), client_at(), complete_reset(), complete_sign_up() (+35 more)

### Community 4 - "Database Engine And Sessions"
Cohesion: 0.03
Nodes (35): _count(), enable_tenant_isolation(), _forget(), _set_tenant(), upgrade(), upgrade(), upgrade(), languages() (+27 more)

### Community 41 - "Booking And Availability Tests"
Cohesion: 0.14
Nodes (40): assign(), new_service(), ready(), test_an_archived_service_is_not_found(), test_only_assigned_workers_with_hours_are_listed(), weekdays(), ready(), _published() (+32 more)

### Community 42 - "Booking Page API Tests"
Cohesion: 0.14
Nodes (40): add_worker(), app(), hits(), owner(), page(), _published(), slug(), slug_of() (+32 more)

### Community 44 - "Sign Up Tests"
Cohesion: 0.20
Nodes (38): fresh_email(), issue_link(), live(), app(), businesses(), client(), complete(), created() (+30 more)

### Community 45 - "Opening Hours Tests"
Cohesion: 0.12
Nodes (36): _another_owner_replaces(), app(), seed_opening(), stored_opening(), test_a_change_to_the_week_is_recorded_once_with_no_details(), test_a_malformed_body_is_refused(), test_a_saved_week_is_read_back(), test_a_shift_spanning_two_touching_opening_rows_is_accepted() (+28 more)

### Community 47 - "Sign In Tests"
Cohesion: 0.12
Nodes (32): holder(), app(), client(), sign_in(), stranger(), test_a_malformed_request_gets_a_code_not_a_description(), test_a_member_of_several_businesses_lands_in_the_oldest_membership(), test_a_password_is_matched_exactly_up_to_unicode_composition() (+24 more)

### Community 48 - "Migration Tests"
Cohesion: 0.12
Nodes (36): delete_bookings(), delete_clients(), booked(), downgrade_outcome(), lock_timeout_config(), plain_insert(), test_0027_refuses_to_backfill_an_existing_booking(), test_0028_does_not_backfill_existing_partial_blocks() (+28 more)

### Community 49 - "Bookings DB Tests"
Cohesion: 0.13
Nodes (34): book(), insert_booking(), load_migration(), seed_service(), statuses_in(), test_a_booking_cannot_point_at_another_businesses_client_worker_or_service(), test_a_cancelled_booking_frees_its_slot(), test_a_pending_booking_must_carry_an_expiry_and_an_expired_one_keeps_it() (+26 more)

### Community 5 - "Bookings API Tests"
Cohesion: 0.08
Nodes (93): new_client(), put_settings(), app(), at(), book_twice_under_auto_confirm(), booking_url(), commit_bypassing_the_lock(), merchant_book() (+85 more)

### Community 50 - "Display Names Tests"
Cohesion: 0.17
Nodes (36): app(), display_name(), invite_token(), routes_behind_signed_in(), uses(), set_user_name(), test_a_blank_name_does_not_use_up_the_invite(), test_a_nameless_account_joining_another_business_stays_nameless() (+28 more)

### Community 53 - "Password Reset Tests"
Cohesion: 0.16
Nodes (35): app_engine(), email_of(), jobs_for(), app(), client(), complete(), request_reset(), sessions_of() (+27 more)

### Community 57 - "Decline Message Tests"
Cohesion: 0.19
Nodes (31): clear_jobs(), app(), body_of(), clean_outbox(), decline(), message_of(), owner(), _published() (+23 more)

### Community 58 - "Tenant Tests"
Cohesion: 0.15
Nodes (32): concurrent(), b(), drop_tenants(), routes(), slug_of(), tag(), test_a_business_country_must_be_two_uppercase_letters(), test_a_business_currency_must_be_three_uppercase_letters() (+24 more)

### Community 62 - "Turnstile Tests"
Cohesion: 0.15
Nodes (26): verify(), json_answer(), never_called(), _no_secret_by_default(), test_a_configured_secret_is_checked_even_when_turnstile_is_disabled(), fake_urlopen(), test_a_malformed_disabled_flag_is_an_error_not_a_quiet_refusal(), test_a_missing_or_over_long_token_never_reaches_the_network() (+18 more)

### Community 64 - "Web Tracing"
Cohesion: 0.09
Nodes (23): enabled(), endProxySpan(), provider(), startProxySpan(), traceparent(), eslintConfig, engines, node (+15 more)

### Community 68 - "Body Size Cap Tests"
Cohesion: 0.14
Nodes (25): app(), client(), limit(), padded(), test_a_body_of_exactly_the_cap_reaches_validation(), test_a_chunked_oversize_body_is_a_413(), chunks(), test_a_declared_oversize_body_is_a_413_with_the_standard_headers() (+17 more)

### Community 69 - "Rate Limits"
Cohesion: 0.11
Nodes (23): email_key(), hit(), ip_key(), action(), test_a_new_window_starts_counting_again(), test_addresses_that_do_not_parse_share_one_key(), test_an_attempt_counts_even_when_the_request_fails(), test_an_email_key_does_not_hold_the_address() (+15 more)

### Community 70 - "Owner Keeping Tests"
Cohesion: 0.15
Nodes (22): add_membership(), add_user(), set_role(), test_a_non_read_committed_change_is_refused(), test_changing_the_only_owner_is_refused(), test_tearing_down_a_whole_business_in_one_statement_is_not_a_last_owner_violation(), test_the_function_and_privileges_the_trigger_needs(), test_the_trigger_lock_lets_a_new_membership_through() (+14 more)

### Community 72 - "Audit DB Tests"
Cohesion: 0.19
Nodes (23): events(), in_business(), record(), reviewed(), target(), test_a_business_sees_only_its_own_events(), test_a_reused_connection_records_an_event_for_no_business(), test_an_event_cannot_be_written_for_another_business() (+15 more)

### Community 75 - "Settings API Tests"
Cohesion: 0.17
Nodes (22): saved_settings(), app(), test_a_change_leaves_the_other_settings_as_they_were(), test_a_saved_setting_can_be_changed_and_set_back_to_its_default(), test_a_setting_of_the_wrong_kind_is_refused(), test_a_stored_setting_no_longer_in_the_registry_is_ignored(), test_a_stored_setting_that_no_longer_fits_fails_loudly(), test_an_old_timezone_name_is_kept_as_sent() (+14 more)

### Community 76 - "Backend Tracing Setup"
Cohesion: 0.14
Nodes (21): configure(), _enabled(), _meter_provider(), _provider(), _resource(), test_service_version_defaults_to_the_app_version(), test_t11_provider_names_the_service(), test_t11_worker_and_migrations_configure_their_own_service_name() (+13 more)

### Community 78 - "API Proxy Upstream"
Cohesion: 0.11
Nodes (8): parsedLogLines(), waitForLog(), LOCALE_MATCHER, MAX_BODY_BYTES, localeRegex, ref_node_http, ref_node_net, ref_node_zlib

### Community 8 - "Booking Approval Tests"
Cohesion: 0.08
Nodes (83): save_setting(), _published(), ago(), app(), confirmed_in_the_past(), events_of(), expire(), expire_in_a_second() (+75 more)

### Community 82 - "Seed Tests"
Cohesion: 0.21
Nodes (17): edges(), local(), local_date(), nows(), past_ends(), test_near_role_is_ahead_but_under_a_day(), test_roles_hold_across_clock_changes(), test_today_role_at_the_edges_of_the_day() (+9 more)

### Community 83 - "Booking Sweep Tests"
Cohesion: 0.27
Nodes (17): sweep(), due(), events_of(), seed(), status_of(), test_a_due_awaiting_payment_expires_as_well(), test_a_live_pending_and_a_confirmed_booking_are_left_alone(), test_a_second_sweep_expires_nothing_and_writes_no_events() (+9 more)

### Community 84 - "Settings Audit Tests"
Cohesion: 0.25
Nodes (17): app(), changes(), test_a_change_that_fails_after_its_event_records_nothing(), test_a_change_whose_event_fails_is_not_saved(), test_a_changed_setting_is_recorded_with_its_old_and_new_value(), test_a_second_change_records_what_it_replaced(), test_a_setting_saved_as_it_already_was_is_not_recorded(), test_an_event_that_changed_nothing_has_no_details_at_all() (+9 more)

### Community 86 - "Repo Check Scripts"
Cohesion: 0.16
Nodes (11): languageOf(), unprefixedNames(), reservedWords(), topLevelRoutes(), unreservedRoutes(), ALLOWED, BUILD_ARGS, READERS (+3 more)

### Community 87 - "Tenant Schema Docs"
Cohesion: 0.19
Nodes (14): broken_tables(), test_schema_has_no_tenant_isolation_violations(), test_the_check_catches_plain_foreign_keys_and_unisolated_tables(), violations(), enable_tenant_isolation (app.db), keep_an_owner trigger, members.member_user(lock=True), tenants root table (+6 more)

### Community 88 - "Private Log Tests"
Cohesion: 0.17
Nodes (14): mailed(), run_until_idle(), client_for(), test_no_personal_data_ever_reaches_a_log(), cookie_secret(), secret(), token_secret(), Any (+6 more)

### Community 89 - "Audit Events API Tests"
Cohesion: 0.28
Nodes (14): add_event(), app(), test_a_page_holds_one_to_a_hundred_events(), test_an_owner_reads_only_their_business_newest_first(), test_an_owner_sees_the_address_and_browser_only_of_their_own_events(), test_only_an_owner_reads_the_log(), test_the_log_comes_in_pages_before_an_event(), test_the_log_needs_a_session() (+6 more)

### Community 9 - "Service Workers Tests"
Cohesion: 0.08
Nodes (83): app(), b_service(), create(), ids(), owner(), pairs(), put(), svc() (+75 more)

### Community 92 - "Mail Rendering"
Cohesion: 0.15
Nodes (10): ics(), _outbox(), test_ics_folds_by_octet_and_escapes_text(), test_ics_lines_and_stamps(), test_ics_strips_control_characters_from_summary(), datetime, Session, UUID (+2 more)

### Community 93 - "Web Proxy Middleware"
Cohesion: 0.21
Nodes (12): forwardApi(), proxy(), readBody(), unavailable(), config, FORWARDING, HOP_BY_HOP, localize (+4 more)

### Community 94 - "Catalog And Hook Checks"
Cohesion: 0.22
Nodes (10): blankBranches(), compareCatalogs(), kind(), placeholders(), CI job: Repo checks, No build-time NEXT_PUBLIC_* env vars check, check(), commit-msg.test.sh script (+2 more)

### Community 98 - "Blank Names Tests"
Cohesion: 0.38
Nodes (9): test_a_name_in_any_script_is_accepted(), test_a_name_that_renders_blank_or_has_no_letter_or_digit_is_refused(), test_an_emoji_only_business_name_at_sign_up_is_a_422(), test_free_text_keeps_emoji_punctuation_and_a_blank_glyph_between_words(), test_free_text_that_renders_blank_is_refused(), test_multiline_text_keeps_a_spacer_line_between_real_lines(), Any, parametrize (+1 more)

### Community 102 - "Renovate And Commit Hooks"
Cohesion: 0.22
Nodes (7): Shared workflow pr-title.yml@v1, commit-msg script, extends, packageRules, $schema, github>fjcloudaiconsulting/.github#v1, PR title workflow

### Community 110 - "Readme Setup Docs"
Cohesion: 0.33
Nodes (6): Conventional Commits PR titles, GHCR images backend/frontend/migrations, release-please, Release PR (chore(main): release X.Y.Z), docker-compose-prod.yaml, Shared release workflows (fjcloudaiconsulting/.github RELEASE_CONTRACT.md)

### Community 113 - "Community 113"
Cohesion: 0.40
Nodes (4): smoke db-init service (profile migrate), db-init service (bootstrap.sql role creation, idempotent), prod migrate init container, CI job: API (ruff, mypy, pytest against Postgres service)

### Community 114 - "Community 114"
Cohesion: 0.40
Nodes (5): CI job: image build matrix (backend, migrations, frontend) as sha-<7>, CI job: promote release images, Shared workflow build-image.yml@v1, Shared workflow promote-release.yml@v1, CI concurrency: PR runs cancel, main runs keyed by sha

### Community 115 - "Community 115"
Cohesion: 0.50
Nodes (3): CI job: Release PR and tag (release-please), packages, $schema

### Community 85 - "Contributor Docs"
Cohesion: 0.12
Nodes (18): app.availability.BOOKED / booked() / OCCUPYING, bookings.sweep worker expiry, app.clients.find_or_create, member_slots (single bookability validator), Alembic single-head migration chain, booking_holds (guest held times), clients (business-owned client records), ex_bookings_worker_overlap exclusion constraint (+10 more)

### Community 91 - "Docs Conventions"
Cohesion: 0.16
Nodes (14): JobKind(handler, timeout, grace), jobs table (app/jobs.py), app.logs.configure() structured logging, tenant_context(tenant_id), app.tracing hand-written spans, email.booking job (ZIF-53), Shared local Grafana LGTM stack, /api/public routes (+6 more)

### Community 99 - "Project Docs"
Cohesion: 0.24
Nodes (10): auth.record, app.clients.CONSENT_TEXTS, join_tenant(session, tenant_id), audit_events audit log, booking_events append-only table, Production path Cloudflare -> Traefik -> web -> API (k3s), consents append-only table, ZIF_CLIENT_IP_HEADER (+2 more)

### Community 101 - "Frontend Dependencies"
Cohesion: 0.22
Nodes (9): dependencies, next, next-intl, @opentelemetry/api, @opentelemetry/exporter-trace-otlp-http, @opentelemetry/resources, @opentelemetry/sdk-trace-base, react (+1 more)

### Community 104 - "Frontend Build Deps"
Cohesion: 0.25
Nodes (8): devDependencies, eslint, eslint-config-next, @hey-api/openapi-ts, @types/node, @types/react, @types/react-dom, typescript

### Community 105 - "Frontend Test Deps"
Cohesion: 0.25
Nodes (8): scripts, build, dev, generate, lint, start, test, typecheck

### Community 106 - "Landing Dependencies"
Cohesion: 0.25
Nodes (7): name, private, scripts, build, dev, test, type

### Community 79 - "Frontend TS Config"
Cohesion: 0.10
Nodes (19): compilerOptions, allowImportingTsExtensions, allowJs, esModuleInterop, incremental, isolatedModules, jsx, lib (+11 more)

### Community 118 - "Community 118"
Cohesion: 0.67
Nodes (3): Brand Red #b31513, Z Logo Mark (white stroked Z glyph), Ziftbook App Icon (favicon)

### Community 119 - "Community 119"
Cohesion: 0.67
Nodes (3): Brand Color #b31513 (Ziftbook red), Z Monogram Logo Mark, Landing Favicon (red rounded square with white Z)

### Community 38 - "Landing And Mail Templates"
Cohesion: 0.08
Nodes (44): Booking flow preview card (service, time, done), Features section for clients and businesses, Landing page HTML template, Language switcher popover menu, Legal footer: FJ Cloud & AI Consulting, KVK 42010737, Young Serif font preload, Mail delivery check, Email localization (en, nl, pt) (+36 more)

### Community 46 - "Cancellation Mail Templates"
Cohesion: 0.12
Nodes (38): Booking Cancelled by Client confirmation email (to client), Booking Cancelled by Business email (to client), Client Cancelled notification email (to business), Client Rescheduled notification email (to business), Booking Confirmed email (to client), Booking Request Declined with Message email (to client), Booking Request Declined email (to client), Booking slot block ($service, $date, $time, $zone) (+30 more)

### Community 59 - "Booking Mail Templates"
Cohesion: 0.17
Nodes (32): Team member booking assignment, Template first line is email subject, Booking request received (guest) email, Guest self-service view/change/cancel link, Booking reminder (guest) email, Booking moved (team) email, Booking request (owner) email, Booking request approval with expiry deadline (+24 more)

## Ambiguous Edges - Review These
- `Landing page HTML template` → `Schibsted Grotesk font (landing)`  [AMBIGUOUS]
  landing/template.html · relation: references
- `SecLists` → `Sign out on every device after password reset`  [AMBIGUOUS]
  backend/licenses/seclists-MIT.txt · relation: conceptually_related_to

## Knowledge Gaps
- **259 isolated node(s):** `Note`, `Pick`, `View`, `Message`, `Stage` (+254 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 1256 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **99 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Landing page HTML template` and `Schibsted Grotesk font (landing)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **What is the exact relationship between `SecLists` and `Sign out on every device after password reset`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `app.logs.configure() structured logging` connect `Docs Conventions` to `Test Fixtures Conftest`?**
  _High betweenness centrality (0.288) - this node is a cross-community bridge._
- **Why does `app.tracing hand-written spans` connect `Docs Conventions` to `Web Proxy Middleware`?**
  _High betweenness centrality (0.288) - this node is a cross-community bridge._
- **Why does `next-intl` connect `Shared UI And Manage Booking` to `Web Tracing`, `Console Calendar Logic`, `Public Booking Page UI`, `Team And Today Console`, `Time Off Console UI`, `Console Today Data`, `Settings Console UI`, `App Shell And Header`, `Week Editor UI`, `Services Console UI`, `Web Proxy Middleware`?**
  _High betweenness centrality (0.202) - this node is a cross-community bridge._
- **Are the 821 inferred relationships involving `People` (e.g. with `test_a_sign_up_for_a_registered_email_says_to_sign_in_without_a_link()` and `test_a_failed_send_keeps_the_request_for_the_retry()`) actually correct?**
  _`People` has 821 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Note`, `Pick`, `View` to the rest of the system?**
  _259 weakly-connected nodes found - possible documentation gaps or missing edges._