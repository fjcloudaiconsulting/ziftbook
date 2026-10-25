"""A person sets their own language: set_own_locale (ZIF-102).

users.locale (0005) is the person's own language, NULL for none, and it is read by every email a
person gets. Nothing could write it after sign-up: the app role has no UPDATE on users (0048), and
the users policies are pinned (tests/test_directory_ban.py). set_own_locale writes that one column
for a person who holds a live membership in the business the caller is acting for, the same gate
as set_own_name (0032, 0055). Which person is the route's business (current.user_id), as for
set_own_name: at the database level app.tenant_id is trusted from the caller.

The tenant filter on the gate is explicit: the memberships policy sign_in (0007) shows every
tenant's rows once the caller sets app.sign_in, so the policy alone would not hold the gate to the
caller's business. It returns whether the person is such a member (FOUND), whether or not the value
changed. No list of languages here: ck_users_locale (0005) is the database's rule.

Executable by ziftbook_app (and so by ziftbook_worker, its member), nobody else.

Revision ID: 0060
Revises: 0056
"""

from alembic import op

revision = "0060"
down_revision = "0056"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"


def upgrade() -> None:
    op.execute(f"""
CREATE FUNCTION set_own_locale(p_user uuid, p_locale text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Sets a person's own language (NULL: none); false unless they are a live member of this business.
BEGIN
  UPDATE users u SET locale = p_locale
  WHERE u.id = p_user
    AND EXISTS (SELECT 1 FROM memberships m
                WHERE m.user_id = p_user
                  AND m.tenant_id = current_setting('app.tenant_id')::uuid
                  AND m.role <> 'removed');
  RETURN FOUND;
END $$""")
    op.execute("REVOKE EXECUTE ON FUNCTION set_own_locale(uuid, text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION set_own_locale(uuid, text) TO ziftbook_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION set_own_locale(uuid, text)")
