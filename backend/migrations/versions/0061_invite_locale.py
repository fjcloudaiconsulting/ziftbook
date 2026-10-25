"""An invite is written in the invitee's own language when they have one: invite_locale (ZIF-102).

The email.invite job wrote every invite in the business's language, also to a person whose account
has a language of their own (users.locale). The job cannot read that row: users is visible only
through a live membership of the business (0055), and an invitee is not a member yet.

invite_locale(p_invite_id) returns the language of the account whose email is that invite's, or
NULL. It takes an invite id, never an email, and reads invites inside the caller's business:
invites has FORCE row-level security, so the definer (the table's owner) sees only the business in
app.tenant_id, and the query filters on it as well. An invite of another business gives NULL.

Executable by ziftbook_worker only: the invite email is a worker job (0053), and the API has no use
for it. The app role, and so the pinned list of definer functions it can call
(tests/test_directory_ban.py), does not change.

Revision ID: 0061
Revises: 0060
"""

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"


def upgrade() -> None:
    op.execute(f"""
CREATE FUNCTION invite_locale(p_invite_id uuid)
RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER {SEARCH_PATH} AS $$
  -- The invitee's own language, NULL when they have no account or chose none.
  SELECT u.locale FROM invites i JOIN users u ON u.email = i.email
  WHERE i.id = p_invite_id AND i.tenant_id = current_setting('app.tenant_id')::uuid
$$""")
    op.execute("REVOKE EXECUTE ON FUNCTION invite_locale(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION invite_locale(uuid) TO ziftbook_worker")


def downgrade() -> None:
    op.execute("DROP FUNCTION invite_locale(uuid)")
