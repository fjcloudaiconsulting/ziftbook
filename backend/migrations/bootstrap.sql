-- Creates the two database roles and their database-level grants. Idempotent: safe to run on every deploy.
-- Run as an admin role (a superuser locally, the provider's admin role when hosted):
--   psql -v ON_ERROR_STOP=1 -f backend/migrations/bootstrap.sql
-- Requires ZIF_MIGRATE_PASSWORD and ZIF_APP_PASSWORD in the environment. If either is unset, the
-- password statement fails and the run stops non-zero (a role created without a password cannot log in).
--
-- ziftbook_migrate owns the schema and runs Alembic. ziftbook_app owns nothing and gets DML only, through
-- default privileges set in migration #1. Owners bypass row-level security, so the app must never own tables.

\getenv migrate_password ZIF_MIGRATE_PASSWORD
\getenv app_password ZIF_APP_PASSWORD

SELECT 'CREATE ROLE ziftbook_migrate LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ziftbook_migrate') \gexec

SELECT 'CREATE ROLE ziftbook_app LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ziftbook_app') \gexec

ALTER ROLE ziftbook_migrate PASSWORD :'migrate_password';
ALTER ROLE ziftbook_app PASSWORD :'app_password';

GRANT CONNECT, CREATE ON DATABASE :"DBNAME" TO ziftbook_migrate;
GRANT CONNECT ON DATABASE :"DBNAME" TO ziftbook_app;
GRANT CREATE ON SCHEMA public TO ziftbook_migrate;
