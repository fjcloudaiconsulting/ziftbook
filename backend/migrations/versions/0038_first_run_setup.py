"""First-run setup: needs_setup() and complete_setup() (ZIF-70).

A fresh install has no users. needs_setup() says so to the app role, which can't read users without
a tenant. complete_setup() makes the first user a platform admin, once: it takes a table lock that
conflicts with every INSERT on users, then checks again on a fresh snapshot, so two setups, or a
setup and a sign-up, can't both create the first user. It refuses anything but READ COMMITTED,
where that re-check would see the old snapshot.

The downgrade drops both functions.

Revision ID: 0038
Revises: 0037
"""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

FUNCTIONS = {
    "needs_setup()": f"""
CREATE FUNCTION needs_setup() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER {SEARCH_PATH}
AS $$ SELECT NOT EXISTS (SELECT FROM users) $$""",
    "complete_setup(text,text,text,text)": f"""
CREATE FUNCTION complete_setup(p_email text, p_password_hash text, p_name text, p_locale text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- The first user, made a platform admin. NULL once any user exists.
DECLARE v_user uuid;
BEGIN
  IF NOT needs_setup() THEN RETURN NULL; END IF;  -- no lock once installed (users never empties)
  IF current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'complete_setup needs READ COMMITTED';  -- the re-check needs a fresh snapshot
  END IF;
  -- Conflicts with itself and with every INSERT on users: setups, sign-ups and invites serialise.
  LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE;
  IF NOT needs_setup() THEN RETURN NULL; END IF;
  INSERT INTO users (id, email, locale, name) VALUES (uuidv7(), p_email, p_locale, p_name)
  RETURNING id INTO v_user;
  INSERT INTO password_credentials (user_id, hash) VALUES (v_user, p_password_hash);
  INSERT INTO platform_admins (user_id) VALUES (v_user);
  RETURN v_user;
END $$""",
}


def upgrade() -> None:
    for signature, definition in FUNCTIONS.items():
        op.execute(definition)
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")


def downgrade() -> None:
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
