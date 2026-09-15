# Changelog

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
