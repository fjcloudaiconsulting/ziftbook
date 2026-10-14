"""Reserve the slug "setup": /{locale}/setup is the first-run setup page (ZIF-70).

slug_ok is replaced in place (CREATE OR REPLACE keeps its grants, and ck_tenants_slug keeps
referencing it). Existing rows are not re-checked by a CHECK, so the upgrade first refuses while a
business holds "setup": rename it, then upgrade. The downgrade restores 0030's list.

scripts/check-reserved-routes.mjs reads the RESERVED block of the newest migration that has one,
so this list must stay a RESERVED = \"\"\"...\"\"\" literal at the start of a line.

Revision ID: 0035
Revises: 0034
"""

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

# 0030's list, verbatim, so the downgrade restores its exact body.
OLD_RESERVED = """
  'booking','forgot-password','invite','reset-password','sign-in','sign-up',
  'calendar','clients','my-hours','opening-hours','services','settings','team',
  'api','admin','www','app','static','assets','public','help','support','about','pricing',
  'legal','privacy','terms','login','logout','account','billing','dashboard','ziftbook'
"""

RESERVED = """
  'booking','forgot-password','invite','reset-password','sign-in','sign-up',
  'calendar','clients','my-hours','opening-hours','services','settings','team',
  'api','admin','www','app','static','assets','public','help','support','about','pricing',
  'legal','privacy','terms','login','logout','account','billing','dashboard','ziftbook','setup'
"""


def slug_ok(reserved: str) -> str:
    """0030's SLUG_OK body with the given list, as CREATE OR REPLACE."""
    return f"""
CREATE OR REPLACE FUNCTION slug_ok(s text)
RETURNS boolean LANGUAGE sql IMMUTABLE {SEARCH_PATH} AS $$
  SELECT s ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(s) BETWEEN 3 AND 40
     AND s <> ALL (ARRAY[{reserved}])
$$"""


def upgrade() -> None:
    # free_slug's lock (0030), held to commit: a sign-up during the deploy waits for the new slug_ok
    # instead of taking "setup" between the check below and this commit.
    op.execute("SELECT pg_advisory_xact_lock(56, 0)")
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT FROM tenants WHERE slug = 'setup') THEN
        RAISE EXCEPTION 'a business holds the slug "setup"; rename it before upgrading';
      END IF;
    END $$""")
    op.execute(slug_ok(RESERVED))


def downgrade() -> None:
    op.execute(slug_ok(OLD_RESERVED))
