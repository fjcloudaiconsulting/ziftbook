-- Creates the three database roles, their database-level grants and their settings. Idempotent: safe to run on
-- every deploy.
-- Run as an admin role (a superuser locally, the provider's admin role when hosted):
--   psql -v ON_ERROR_STOP=1 -d <app database> -f backend/migrations/bootstrap.sql
-- Connect to the application's database (compose: ziftbook): the database grants and the TEMPORARY
-- revoke below apply to the database psql is connected to.
-- Requires ZIF_MIGRATE_PASSWORD, ZIF_APP_PASSWORD and ZIF_WORKER_PASSWORD in the environment. All three are
-- checked before anything is created or changed: if any is unset or empty (a PASSWORD '' would clear the
-- role's password), the run stops non-zero, even without ON_ERROR_STOP, and changes nothing. Set the
-- password and run it again. The worker must connect as ziftbook_worker: only it may mint email tokens
-- (0051) and set invite tokens (0053), so a worker still on ziftbook_app fails every sign-up, reset and
-- invite email (ZB-08).
--
-- ziftbook_migrate owns the schema and runs Alembic. ziftbook_app owns nothing and gets DML only, through
-- default privileges set in migration #1. Owners bypass row-level security, so the app must never own tables.
-- ziftbook_worker is the worker's: every privilege of ziftbook_app (CONNECT included) plus what only
-- background jobs may do, granted by migrations (0049: mint_email_token) or left to it by a trigger (0053: an
-- invite's token_hash). It owns nothing either.

\getenv migrate_password ZIF_MIGRATE_PASSWORD
\getenv app_password ZIF_APP_PASSWORD
\getenv worker_password ZIF_WORKER_PASSWORD

-- Before any CREATE or ALTER, so a run this stops leaves every role as it was. Empty counts as unset. Each
-- password starts as missing, and only a set, non-empty value clears that.
\set migrate_password_missing true
\set app_password_missing true
\set worker_password_missing true
\if :{?migrate_password}
SELECT :'migrate_password' = '' AS migrate_password_missing \gset
\endif
\if :{?app_password}
SELECT :'app_password' = '' AS app_password_missing \gset
\endif
\if :{?worker_password}
SELECT :'worker_password' = '' AS worker_password_missing \gset
\endif
-- Each stops non-zero even when the caller did not set ON_ERROR_STOP.
\if :migrate_password_missing
\set ON_ERROR_STOP on
DO $$ BEGIN RAISE EXCEPTION 'ZIF_MIGRATE_PASSWORD is unset or empty: ziftbook_migrate needs a password'; END $$;
\endif
\if :app_password_missing
\set ON_ERROR_STOP on
DO $$ BEGIN RAISE EXCEPTION 'ZIF_APP_PASSWORD is unset or empty: ziftbook_app needs a password'; END $$;
\endif
\if :worker_password_missing
\set ON_ERROR_STOP on
DO $$ BEGIN RAISE EXCEPTION 'ZIF_WORKER_PASSWORD is unset or empty: ziftbook_worker needs a password'; END $$;
\endif

SELECT 'CREATE ROLE ziftbook_migrate LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ziftbook_migrate') \gexec

SELECT 'CREATE ROLE ziftbook_app LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ziftbook_app') \gexec

SELECT 'CREATE ROLE ziftbook_worker LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ziftbook_worker') \gexec

ALTER ROLE ziftbook_migrate PASSWORD :'migrate_password';
ALTER ROLE ziftbook_app PASSWORD :'app_password';
ALTER ROLE ziftbook_worker PASSWORD :'worker_password';

GRANT CONNECT, CREATE ON DATABASE :"DBNAME" TO ziftbook_migrate;
GRANT CONNECT ON DATABASE :"DBNAME" TO ziftbook_app;
GRANT CREATE ON SCHEMA public TO ziftbook_migrate;
GRANT ziftbook_app TO ziftbook_worker;

-- ZB-31: neither runtime role creates temporary tables, and both search public before pg_temp. Only
-- the database owner can take TEMPORARY from PUBLIC, so it is done here, as the admin; migration 0050
-- does it where ziftbook_migrate owns the database (the test databases). ziftbook_migrate keeps it,
-- as it does where it owns the database. Role settings need a superuser or CREATEROLE, so they live
-- here too, one per role: ziftbook_worker inherits ziftbook_app's privileges, not its settings.
-- Where the admin role neither owns the database nor is a member of its owner (some hosted providers),
-- the REVOKE only warns that nothing was revoked, so the check right after it stops the run with an
-- error if either runtime role still holds TEMPORARY.
REVOKE TEMPORARY ON DATABASE :"DBNAME" FROM PUBLIC;
GRANT TEMPORARY ON DATABASE :"DBNAME" TO ziftbook_migrate;
-- Stops non-zero even when the caller did not set ON_ERROR_STOP.
\set ON_ERROR_STOP on
DO $$
BEGIN
  IF has_database_privilege('ziftbook_app', current_database(), 'TEMPORARY')
     OR has_database_privilege('ziftbook_worker', current_database(), 'TEMPORARY') THEN
    RAISE EXCEPTION 'ziftbook_app or ziftbook_worker can still create temporary tables in %: the REVOKE TEMPORARY ... FROM PUBLIC did not take. Run that REVOKE as the database owner, or run bootstrap.sql as the owner or a superuser.', current_database();
  END IF;
END $$;
ALTER ROLE ziftbook_app SET search_path = public, pg_temp;
ALTER ROLE ziftbook_worker SET search_path = public, pg_temp;
