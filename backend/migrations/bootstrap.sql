-- Creates the three database roles, their database-level grants and their settings. Idempotent: safe to run on
-- every deploy.
-- Run as an admin role (a superuser locally, the provider's admin role when hosted):
--   psql -v ON_ERROR_STOP=1 -f backend/migrations/bootstrap.sql
-- Requires ZIF_MIGRATE_PASSWORD and ZIF_APP_PASSWORD in the environment. If either is unset, the
-- password statement fails and the run stops non-zero (a role created without a password cannot log in).
-- ZIF_WORKER_PASSWORD may still be unset while a deployment's worker connects as ziftbook_app: the run
-- warns and leaves ziftbook_worker's password as it is, so a role created without one cannot log in yet.
-- Empty counts as unset (a PASSWORD '' would clear the role's password). Set it before the release
-- that takes mint_email_token from ziftbook_app (the contract migration, ZB-08).
--
-- ziftbook_migrate owns the schema and runs Alembic. ziftbook_app owns nothing and gets DML only, through
-- default privileges set in migration #1. Owners bypass row-level security, so the app must never own tables.
-- ziftbook_worker is the worker's: every privilege of ziftbook_app (CONNECT included) plus what only
-- background jobs may do, granted by migrations (0049: mint_email_token). It owns nothing either.

\getenv migrate_password ZIF_MIGRATE_PASSWORD
\getenv app_password ZIF_APP_PASSWORD
\getenv worker_password ZIF_WORKER_PASSWORD

SELECT 'CREATE ROLE ziftbook_migrate LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ziftbook_migrate') \gexec

SELECT 'CREATE ROLE ziftbook_app LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ziftbook_app') \gexec

SELECT 'CREATE ROLE ziftbook_worker LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ziftbook_worker') \gexec

ALTER ROLE ziftbook_migrate PASSWORD :'migrate_password';
ALTER ROLE ziftbook_app PASSWORD :'app_password';
\set worker_password_set false
\if :{?worker_password}
SELECT :'worker_password' <> '' AS worker_password_set \gset
\endif
\if :worker_password_set
ALTER ROLE ziftbook_worker PASSWORD :'worker_password';
\else
\warn 'ZIF_WORKER_PASSWORD is unset or empty: ziftbook_worker''s password is unchanged. Until it has one, it cannot log in and the worker must keep ziftbook_app''s URL.'
\endif

GRANT CONNECT, CREATE ON DATABASE :"DBNAME" TO ziftbook_migrate;
GRANT CONNECT ON DATABASE :"DBNAME" TO ziftbook_app;
GRANT CREATE ON SCHEMA public TO ziftbook_migrate;
GRANT ziftbook_app TO ziftbook_worker;

-- ZB-31: neither runtime role creates temporary tables, and both search public before pg_temp. Only
-- the database owner can take TEMPORARY from PUBLIC, so it is done here, as the admin; migration 0050
-- does it where ziftbook_migrate owns the database (the test databases). ziftbook_migrate keeps it,
-- as it does where it owns the database. Role settings need a superuser or CREATEROLE, so they live
-- here too, one per role: ziftbook_worker inherits ziftbook_app's privileges, not its settings.
REVOKE TEMPORARY ON DATABASE :"DBNAME" FROM PUBLIC;
GRANT TEMPORARY ON DATABASE :"DBNAME" TO ziftbook_migrate;
ALTER ROLE ziftbook_app SET search_path = public, pg_temp;
ALTER ROLE ziftbook_worker SET search_path = public, pg_temp;
