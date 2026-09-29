"""tenants.slug: a business's public booking-page address (ZIF-56).

The slug is derived by a trigger, so complete_sign_up is unchanged, the old app keeps signing up
during the deploy, and NOT NULL is safe in this same release (0013's precedent: nothing but that
function and test fixtures insert tenants, and both go through the trigger).

Revision ID: 0030
Revises: 0029
"""

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

# Every top-level route under frontend/app/[locale]/ today (console included), plus squats. The
# page PR may pick /{locale}/{slug}; a slug handed out is a one-way door. Locales and anything
# under 3 chars are already refused by the length rule. Changing the list: CREATE OR REPLACE in a
# new migration; existing rows are NOT re-checked, so first check no row holds the new word.
RESERVED = """
  'booking','forgot-password','invite','reset-password','sign-in','sign-up',
  'calendar','clients','my-hours','opening-hours','services','settings','team',
  'api','admin','www','app','static','assets','public','help','support','about','pricing',
  'legal','privacy','terms','login','logout','account','billing','dashboard','ziftbook'
"""

SLUG_OK = f"""
CREATE FUNCTION slug_ok(s text) RETURNS boolean LANGUAGE sql IMMUTABLE {SEARCH_PATH} AS $$
  SELECT s ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(s) BETWEEN 3 AND 40
     AND s <> ALL (ARRAY[{RESERVED}])
$$"""

FREE_SLUG = rf"""
CREATE FUNCTION free_slug(p_name text) RETURNS text LANGUAGE plpgsql {SEARCH_PATH} AS $$
-- A free, valid slug derived from a business name: "Salão da Ana" -> salao-da-ana, then -2, -3.
DECLARE base text; candidate text; n int := 1;
BEGIN
  -- ZIF-56's key in the ticket-number convention (51 bookings, 105 opening hours). GLOBAL, not
  -- per base: "Nail bar 2" (base nail-bar-2) and a second "Nail bar" (probing nail-bar-2) have
  -- different bases but meet on one candidate. After the lock, READ COMMITTED gives each
  -- statement below a fresh snapshot, so a same-slug sign-up that committed first is seen. Held
  -- to commit. Sign-ups are rare.
  PERFORM pg_advisory_xact_lock(56, 0);
  -- Letters NFKD does not decompose, first; then strip combining marks (U+0300..U+036F). lower()
  -- under COLLATE "C" only folds ASCII, so ÆØŁĐŒÞẞ's uppercase forms must be mapped explicitly
  -- too, not just their lower() output.
  base := translate(replace(replace(replace(replace(
            replace(replace(replace(replace(lower(p_name),
              'ß', 'ss'), 'ẞ', 'ss'), 'æ', 'ae'), 'Æ', 'ae'),
            'œ', 'oe'), 'Œ', 'oe'), 'þ', 'th'), 'Þ', 'th'),
            'øłđØŁĐ', 'oldold');
  base := regexp_replace(normalize(base, NFKD), '[\u0300-\u036f]', '', 'g');
  -- Decomposing an uppercase accented letter (\u00c3 -> A + combining tilde) exposes a plain-ASCII
  -- uppercase base letter lower() never got a chance at; lower() again, now safely ASCII-only.
  base := lower(base);
  base := trim(both '-' from regexp_replace(base, '[^a-z0-9]+', '-', 'g'));
  base := trim(both '-' from left(base, 34));   -- room for "-NNNNN" within 40
  IF base = '' THEN base := 'business'; END IF;  -- "!!!", non-Latin scripts
  candidate := base;
  -- ponytail: linear probe; switch to a random suffix if one base ever has hundreds of owners.
  WHILE NOT slug_ok(candidate) OR EXISTS (SELECT 1 FROM tenants WHERE slug = candidate) LOOP
    n := n + 1;                                  -- "a" -> a-2, "Calendar" -> calendar-2
    -- Production safety, not a test aid: a bug here would loop while holding (56, 0) and stall
    -- every sign-up. 1000 keeps the suffix within the 34 + 6 = 40 budget.
    IF n > 1000 THEN
      RAISE EXCEPTION 'free_slug: no free slug for base %', base USING ERRCODE = 'P0001';
    END IF;
    candidate := base || '-' || n;
  END LOOP;
  RETURN candidate;
END $$"""

DEFAULT_SLUG = f"""
CREATE FUNCTION tenants_default_slug() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- SECURITY DEFINER: free_slug's EXECUTE is not granted to ziftbook_app (below), so an app-role
-- insert's trigger call needs the owner's privileges to reach it.
BEGIN NEW.slug := free_slug(NEW.name); RETURN NEW; END $$"""

TRIGGER = """
CREATE TRIGGER tenants_default_slug BEFORE INSERT ON tenants
FOR EACH ROW WHEN (NEW.slug IS NULL) EXECUTE FUNCTION tenants_default_slug()"""


def upgrade() -> None:
    op.execute(SLUG_OK)
    op.execute(FREE_SLUG)
    op.execute(DEFAULT_SLUG)
    # Nullable first: no rewrite. ADD COLUMN holds ACCESS EXCLUSIVE on tenants until commit, so no
    # sign-up interleaves with the backfill.
    op.execute(
        "ALTER TABLE tenants ADD COLUMN slug text CONSTRAINT ck_tenants_slug CHECK (slug_ok(slug))"
    )
    # Oldest first (uuidv7 ids), one statement per row so each probe sees the rows before it.
    # tenants has no RLS: no per-tenant set_config.
    op.execute("""
    DO $$ DECLARE r record; BEGIN
      FOR r IN SELECT id, name FROM tenants ORDER BY id LOOP
        UPDATE tenants SET slug = free_slug(r.name) WHERE id = r.id;
      END LOOP;
    END $$""")
    op.execute("ALTER TABLE tenants ADD CONSTRAINT uq_tenants_slug UNIQUE (slug)")
    op.execute("ALTER TABLE tenants ALTER COLUMN slug SET NOT NULL")
    op.execute(TRIGGER)
    # free_slug is not for ziftbook_app to call directly (it holds the (56, 0) advisory lock);
    # tenants_default_slug is SECURITY DEFINER so an app-role insert still reaches it. slug_ok
    # stays reachable: ck_tenants_slug's CHECK runs as the inserting role, not the trigger's.
    # tenants_default_slug itself needs no grant, since firing a trigger is not a privilege-checked
    # call. Its REVOKE exists only for test_password_auth_db.py's rule that a SECURITY DEFINER
    # function is never executable by PUBLIC.
    op.execute("REVOKE EXECUTE ON FUNCTION slug_ok(text), free_slug(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION slug_ok(text) TO ziftbook_app")
    op.execute("REVOKE EXECUTE ON FUNCTION tenants_default_slug() FROM PUBLIC")
    # No other grant changes: ziftbook_app keeps INSERT and UPDATE (name) only; no UPDATE (slug)
    # until ZIF-42.


def downgrade() -> None:
    # Trigger first (every later insert would hit a missing column), then the column (takes its
    # CHECK and UNIQUE; the CHECK depends on slug_ok), then the functions.
    op.execute("DROP TRIGGER tenants_default_slug ON tenants")
    op.execute("ALTER TABLE tenants DROP COLUMN slug")
    op.execute("DROP FUNCTION tenants_default_slug(), free_slug(text), slug_ok(text)")
