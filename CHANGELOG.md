# Changelog

## [0.30.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.29.0...v0.30.0) (2026-10-22)


### Features

* **frontend:** settings use the desktop width, a section's name and purpose beside its fields (ZIF-163) ([#319](https://github.com/fjcloudaiconsulting/ziftbook/issues/319)) ([5fc89f3](https://github.com/fjcloudaiconsulting/ziftbook/commit/5fc89f30eb5e9e6792d183eccea6d9fccd2ca6fc))

## [0.29.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.28.0...v0.29.0) (2026-10-22)


### Features

* **frontend:** the dev version line reads v0.28.0-dev, so dev never looks like a stable release (ZIF-158) ([#318](https://github.com/fjcloudaiconsulting/ziftbook/issues/318)) ([499438d](https://github.com/fjcloudaiconsulting/ziftbook/commit/499438d15794a48b5d947b279af60fa5e7cae2e5))


### Bug Fixes

* **deps:** update python non-major ([#269](https://github.com/fjcloudaiconsulting/ziftbook/issues/269)) ([1aa103b](https://github.com/fjcloudaiconsulting/ziftbook/commit/1aa103bba02c2d6fb1ec24f7790a59edd0ff06ca))
* **frontend:** the card check loads its payment fields again on Stripe.js 10 (ZIF-158) ([#317](https://github.com/fjcloudaiconsulting/ziftbook/issues/317)) ([f364d89](https://github.com/fjcloudaiconsulting/ziftbook/commit/f364d89f36b88a44d48a951f1f5dfeac3fd96ad3))
* the API gets ZIF_APP_URL too, so Stripe's onboarding returns to the web app (ZIF-158) ([#316](https://github.com/fjcloudaiconsulting/ziftbook/issues/316)) ([6e15218](https://github.com/fjcloudaiconsulting/ziftbook/commit/6e152188906fb571e2f28034509583fd12ed7191))

## [0.28.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.27.0...v0.28.0) (2026-10-21)


### Features

* **frontend:** show the app version on dev (ZIF-158) ([#312](https://github.com/fjcloudaiconsulting/ziftbook/issues/312)) ([b3a99d3](https://github.com/fjcloudaiconsulting/ziftbook/commit/b3a99d31dcb20138e43e344fb3d561d920a46834))


### Bug Fixes

* **backend:** create Stripe accounts on the preview API version (ZIF-158) ([#309](https://github.com/fjcloudaiconsulting/ziftbook/issues/309)) ([315a9c5](https://github.com/fjcloudaiconsulting/ziftbook/commit/315a9c59fe1dc64ea2eaeaca28bc65320f4d2730))

## [0.27.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.26.1...v0.27.0) (2026-10-21)


### Features

* **backend:** checkout checks the client's card (ZIF-158) ([#302](https://github.com/fjcloudaiconsulting/ziftbook/issues/302)) ([5b88860](https://github.com/fjcloudaiconsulting/ziftbook/commit/5b888601d2356878beb358fb6ab607b0242d6afe))
* **backend:** clients pay in advance, refunds follow the terms (ZIF-158) ([#304](https://github.com/fjcloudaiconsulting/ziftbook/issues/304)) ([3abf303](https://github.com/fjcloudaiconsulting/ziftbook/commit/3abf303661719e99990e285c26fbae470740c830))
* **backend:** every booking keeps the name, phone and message the client typed (ZIF-157) ([#292](https://github.com/fjcloudaiconsulting/ziftbook/issues/292)) ([e221ea4](https://github.com/fjcloudaiconsulting/ziftbook/commit/e221ea4e67b24063f5ba13443cf19bef8482e1e6))
* **backend:** guests book straight from the booking page, without an emailed link (ZIF-157) ([#293](https://github.com/fjcloudaiconsulting/ziftbook/issues/293)) ([2b98195](https://github.com/fjcloudaiconsulting/ziftbook/commit/2b9819579decc8034ff9bdcc1470567b07425035))
* **backend:** owners connect their business to Stripe (ZIF-158) ([#300](https://github.com/fjcloudaiconsulting/ziftbook/issues/300)) ([d201eef](https://github.com/fjcloudaiconsulting/ziftbook/commit/d201eefe259c513432b525aa41d7b2545caac866))
* clients move a booking request until it is confirmed (ZIF-157) ([#294](https://github.com/fjcloudaiconsulting/ziftbook/issues/294)) ([3d20809](https://github.com/fjcloudaiconsulting/ziftbook/commit/3d20809264f80c9dd171ac97fa56e361e6f59437))
* **frontend:** bookings show what was paid and refunded (ZIF-158) ([#306](https://github.com/fjcloudaiconsulting/ziftbook/issues/306)) ([d0815ee](https://github.com/fjcloudaiconsulting/ziftbook/commit/d0815ee274d0e1c7c38ba13e24bcd07ed4c85351))
* **frontend:** clients book from a service dialog and a checkout page (ZIF-157) ([#297](https://github.com/fjcloudaiconsulting/ziftbook/issues/297)) ([62bbf7d](https://github.com/fjcloudaiconsulting/ziftbook/commit/62bbf7d41a008b1e037a955d5890797ae7a0fa06))
* **frontend:** owners connect to Stripe from settings (ZIF-158) ([#303](https://github.com/fjcloudaiconsulting/ziftbook/issues/303)) ([679be82](https://github.com/fjcloudaiconsulting/ziftbook/commit/679be824778c078f915ffd0d7273db79803ddb15))
* **frontend:** the phone country picker shows flags and searches by country or code (ZIF-157) ([#298](https://github.com/fjcloudaiconsulting/ziftbook/issues/298)) ([0a14ce4](https://github.com/fjcloudaiconsulting/ziftbook/commit/0a14ce4c2118fbf35d4b1e6c885fd0f00ee199ce))


### Bug Fixes

* **backend:** a refund Stripe refuses is flagged for the business once, not retried every hour (ZIF-158) ([#307](https://github.com/fjcloudaiconsulting/ziftbook/issues/307)) ([0f72630](https://github.com/fjcloudaiconsulting/ziftbook/commit/0f726302d8c7ac84bf14df9b60adb68054712e4e))
* **backend:** the seed sends a phone with every guest booking again ([#295](https://github.com/fjcloudaiconsulting/ziftbook/issues/295)) ([1be5c1a](https://github.com/fjcloudaiconsulting/ziftbook/commit/1be5c1a67846fe88fe4a64279de04f0bf908ce8b))
* **frontend:** admins can sign out again on prod ([#289](https://github.com/fjcloudaiconsulting/ziftbook/issues/289)) ([abddc01](https://github.com/fjcloudaiconsulting/ziftbook/commit/abddc01902dfee6901d82215e1488d9710504f22))
* **frontend:** counts of one read right in every language ([#296](https://github.com/fjcloudaiconsulting/ziftbook/issues/296)) ([9dee7c9](https://github.com/fjcloudaiconsulting/ziftbook/commit/9dee7c935cf6bb5e325737ce74584b1594c2f7ed))

## [0.26.1](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.26.0...v0.26.1) (2026-10-18)


### Bug Fixes

* **backend:** the backend image starts again; the S3 type stubs are imported for type checking only (ZIF-133) ([#287](https://github.com/fjcloudaiconsulting/ziftbook/issues/287)) ([25681e4](https://github.com/fjcloudaiconsulting/ziftbook/commit/25681e47305bd106635fdfb1987d023f02e60697))

## [0.26.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.25.0...v0.26.0) (2026-10-18)


### Features

* **backend:** owners upload a service photo, checked and resized before it is linked (ZIF-133) ([#279](https://github.com/fjcloudaiconsulting/ziftbook/issues/279)) ([aed5670](https://github.com/fjcloudaiconsulting/ziftbook/commit/aed56701af8d664536e6274fa5e81dfe0ee9c6f7))
* **backend:** the worker prunes jobs finished more than 30 days ago (ZIF-35) ([#267](https://github.com/fjcloudaiconsulting/ziftbook/issues/267)) ([6c9ca48](https://github.com/fjcloudaiconsulting/ziftbook/commit/6c9ca482dee4226ab300489007f614ddeeba1268))
* every service shows a placeholder picture, picked from its name or by the owner (ZIF-133) ([#276](https://github.com/fjcloudaiconsulting/ziftbook/issues/276)) ([327b465](https://github.com/fjcloudaiconsulting/ziftbook/commit/327b465965040ce7512490f8036186a27477bfc7))
* **frontend:** a mistyped address shows a "page not found" in the visitor's language (ZIF-148) ([#273](https://github.com/fjcloudaiconsulting/ziftbook/issues/273)) ([9ea5558](https://github.com/fjcloudaiconsulting/ziftbook/commit/9ea55584165982ae099c6f5a5ab6d58528c08411))
* **frontend:** clients can read ziftbook's Terms and Privacy pages (ZIF-147) ([#275](https://github.com/fjcloudaiconsulting/ziftbook/issues/275)) ([c796d3c](https://github.com/fjcloudaiconsulting/ziftbook/commit/c796d3c2ef7f4fed30d42d40739a246e0a62e60e))
* **frontend:** owners add, replace and remove a service photo, and every list shows it (ZIF-133) ([#281](https://github.com/fjcloudaiconsulting/ziftbook/issues/281)) ([8a88674](https://github.com/fjcloudaiconsulting/ziftbook/commit/8a88674589a664a2c91ceb6f604a4ba196fcf971))
* **frontend:** the guest reschedule page uses the booking page's day and time picker (ZIF-149) ([#277](https://github.com/fjcloudaiconsulting/ziftbook/issues/277)) ([a452e7e](https://github.com/fjcloudaiconsulting/ziftbook/commit/a452e7ea1e8fb19ecd81f6019ad1a9fe00071394))
* online bookings need a phone, checked and stored in one international format (ZIF-152) ([#272](https://github.com/fjcloudaiconsulting/ziftbook/issues/272)) ([3f0ae29](https://github.com/fjcloudaiconsulting/ziftbook/commit/3f0ae29eb93787f819963c710c0504d967afc480))


### Bug Fixes

* **backend:** a failing healthcheck ping never stops the worker (ZIF-21) ([#271](https://github.com/fjcloudaiconsulting/ziftbook/issues/271)) ([53a5a12](https://github.com/fjcloudaiconsulting/ziftbook/commit/53a5a12d3e3b03de38c3c35c072c8a60d19c075b))
* **frontend:** keyboard focus scrolls clear of the console's sticky bars (ZIF-144) ([#278](https://github.com/fjcloudaiconsulting/ziftbook/issues/278)) ([46be628](https://github.com/fjcloudaiconsulting/ziftbook/commit/46be628696ac9ebd1460ae478bf7f685c6b7eaf9))
* **frontend:** the dev and build logs drop Edge-runtime and extension noise (ZIF-141) ([#268](https://github.com/fjcloudaiconsulting/ziftbook/issues/268)) ([f596766](https://github.com/fjcloudaiconsulting/ziftbook/commit/f5967660cf876d68e2753fd03d11975f5b735287))
* **frontend:** the privacy notice names server logs at Grafana Labs and service photos at Cloudflare (ZIF-89) ([#283](https://github.com/fjcloudaiconsulting/ziftbook/issues/283)) ([d5bb154](https://github.com/fjcloudaiconsulting/ziftbook/commit/d5bb15462684fb04fd49fb53435460e112d58c91))
* **frontend:** the Terms and Privacy pages name FJ Cloud & AI Consulting and its details (ZIF-147) ([#285](https://github.com/fjcloudaiconsulting/ziftbook/issues/285)) ([9ebc315](https://github.com/fjcloudaiconsulting/ziftbook/commit/9ebc315d9220621ea96c1a44cc30380ea0237936))

## [0.25.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.24.1...v0.25.0) (2026-10-15)


### Features

* **backend:** admins need a second factor (ZIF-71) ([#259](https://github.com/fjcloudaiconsulting/ziftbook/issues/259)) ([35e0d15](https://github.com/fjcloudaiconsulting/ziftbook/commit/35e0d150fa2b927e9a6cd83c4cbc537a348f8a3f))
* **backend:** first-run setup creates the platform admin with a deployment token (ZIF-70) ([#256](https://github.com/fjcloudaiconsulting/ziftbook/issues/256)) ([1053e39](https://github.com/fjcloudaiconsulting/ziftbook/commit/1053e39c3e6526b7e8a9071fd4c1aab6ccd34cb1))
* **backend:** platform admin role and grant/revoke CLI (ZIF-70) ([#253](https://github.com/fjcloudaiconsulting/ziftbook/issues/253)) ([b413b8b](https://github.com/fjcloudaiconsulting/ziftbook/commit/b413b8b4c6c05ba906175bcc06fdf5f6dfc9eaca))
* **backend:** platform admins sign in to their own short-lived session (ZIF-70) ([#255](https://github.com/fjcloudaiconsulting/ziftbook/issues/255)) ([d5a3af7](https://github.com/fjcloudaiconsulting/ziftbook/commit/d5a3af75ab9a1be6711a35b180c2cbed9f6b69d3))
* **backend:** reserve the setup slug for first-run setup (ZIF-70) ([#252](https://github.com/fjcloudaiconsulting/ziftbook/issues/252)) ([04a44a5](https://github.com/fjcloudaiconsulting/ziftbook/commit/04a44a5f16cdf71af0ebcaba5182e0bd20dcd0bc))
* **backend:** TOTP credentials and the enrol CLI (ZIF-71) ([#258](https://github.com/fjcloudaiconsulting/ziftbook/issues/258)) ([1656f9b](https://github.com/fjcloudaiconsulting/ziftbook/commit/1656f9b532080e6f61438fa05882475cf2c83473))
* **frontend:** the web app's API proxy and page errors as OTLP metrics (ZIF-88) ([#229](https://github.com/fjcloudaiconsulting/ziftbook/issues/229)) ([be17dbf](https://github.com/fjcloudaiconsulting/ziftbook/commit/be17dbfdf8e99ff14e72700575d73766a6e7aa0b))
* **web:** admin sign-in with two-factor (ZIF-71) ([#261](https://github.com/fjcloudaiconsulting/ziftbook/issues/261)) ([2999630](https://github.com/fjcloudaiconsulting/ziftbook/commit/29996302c1711f2addf873317a2d7f328e9f402d))
* **web:** first-run /setup page and redirect (ZIF-70) ([#262](https://github.com/fjcloudaiconsulting/ziftbook/issues/262)) ([1f59733](https://github.com/fjcloudaiconsulting/ziftbook/commit/1f597336b25ce668af442da85ab195ec8499faf1))


### Bug Fixes

* **deps:** update dependency fastapi to v0.143.1 ([#260](https://github.com/fjcloudaiconsulting/ziftbook/issues/260)) ([cc209a5](https://github.com/fjcloudaiconsulting/ziftbook/commit/cc209a58b45901a171eb698b6fd0f7b8c64a757b))
* **deps:** update dependency pydantic-settings to v2.16.0 ([#257](https://github.com/fjcloudaiconsulting/ziftbook/issues/257)) ([40e33c9](https://github.com/fjcloudaiconsulting/ziftbook/commit/40e33c90fb2a8eebd3130407c5ebea32ce6775cd))
* **migrations:** a migration that can't get a table lock fails in 5s instead of hanging the rollout (ZIF-34) ([#246](https://github.com/fjcloudaiconsulting/ziftbook/issues/246)) ([76c6cbc](https://github.com/fjcloudaiconsulting/ziftbook/commit/76c6cbcb44568d30664a9b64cb179759463843e3))

## [0.24.1](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.24.0...v0.24.1) (2026-10-12)


### Bug Fixes

* **deps:** update python non-major ([#232](https://github.com/fjcloudaiconsulting/ziftbook/issues/232)) ([3153e66](https://github.com/fjcloudaiconsulting/ziftbook/commit/3153e6649a5328aaddaef30df62fd00488a86678))

## [0.24.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.23.0...v0.24.0) (2026-10-10)


### Features

* **backend:** jobs, email, database pool and business metrics over OTLP (ZIF-88) ([#228](https://github.com/fjcloudaiconsulting/ziftbook/issues/228)) ([ce3e0f5](https://github.com/fjcloudaiconsulting/ziftbook/commit/ce3e0f56b566c42a2b5719503eac9f7169a9e522))


### Bug Fixes

* **backend:** a shift inside the hour DST skips is no longer relocated (ZIF-113) ([#221](https://github.com/fjcloudaiconsulting/ziftbook/issues/221)) ([227c5b0](https://github.com/fjcloudaiconsulting/ziftbook/commit/227c5b09b8a4298fe137b03f8be1207d1a47e0ac))
* **backend:** public bookings fail closed when no Turnstile secret is set (ZIF-116) ([#231](https://github.com/fjcloudaiconsulting/ziftbook/issues/231)) ([605db71](https://github.com/fjcloudaiconsulting/ziftbook/commit/605db7126a415e31433a6881b1bf59da24958a90))
* **deps:** update dependency fastapi to v0.143.0 ([#199](https://github.com/fjcloudaiconsulting/ziftbook/issues/199)) ([83a195f](https://github.com/fjcloudaiconsulting/ziftbook/commit/83a195fb721f789d724885c581b35ee0b615e651))
* **deps:** update npm non-major ([#202](https://github.com/fjcloudaiconsulting/ziftbook/issues/202)) ([0429ce4](https://github.com/fjcloudaiconsulting/ziftbook/commit/0429ce4959d28537e409f5d8400d84c938a04343))
* **deps:** update opentelemetry ([#203](https://github.com/fjcloudaiconsulting/ziftbook/issues/203)) ([c462380](https://github.com/fjcloudaiconsulting/ziftbook/commit/c46238094bfddf68b62d4e226523d0deefc95afb))
* **deps:** update python non-major ([#200](https://github.com/fjcloudaiconsulting/ziftbook/issues/200)) ([7ef0f7e](https://github.com/fjcloudaiconsulting/ziftbook/commit/7ef0f7e0beb16afe66a983c51efec7e1e7ca6f7d))
* **frontend:** an API request body over 64 KiB gets a 413 and never reaches the API truncated (ZIF-136) ([#222](https://github.com/fjcloudaiconsulting/ziftbook/issues/222)) ([a46d383](https://github.com/fjcloudaiconsulting/ziftbook/commit/a46d383ebd42e24b72ae8e541d09d79708af87c0))
* **frontend:** the Copy button shows Copied after copying the booking page address ([#224](https://github.com/fjcloudaiconsulting/ziftbook/issues/224)) ([6e888a6](https://github.com/fjcloudaiconsulting/ziftbook/commit/6e888a69af2c85aba25f05a85246133fbae02af9))

## [0.23.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.22.0...v0.23.0) (2026-10-08)


### Features

* **backend:** availability says which workers can take each slot (ZIF-100) ([#198](https://github.com/fjcloudaiconsulting/ziftbook/issues/198)) ([2dcadaf](https://github.com/fjcloudaiconsulting/ziftbook/commit/2dcadaf35ae34414ad09761287b67d24c0906c3f))
* **backend:** hold a guest's time until they confirm the emailed link (ZIF-117) ([#218](https://github.com/fjcloudaiconsulting/ziftbook/issues/218)) ([f3531fa](https://github.com/fjcloudaiconsulting/ziftbook/commit/f3531fa278f1e152f55c7aabb4d7d4971e56ad09))
* **backend:** make seed fills the local app with synthetic businesses, people and bookings (ZIF-142) ([#194](https://github.com/fjcloudaiconsulting/ziftbook/issues/194)) ([d1074d5](https://github.com/fjcloudaiconsulting/ziftbook/commit/d1074d54dd3ff6b1d5b854344752aff24ca4253a))
* **backend:** owners answer booking requests unless the business lets team members (ZIF-143) ([#208](https://github.com/fjcloudaiconsulting/ziftbook/issues/208)) ([2c7781b](https://github.com/fjcloudaiconsulting/ziftbook/commit/2c7781bf80da9c53d6db08378ec7ba9c17913e2d))
* **backend:** send mail through the Mailgun HTTP API, never SMTP (ZIF-151) ([#211](https://github.com/fjcloudaiconsulting/ziftbook/issues/211)) ([60d3249](https://github.com/fjcloudaiconsulting/ziftbook/commit/60d3249d4453138c67fdd72ab07b70d16210cad2))
* **frontend:** booking page clients pick a time and see who is free (ZIF-100) ([#206](https://github.com/fjcloudaiconsulting/ziftbook/issues/206)) ([e2d76ec](https://github.com/fjcloudaiconsulting/ziftbook/commit/e2d76ec21ee9f82d9d77f4ba61d1f57ad0404612))
* **frontend:** calendar shows 7 days from today, or Mon-Sun, and remembers the view (ZIF-143) ([#204](https://github.com/fjcloudaiconsulting/ziftbook/issues/204)) ([4d42428](https://github.com/fjcloudaiconsulting/ziftbook/commit/4d42428e764ee2f44bdaa26c74ebf906e5d99fc1))
* **frontend:** change a person's name and role from their page (ZIF-126) ([#214](https://github.com/fjcloudaiconsulting/ziftbook/issues/214)) ([6a511cf](https://github.com/fjcloudaiconsulting/ziftbook/commit/6a511cf405a388d35e8fb75a31a11aec08bd174e))
* **frontend:** edit a service's description and gap (ZIF-126) ([#215](https://github.com/fjcloudaiconsulting/ziftbook/issues/215)) ([b72313f](https://github.com/fjcloudaiconsulting/ziftbook/commit/b72313fdec875380d95b23417ebc70b9439b6a46))
* **frontend:** guests confirm the emailed link before their booking exists (ZIF-117) ([#220](https://github.com/fjcloudaiconsulting/ziftbook/issues/220)) ([c8fbf83](https://github.com/fjcloudaiconsulting/ziftbook/commit/c8fbf832682daa045818ce49ec76ef7e48b0a3e2))
* **frontend:** team members see their requests read only unless the business lets them answer (ZIF-143) ([#209](https://github.com/fjcloudaiconsulting/ziftbook/issues/209)) ([cf594bc](https://github.com/fjcloudaiconsulting/ziftbook/commit/cf594bc44c6335c5ac5c4ebec9be98cb1ec06f33))
* **frontend:** working hours start from the shop's opening hours in one click (ZIF-134) ([#207](https://github.com/fjcloudaiconsulting/ziftbook/issues/207)) ([8979e1f](https://github.com/fjcloudaiconsulting/ziftbook/commit/8979e1f18b4f1353e3bfdd8da61cb1e0c8d50e73))
* **services:** show when a service was archived (ZIF-126) ([#213](https://github.com/fjcloudaiconsulting/ziftbook/issues/213)) ([820b967](https://github.com/fjcloudaiconsulting/ziftbook/commit/820b967970fc4db7b83753b1e8941d61a339b300))
* **time-off:** blocking time over an appointment is refused and names it (ZIF-130) ([#212](https://github.com/fjcloudaiconsulting/ziftbook/issues/212)) ([c843f9e](https://github.com/fjcloudaiconsulting/ziftbook/commit/c843f9ed23aa9da24cdfea3f5de9fe3592190526))


### Bug Fixes

* **backend:** a stalled lock holder costs a booker a 503, not a hang for every tenant (ZIF-114) ([#217](https://github.com/fjcloudaiconsulting/ziftbook/issues/217)) ([29b370f](https://github.com/fjcloudaiconsulting/ziftbook/commit/29b370f58993e9c895966fbbf2f0856c60f96584))
* **backend:** auto-confirmed bookings count toward the per-address cap (ZIF-115) ([#216](https://github.com/fjcloudaiconsulting/ziftbook/issues/216)) ([712cfb9](https://github.com/fjcloudaiconsulting/ziftbook/commit/712cfb9809f6a7fda433311453f9fbfe3c96ccef))
* **backend:** cap request bodies at 64 KiB and refuse blank-looking names (ZIF-83) ([#196](https://github.com/fjcloudaiconsulting/ziftbook/issues/196)) ([622fb43](https://github.com/fjcloudaiconsulting/ziftbook/commit/622fb435e5e04c2347de19148521b69c18199b53))
* **deps:** update opentelemetry to v1.45.1 ([#192](https://github.com/fjcloudaiconsulting/ziftbook/issues/192)) ([23e8ef3](https://github.com/fjcloudaiconsulting/ziftbook/commit/23e8ef3654432a2258f0c503ef327da727095a4f))
* **frontend:** reopening a sign-up or reset link in an open tab starts the page over (ZIF-94) ([#195](https://github.com/fjcloudaiconsulting/ziftbook/issues/195)) ([d0a7cef](https://github.com/fjcloudaiconsulting/ziftbook/commit/d0a7cef7ab83d460fa8197d798485ddef746a271))
* **frontend:** week save bar names a refusal that is not about one day, and history comments go (ZIF-127) ([#197](https://github.com/fjcloudaiconsulting/ziftbook/issues/197)) ([4fc440e](https://github.com/fjcloudaiconsulting/ziftbook/commit/4fc440e1e1fb8e8f11bf2eded9477500e316226b))
* **landing:** www redirect moves to the zone rule, Worker runs only for / (INFRA-131) ([#188](https://github.com/fjcloudaiconsulting/ziftbook/issues/188)) ([d024cb4](https://github.com/fjcloudaiconsulting/ziftbook/commit/d024cb44f2abd28de464f9ad817ce9e064310a1f))

## [0.22.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.21.0...v0.22.0) (2026-10-06)


### Features

* **backend:** database health check and live smoke script (INFRA-120) ([#190](https://github.com/fjcloudaiconsulting/ziftbook/issues/190)) ([b1f5db3](https://github.com/fjcloudaiconsulting/ziftbook/commit/b1f5db3d7dac619d7012c42513f56cb025669de6))
* **web:** drop OTLP log export, logs are stdout JSON only (INFRA-118) ([#189](https://github.com/fjcloudaiconsulting/ziftbook/issues/189)) ([e161ee1](https://github.com/fjcloudaiconsulting/ziftbook/commit/e161ee1f335934d0926bb47ad519ddb1df093a23))

## [0.21.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.20.4...v0.21.0) (2026-10-04)


### Features

* **backend:** HTTP metrics on, logs leave on stdout only (INFRA-106) ([#183](https://github.com/fjcloudaiconsulting/ziftbook/issues/183)) ([93a49df](https://github.com/fjcloudaiconsulting/ziftbook/commit/93a49df55dffa3246620751c0a469060bd63545e))

## [0.20.4](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.20.3...v0.20.4) (2026-10-04)


### Bug Fixes

* **landing:** deploy with a Worker-scoped token from the landing environment (INFRA-113) ([#181](https://github.com/fjcloudaiconsulting/ziftbook/issues/181)) ([e507a66](https://github.com/fjcloudaiconsulting/ziftbook/commit/e507a66a3318b02a5bf2b511a5f1a4feb84e8402))

## [0.20.3](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.20.2...v0.20.3) (2026-10-03)


### Bug Fixes

* **ci:** release token carries the workflows permission (INFRA-78) ([#172](https://github.com/fjcloudaiconsulting/ziftbook/issues/172)) ([4e88489](https://github.com/fjcloudaiconsulting/ziftbook/commit/4e884894b9c3fd836c1e1b95c735b3846ccb6471))
* **deps:** update dependency fastapi to v0.142.2 ([#166](https://github.com/fjcloudaiconsulting/ziftbook/issues/166)) ([12547e1](https://github.com/fjcloudaiconsulting/ziftbook/commit/12547e1808203266983dcae7cdeec492ced8fa62))
* **deps:** update dependency next-intl to v4.14.9 ([#152](https://github.com/fjcloudaiconsulting/ziftbook/issues/152)) ([869dc9a](https://github.com/fjcloudaiconsulting/ziftbook/commit/869dc9ac23e9b7fbf8b122181c0570cf72d83ffb))
* **deps:** update dependency sqlalchemy to v2.1.3 ([#167](https://github.com/fjcloudaiconsulting/ziftbook/issues/167)) ([fce1740](https://github.com/fjcloudaiconsulting/ziftbook/commit/fce1740e1a1a3a7b713ed7931c223fda26b42af7))
* **deps:** update dependency tzdata to v2026.5 ([#174](https://github.com/fjcloudaiconsulting/ziftbook/issues/174)) ([7b85401](https://github.com/fjcloudaiconsulting/ziftbook/commit/7b85401b80db8faa1b8b520f70e05e182411714b))
* **deps:** update dependency uvicorn to v0.54.0 ([#168](https://github.com/fjcloudaiconsulting/ziftbook/issues/168)) ([13ecff4](https://github.com/fjcloudaiconsulting/ziftbook/commit/13ecff4070a77fe506b65b3fbcc5d24a9a72b32d))
* **deps:** update opentelemetry-python monorepo to v1.45.0 ([#169](https://github.com/fjcloudaiconsulting/ziftbook/issues/169)) ([04dcd6c](https://github.com/fjcloudaiconsulting/ziftbook/commit/04dcd6c4612a842c2d5adf13e6a19bb36fb63e8c))

## [0.20.2](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.20.1...v0.20.2) (2026-10-03)


### Bug Fixes

* **deps:** update nextjs monorepo to v16.3.8 ([#154](https://github.com/fjcloudaiconsulting/ziftbook/issues/154)) ([8b2e60e](https://github.com/fjcloudaiconsulting/ziftbook/commit/8b2e60ee3c4ce849c82637e66fe4f3999814d9a1))

## [0.20.1](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.20.0...v0.20.1) (2026-10-02)


### Bug Fixes

* **deps:** update dependency psycopg to v3.3.6 ([#153](https://github.com/fjcloudaiconsulting/ziftbook/issues/153)) ([06137a5](https://github.com/fjcloudaiconsulting/ziftbook/commit/06137a53902c36cd37b604340b70d12fe69ebfb7))

## [0.20.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.19.0...v0.20.0) (2026-10-01)


### Features

* **backend:** a declined client reads the business's message in their email (ZIF-121) ([#138](https://github.com/fjcloudaiconsulting/ziftbook/issues/138)) ([d6c8d73](https://github.com/fjcloudaiconsulting/ziftbook/commit/d6c8d735826b079cfd925dbb43b4033ae5a16a20))
* **backend:** the worker settles expired pending bookings on its own (ZIF-122) ([#136](https://github.com/fjcloudaiconsulting/ziftbook/issues/136)) ([b21c3b3](https://github.com/fjcloudaiconsulting/ziftbook/commit/b21c3b39e00134e7062c5a2e1d46dcbfcd468eed))
* everyone gives a name when they create an account (ZIF-131) ([#139](https://github.com/fjcloudaiconsulting/ziftbook/issues/139)) ([90117c6](https://github.com/fjcloudaiconsulting/ziftbook/commit/90117c6e5be32b9667fada839fc125601eb33037))
* **frontend:** owners change their business and booking settings in one place (ZIF-42) ([#140](https://github.com/fjcloudaiconsulting/ziftbook/issues/140)) ([0cb7b25](https://github.com/fjcloudaiconsulting/ziftbook/commit/0cb7b2516a5b33413eae53ca3857d0dd91f97d01))
* **frontend:** owners see their calendar and act on a booking from it (ZIF-57) ([#143](https://github.com/fjcloudaiconsulting/ziftbook/issues/143)) ([30f7b8b](https://github.com/fjcloudaiconsulting/ziftbook/commit/30f7b8b0730b01e5365c0b791b52c116e0f39372))
* **frontend:** owners set cancelling rules and team hours in Settings (ZIF-42) ([#141](https://github.com/fjcloudaiconsulting/ziftbook/issues/141)) ([ad2c78a](https://github.com/fjcloudaiconsulting/ziftbook/commit/ad2c78a5a8df3a76cb2c5273ff82859572805414))
* owners and team book walk-ins, move bookings and block time from the calendar (ZIF-57) ([#145](https://github.com/fjcloudaiconsulting/ziftbook/issues/145)) ([5d3ded7](https://github.com/fjcloudaiconsulting/ziftbook/commit/5d3ded718fab5b06de9f1007ec0dc13096077dc0))
* owners answer requests and see their day on Today (ZIF-57) ([#142](https://github.com/fjcloudaiconsulting/ziftbook/issues/142)) ([1786de6](https://github.com/fjcloudaiconsulting/ziftbook/commit/1786de689c09f16cf1271cf2a470727b0a0cc029))


### Bug Fixes

* **backend:** a client can move their booking into a slot a lapsed request was holding (ZIF-57) ([#144](https://github.com/fjcloudaiconsulting/ziftbook/issues/144)) ([7fe3fc4](https://github.com/fjcloudaiconsulting/ziftbook/commit/7fe3fc4a577535717235473ee7db5f45691437f2))
* **frontend:** land the calendar review fixes left out of [#143](https://github.com/fjcloudaiconsulting/ziftbook/issues/143) (ZIF-57) ([#146](https://github.com/fjcloudaiconsulting/ziftbook/issues/146)) ([710fe47](https://github.com/fjcloudaiconsulting/ziftbook/commit/710fe47dd3814b6c0e73415be8fdfb5f6bc54e67))

## [0.19.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.18.1...v0.19.0) (2026-09-29)


### Features

* **backend:** every business gets a slug and a public booking-page endpoint (ZIF-56) ([#131](https://github.com/fjcloudaiconsulting/ziftbook/issues/131)) ([e7ac572](https://github.com/fjcloudaiconsulting/ziftbook/commit/e7ac572d911a3e6d101a599ab5f21b8acd54e459))
* **frontend:** clients book online on each business's public page (ZIF-56) ([#134](https://github.com/fjcloudaiconsulting/ziftbook/issues/134)) ([86ae8c7](https://github.com/fjcloudaiconsulting/ziftbook/commit/86ae8c77befd9c8e876109adf1bdae4b91101a0d))
* owners publish their booking page when they are ready (ZIF-145) ([#135](https://github.com/fjcloudaiconsulting/ziftbook/issues/135)) ([f4deb7b](https://github.com/fjcloudaiconsulting/ziftbook/commit/f4deb7b9acac7d75d21415fec631dd61d31bacfe))


### Bug Fixes

* **frontend:** the save bar rests on the window edge at the end of a page (ZIF-124) ([#130](https://github.com/fjcloudaiconsulting/ziftbook/issues/130)) ([a1ae5e0](https://github.com/fjcloudaiconsulting/ziftbook/commit/a1ae5e05c76fdd6d6bb901702bb06183e9c0c7a3))

## [0.18.1](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.18.0...v0.18.1) (2026-09-28)


### Bug Fixes

* **frontend:** console buttons sit at their natural width on desktop ([#128](https://github.com/fjcloudaiconsulting/ziftbook/issues/128)) ([1fd483f](https://github.com/fjcloudaiconsulting/ziftbook/commit/1fd483f65e900f15de8cdd4b0bcf80030fce4969))

## [0.18.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.17.1...v0.18.0) (2026-09-28)


### Features

* **backend:** booking emails to clients and merchants, with a reminder and an .ics (ZIF-53) ([#124](https://github.com/fjcloudaiconsulting/ziftbook/issues/124)) ([0dcd03e](https://github.com/fjcloudaiconsulting/ziftbook/commit/0dcd03e0ec97142b7ac410cf04cef813860d7e2f))
* clients view, cancel or reschedule a booking from their email link (ZIF-54) ([#127](https://github.com/fjcloudaiconsulting/ziftbook/issues/127)) ([6164f8a](https://github.com/fjcloudaiconsulting/ziftbook/commit/6164f8a77ec00a21eb89701607df0eabbe3c7487))
* **dev:** export traces to the shared lgtm stack, document every observability switch (ZIF-90) ([#122](https://github.com/fjcloudaiconsulting/ziftbook/issues/122)) ([16ccd21](https://github.com/fjcloudaiconsulting/ziftbook/commit/16ccd211f0630a8b61586f5bf008a0244825fd18))
* **frontend:** the web server logs like the backend (ZIF-86) ([#119](https://github.com/fjcloudaiconsulting/ziftbook/issues/119)) ([238bc58](https://github.com/fjcloudaiconsulting/ziftbook/commit/238bc588ea545267ca657052e92c580c6641d087))
* logs go out over OTLP from the API, worker, migrations and web app (ZIF-137) ([#123](https://github.com/fjcloudaiconsulting/ziftbook/issues/123)) ([0b9a3eb](https://github.com/fjcloudaiconsulting/ziftbook/commit/0b9a3eb08fbcfd4fe34ffc1b4955d42a937f593b))
* traces from the web app through the API, database, jobs and email (ZIF-87) ([#121](https://github.com/fjcloudaiconsulting/ziftbook/issues/121)) ([1cc5aae](https://github.com/fjcloudaiconsulting/ziftbook/commit/1cc5aae574a0a1e1c2102d0246ea74b89e5fb952))

## [0.17.1](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.17.0...v0.17.1) (2026-09-23)


### Bug Fixes

* **config:** an empty environment variable falls back to its default ([#116](https://github.com/fjcloudaiconsulting/ziftbook/issues/116)) ([eb81dee](https://github.com/fjcloudaiconsulting/ziftbook/commit/eb81deee02544b785229c6c22da0942aa99b6a7f))
* **frontend:** service names and descriptions need no particular language (ZIF-50) ([#117](https://github.com/fjcloudaiconsulting/ziftbook/issues/117)) ([ff7920b](https://github.com/fjcloudaiconsulting/ziftbook/commit/ff7920bad92b387ff5e24d2f5cdebbd402a3b96e))

## [0.17.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.16.0...v0.17.0) (2026-09-23)


### Features

* **backend:** creating a service assigns its workers in the same request (ZIF-104) ([#106](https://github.com/fjcloudaiconsulting/ziftbook/issues/106)) ([aa66c5d](https://github.com/fjcloudaiconsulting/ziftbook/commit/aa66c5d9498119912dac96e7d45f8305f734968b))
* **backend:** time off can block whole days and runs of days (ZIF-101) ([#108](https://github.com/fjcloudaiconsulting/ziftbook/issues/108)) ([f65570c](https://github.com/fjcloudaiconsulting/ziftbook/commit/f65570c594bb54f721bd1bdabaf89e6cf120329d))
* **frontend:** owners and workers set working hours in the console (ZIF-50) ([#111](https://github.com/fjcloudaiconsulting/ziftbook/issues/111)) ([e2cffbd](https://github.com/fjcloudaiconsulting/ziftbook/commit/e2cffbdaa96e77a9f4b7979ed49b5947485d6249))
* **frontend:** owners create, edit and archive services in the console (ZIF-50) ([#112](https://github.com/fjcloudaiconsulting/ziftbook/issues/112)) ([c65917f](https://github.com/fjcloudaiconsulting/ziftbook/commit/c65917f3e9acc527808bb2e5aa0e56c148b2b2db))
* **frontend:** owners invite people to their business from the console (ZIF-50) ([#113](https://github.com/fjcloudaiconsulting/ziftbook/issues/113)) ([22e690c](https://github.com/fjcloudaiconsulting/ziftbook/commit/22e690ced82d3da2d011ec618f249f5d19ce0f8f))
* **frontend:** owners set the shop's opening hours in the console (ZIF-50) ([#110](https://github.com/fjcloudaiconsulting/ziftbook/issues/110)) ([ae5dd08](https://github.com/fjcloudaiconsulting/ziftbook/commit/ae5dd08779c3a63f3bb9027b1d678136ae79b13c))
* **frontend:** people block whole days or part of a day off in the console (ZIF-50) ([#114](https://github.com/fjcloudaiconsulting/ziftbook/issues/114)) ([9471b4a](https://github.com/fjcloudaiconsulting/ziftbook/commit/9471b4a091cc809fe537233091f4223328371c3f))
* **frontend:** the merchant console shell with navigation for owners and workers (ZIF-50) ([#107](https://github.com/fjcloudaiconsulting/ziftbook/issues/107)) ([183e478](https://github.com/fjcloudaiconsulting/ziftbook/commit/183e478de472e9ffb603c725adecb56f1d51aa93))

## [0.16.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.15.0...v0.16.0) (2026-09-22)


### Features

* **backend:** a business has opening hours that bound every worker and booking (ZIF-105) ([#100](https://github.com/fjcloudaiconsulting/ziftbook/issues/100)) ([93660ab](https://github.com/fjcloudaiconsulting/ziftbook/commit/93660ab8c2f491e5eb1a68d58aeaeffc052f7921))
* **backend:** configurable cancellation rules, snapshotted on each booking (ZIF-55) ([#105](https://github.com/fjcloudaiconsulting/ziftbook/issues/105)) ([576c498](https://github.com/fjcloudaiconsulting/ziftbook/commit/576c498dd4507ec9902424941194be8c2787eced))
* **backend:** merchants accept or decline pending bookings (ZIF-52) ([#104](https://github.com/fjcloudaiconsulting/ziftbook/issues/104)) ([11e0edf](https://github.com/fjcloudaiconsulting/ziftbook/commit/11e0edfba0e3f1e390bf80df0f6752bd67dd6239))


### Bug Fixes

* **backend:** run the test suite in parallel with one database per worker (ZIF-111) ([#103](https://github.com/fjcloudaiconsulting/ziftbook/issues/103)) ([01e4d1b](https://github.com/fjcloudaiconsulting/ziftbook/commit/01e4d1b9e1bcfd3c1d3774b3822a01998d12fc51))

## [0.15.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.14.0...v0.15.0) (2026-09-21)


### Features

* **backend:** clients and provable marketing consent per business (ZIF-49) ([#99](https://github.com/fjcloudaiconsulting/ziftbook/issues/99)) ([b13a3ed](https://github.com/fjcloudaiconsulting/ziftbook/commit/b13a3ed69df30a26f06da4ad77fb530f45067ae4))
* **backend:** members have a display name that clients see when booking (ZIF-97) ([#97](https://github.com/fjcloudaiconsulting/ziftbook/issues/97)) ([e2f27de](https://github.com/fjcloudaiconsulting/ziftbook/commit/e2f27defc110ec37bf616a7d7c963bc87c26778d))

## [0.14.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.13.0...v0.14.0) (2026-09-17)


### ⚠ BREAKING CHANGES

* web apps older than 0.12.0 can no longer complete a sign-up; they must send a country.

### Features

* **backend:** owners choose which workers perform each service (ZIF-45) ([#91](https://github.com/fjcloudaiconsulting/ziftbook/issues/91)) ([8a34709](https://github.com/fjcloudaiconsulting/ziftbook/commit/8a347097b2f259892c20c118e3248400e8465073))
* **backend:** public availability per service with automatic buffers (ZIF-48) ([#92](https://github.com/fjcloudaiconsulting/ziftbook/issues/92)) ([275b287](https://github.com/fjcloudaiconsulting/ziftbook/commit/275b287be92336154a0edd6eb41c838cbb18a3ac))
* require the business country at sign-up (ZIF-96) ([#90](https://github.com/fjcloudaiconsulting/ziftbook/issues/90)) ([904a5ed](https://github.com/fjcloudaiconsulting/ziftbook/commit/904a5eda382ec39d5d58e60f7ecfe2073c23170e))


### Bug Fixes

* failed jobs store the error class, never its message (ZIF-93) ([#89](https://github.com/fjcloudaiconsulting/ziftbook/issues/89)) ([3c4c8ee](https://github.com/fjcloudaiconsulting/ziftbook/commit/3c4c8ee08da679561a6c23dac5d1bcc91cf987a9))

## [0.13.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.12.0...v0.13.0) (2026-09-17)


### Features

* log job, email and startup events, and an opt-in SQL switch (ZIF-95) ([#87](https://github.com/fjcloudaiconsulting/ziftbook/issues/87)) ([2e1357e](https://github.com/fjcloudaiconsulting/ziftbook/commit/2e1357e8822984bfeeb006609edbdc941adb585c))


### Bug Fixes

* make up names the port that's taken and waits for Mailpit (ZIF-91) ([#86](https://github.com/fjcloudaiconsulting/ziftbook/issues/86)) ([8e0c2a5](https://github.com/fjcloudaiconsulting/ziftbook/commit/8e0c2a539fcb130c1bb5e7d14a498f28a5c7b695))

## [0.12.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.11.0...v0.12.0) (2026-09-17)


### Features

* **backend:** send, list, revoke and accept invites (ZIF-27) ([#82](https://github.com/fjcloudaiconsulting/ziftbook/issues/82)) ([ce5d652](https://github.com/fjcloudaiconsulting/ziftbook/commit/ce5d652e68ecd3ad7f0f2829dc2dbc55b9c65777))
* **frontend:** accept an invite to a business (ZIF-27) ([#83](https://github.com/fjcloudaiconsulting/ziftbook/issues/83)) ([c5bd2d1](https://github.com/fjcloudaiconsulting/ziftbook/commit/c5bd2d11e4fd8133527cfe3b34e956212deb1519))
* **frontend:** ask for the business's country at sign-up (ZIF-27) ([#80](https://github.com/fjcloudaiconsulting/ziftbook/issues/80)) ([51ce853](https://github.com/fjcloudaiconsulting/ziftbook/commit/51ce853e502248c970cc5f9c7bb51268feee9aca))
* structured, levelled logs for the API, worker and migrations (ZIF-85) ([#85](https://github.com/fjcloudaiconsulting/ziftbook/issues/85)) ([95f1797](https://github.com/fjcloudaiconsulting/ziftbook/commit/95f1797108e7469f1d6f7d688a8e4aa15daf0f5a))


### Bug Fixes

* rate limits and audit events see the visitor's address (ZIF-82) ([#84](https://github.com/fjcloudaiconsulting/ziftbook/issues/84)) ([ab91434](https://github.com/fjcloudaiconsulting/ziftbook/commit/ab914341ef6fbbff55209f2f4dc304d3f091da3c))

## [0.11.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.10.0...v0.11.0) (2026-09-17)


### Features

* **backend:** business country, currency and language at sign-up (ZIF-27) ([#70](https://github.com/fjcloudaiconsulting/ziftbook/issues/70)) ([7573b94](https://github.com/fjcloudaiconsulting/ziftbook/commit/7573b9473d78ab0640a6ef3d6306c8c4cc3a142e))
* **backend:** invites table and invite email (ZIF-27) ([#75](https://github.com/fjcloudaiconsulting/ziftbook/issues/75)) ([5f48fea](https://github.com/fjcloudaiconsulting/ziftbook/commit/5f48fea57c43e6f69476c7b6c92eca8f53cd7ef7))
* **backend:** list, promote, demote and remove members (ZIF-27) ([#72](https://github.com/fjcloudaiconsulting/ziftbook/issues/72)) ([2726fe2](https://github.com/fjcloudaiconsulting/ziftbook/commit/2726fe27ca5a71e664276b9aacb0efce70523c9f))
* **backend:** owners manage their business's services (ZIF-44) ([#71](https://github.com/fjcloudaiconsulting/ziftbook/issues/71)) ([08df698](https://github.com/fjcloudaiconsulting/ziftbook/commit/08df69897e0be0e81fb96cb7d915f89f2202e337))
* **backend:** working hours with split shifts (ZIF-46) ([#73](https://github.com/fjcloudaiconsulting/ziftbook/issues/73)) ([5459afe](https://github.com/fjcloudaiconsulting/ziftbook/commit/5459afe5a915cee9f7a5673f7cc91671b2f885a4))
* **backend:** workers block time off (ZIF-47) ([#76](https://github.com/fjcloudaiconsulting/ziftbook/issues/76), shipped inside [#77](https://github.com/fjcloudaiconsulting/ziftbook/issues/77)) ([6e00019](https://github.com/fjcloudaiconsulting/ziftbook/commit/6e00019df6bba91e2e56e6ba6c02feaa38e356db))


### Bug Fixes

* **backend:** merge the two migration heads (ZIF-27, ZIF-46) ([#77](https://github.com/fjcloudaiconsulting/ziftbook/issues/77)) ([6e00019](https://github.com/fjcloudaiconsulting/ziftbook/commit/6e00019df6bba91e2e56e6ba6c02feaa38e356db))

## [0.10.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.9.0...v0.10.0) (2026-09-16)


### Features

* **backend:** record settings changes in the audit log (ZIF-23) ([#68](https://github.com/fjcloudaiconsulting/ziftbook/issues/68)) ([d49b078](https://github.com/fjcloudaiconsulting/ziftbook/commit/d49b078534b9aac053d253c3d38f1b6bcef20018))

## [0.9.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.8.0...v0.9.0) (2026-09-16)


### Features

* **backend:** an append-only audit log table (ZIF-28) ([#63](https://github.com/fjcloudaiconsulting/ziftbook/issues/63)) ([e50b662](https://github.com/fjcloudaiconsulting/ziftbook/commit/e50b662d6d9f140283e46c74d4ec58e4719103d0))
* **backend:** business settings with typed defaults (ZIF-23) ([#66](https://github.com/fjcloudaiconsulting/ziftbook/issues/66)) ([dc302d2](https://github.com/fjcloudaiconsulting/ziftbook/commit/dc302d2dde06063bec749dc87347532b5b90bea9))
* **backend:** owners read their business's audit log (ZIF-28) ([#65](https://github.com/fjcloudaiconsulting/ziftbook/issues/65)) ([223bbf2](https://github.com/fjcloudaiconsulting/ziftbook/commit/223bbf23981225b2ed57f46e64d188e81645aebb))
* **backend:** record sign-ins, sign-outs, new businesses and resets in the audit log (ZIF-28) ([#64](https://github.com/fjcloudaiconsulting/ziftbook/issues/64)) ([2791319](https://github.com/fjcloudaiconsulting/ziftbook/commit/2791319b6410268e9ba807f608bf3b5014386878))

## [0.8.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.7.0...v0.8.0) (2026-09-15)


### Features

* **backend:** reset a forgotten password by email link (ZIF-25) ([#60](https://github.com/fjcloudaiconsulting/ziftbook/issues/60)) ([ad31972](https://github.com/fjcloudaiconsulting/ziftbook/commit/ad319726053101c59eb67c2c20092741e7488b2e))
* **backend:** sign in with email and password (ZIF-25) ([#58](https://github.com/fjcloudaiconsulting/ziftbook/issues/58)) ([46e9474](https://github.com/fjcloudaiconsulting/ziftbook/commit/46e9474a535d3a63ffcd52ab3bc800c34bf63d7b))
* **backend:** sign up with an email link that sets up the business (ZIF-25) ([#59](https://github.com/fjcloudaiconsulting/ziftbook/issues/59)) ([57747fd](https://github.com/fjcloudaiconsulting/ziftbook/commit/57747fddb0b6b166fcba9eb4f2e4056877f02de8))
* **frontend:** account screens (ZIF-25) ([#62](https://github.com/fjcloudaiconsulting/ziftbook/issues/62)) ([4a48c33](https://github.com/fjcloudaiconsulting/ziftbook/commit/4a48c333f5f439f544c3dad055bcc23020c0b24f))

## [0.7.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.6.0...v0.7.0) (2026-09-15)


### Features

* **backend:** count rate limits in Postgres (ZIF-25) ([#54](https://github.com/fjcloudaiconsulting/ziftbook/issues/54)) ([2f9bb62](https://github.com/fjcloudaiconsulting/ziftbook/commit/2f9bb62ec70bf408af08ed118da654c7876af4d5))
* **backend:** send single-use account links by email (ZIF-25) ([#55](https://github.com/fjcloudaiconsulting/ziftbook/issues/55)) ([31d8152](https://github.com/fjcloudaiconsulting/ziftbook/commit/31d81527d6596a2e8e2e39ab350e1a7f50ed99de))
* **backend:** store password credentials and email tokens (ZIF-25) ([#53](https://github.com/fjcloudaiconsulting/ziftbook/issues/53)) ([527d5a4](https://github.com/fjcloudaiconsulting/ziftbook/commit/527d5a4de2e51a4dc56d15d6255329ead58c3d2a))

## [0.6.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.5.0...v0.6.0) (2026-09-15)


### Features

* **backend:** add server-side sessions (ZIF-24) ([#48](https://github.com/fjcloudaiconsulting/ziftbook/issues/48)) ([8299b09](https://github.com/fjcloudaiconsulting/ziftbook/commit/8299b090b61af1c7fa8ad1e616895124cc889e94))
* **backend:** add users and memberships (ZIF-24) ([#45](https://github.com/fjcloudaiconsulting/ziftbook/issues/45)) ([7e5b268](https://github.com/fjcloudaiconsulting/ziftbook/commit/7e5b268082ffdafcfb41f45778a8f88448815b42))
* **backend:** resolve the session cookie and sign out (ZIF-24) ([#50](https://github.com/fjcloudaiconsulting/ziftbook/issues/50)) ([3cf82ea](https://github.com/fjcloudaiconsulting/ziftbook/commit/3cf82eacf0966d2ec453a11826c398f079d8d309))

## [0.5.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.4.0...v0.5.0) (2026-09-14)


### Features

* **backend:** connect the API to the database (ZIF-24) ([#43](https://github.com/fjcloudaiconsulting/ziftbook/issues/43)) ([c561f3a](https://github.com/fjcloudaiconsulting/ziftbook/commit/c561f3acd0d47c304147a2e5a13a353d846ec26d))
* **backend:** reject state-changing requests that aren't JSON (ZIF-24) ([#44](https://github.com/fjcloudaiconsulting/ziftbook/issues/44)) ([7227596](https://github.com/fjcloudaiconsulting/ziftbook/commit/72275966dc0e23b270ad0af62f5962b9ec56bdcf))

## [0.4.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.3.0...v0.4.0) (2026-09-14)


### Features

* **backend:** add the worker process ([#38](https://github.com/fjcloudaiconsulting/ziftbook/issues/38)) ([d11e3f5](https://github.com/fjcloudaiconsulting/ziftbook/commit/d11e3f59086eae62d20b16f8180f7ed037788e48))
* **backend:** send email through a tenant-isolated outbox ([#39](https://github.com/fjcloudaiconsulting/ziftbook/issues/39)) ([396024b](https://github.com/fjcloudaiconsulting/ziftbook/commit/396024b0b74a76c3b8d98ba34775b1d8640008dc))

## [0.3.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.2.0...v0.3.0) (2026-09-14)


### Features

* **backend:** add a jobs table with idempotent enqueue ([#34](https://github.com/fjcloudaiconsulting/ziftbook/issues/34)) ([8c513bd](https://github.com/fjcloudaiconsulting/ziftbook/commit/8c513bde985902625ecaec19b1d787d076abda74))
* **backend:** run due jobs with backoff, timeouts and staleness skips ([#35](https://github.com/fjcloudaiconsulting/ziftbook/issues/35)) ([3323fee](https://github.com/fjcloudaiconsulting/ziftbook/commit/3323fee61902b8c5c0b3c75be65afec79850c2df))

## [0.2.0](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.1.1...v0.2.0) (2026-09-14)


### Features

* **backend:** add tenants table with UUIDv7 ids ([#28](https://github.com/fjcloudaiconsulting/ziftbook/issues/28)) ([e0f69b8](https://github.com/fjcloudaiconsulting/ziftbook/commit/e0f69b8bf213dba5f07a14e3dbf5607b378dd115))
* **backend:** set tenant context per transaction with tenant_context ([#29](https://github.com/fjcloudaiconsulting/ziftbook/issues/29)) ([5692944](https://github.com/fjcloudaiconsulting/ziftbook/commit/5692944af1ad647d52c570d90335249a6ac1b2a0))

## [0.1.1](https://github.com/fjcloudaiconsulting/ziftbook/compare/v0.1.0...v0.1.1) (2026-09-14)


### Miscellaneous Chores

* release 0.1.1 to publish the first images ([#23](https://github.com/fjcloudaiconsulting/ziftbook/issues/23)) ([a1e1efa](https://github.com/fjcloudaiconsulting/ziftbook/commit/a1e1efac6c4edf0477f7878286aca0b722f1b6d4))

## 0.1.0 (2026-09-14)


### Features

* **api:** commit the OpenAPI contract with stable operation ids ([#9](https://github.com/fjcloudaiconsulting/ziftbook/issues/9)) ([cded68b](https://github.com/fjcloudaiconsulting/ziftbook/commit/cded68b7be411dd4186ef8c162b4305d1fdfbf9a))
* **api:** scaffold FastAPI app with health endpoint ([#7](https://github.com/fjcloudaiconsulting/ziftbook/issues/7)) ([635acbd](https://github.com/fjcloudaiconsulting/ziftbook/commit/635acbd130cee2f889d4090a56d152f3f906feef))
* **landing:** add coming-soon page in English, Dutch and Portuguese ([#4](https://github.com/fjcloudaiconsulting/ziftbook/issues/4)) ([2db8daf](https://github.com/fjcloudaiconsulting/ziftbook/commit/2db8dafced8e3ea7ee572f8c4e199f5a0b176bf2))
* **landing:** deploy to Cloudflare with www and https redirects as code ([#5](https://github.com/fjcloudaiconsulting/ziftbook/issues/5)) ([634454b](https://github.com/fjcloudaiconsulting/ziftbook/commit/634454b0be8c519eb64428165033b564a3c57c72))
* **web:** forward /api to the API at runtime and show its version ([#10](https://github.com/fjcloudaiconsulting/ziftbook/issues/10)) ([1d8a1de](https://github.com/fjcloudaiconsulting/ziftbook/commit/1d8a1dec70ba8d6304cbe571e85e1f9d6e30b964))
* **web:** scaffold Next.js app with English, Dutch and Portuguese routing ([#6](https://github.com/fjcloudaiconsulting/ziftbook/issues/6)) ([f159907](https://github.com/fjcloudaiconsulting/ziftbook/commit/f159907a894f0ea3d8188ac4416c9b4dd05eb584))
