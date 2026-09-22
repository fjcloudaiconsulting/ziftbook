# Changelog

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
