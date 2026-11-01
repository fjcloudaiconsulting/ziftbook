"""The app role can no longer bring back a removed membership (ZB-32, defence in depth).

0055 hides a removed member's account from the business, but 0054 leaves ziftbook_app UPDATE of
role and removed_at on every membership of its tenant, removed ones included: an app role that set
a removed row back to a live role would read the account again. The app never writes a removed
row (members.py's target and member_user filter role <> 'removed'); the one way back is an invite,
accept_invite's revive (0045), a SECURITY DEFINER function owned by ziftbook_migrate.

A trigger refuses an UPDATE of role or removed_at on a removed membership unless the role running
the statement is ziftbook_migrate, which accept_invite runs as. A trigger rather than a grant or a
policy, as in 0053: the app must still set both columns on live rows (change_role, remove), and a
column grant cannot tell a removed row from a live one. The trigger function is SECURITY INVOKER,
so current_user is the role running the statement: ziftbook_app and ziftbook_worker are refused,
including where a login role runs as them through its role setting, and so are superusers as
themselves. Removing a member (a live row becoming removed), role changes of live members and
display names are not affected.

Operations: to bring a membership back by hand, invite the person again, or run as a superuser
after SET ROLE ziftbook_migrate; do not disable the trigger. A restore inserts rows and is not
affected.

No precondition: no release up to v0.31.0 updates a removed membership as the app role.

Revision ID: 0056
Revises: 0055
"""

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"


def upgrade() -> None:
    # 42501 by name: the caller lacks the right to bring the membership back, as a missing grant
    # would say.
    op.execute(f"""
    CREATE FUNCTION memberships_revive_by_migrate() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER {SEARCH_PATH} AS $$
      BEGIN
        IF current_user <> 'ziftbook_migrate' THEN
          RAISE EXCEPTION 'a removed membership comes back only through an invite'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("""
    CREATE TRIGGER memberships_revive_by_migrate BEFORE UPDATE OF role, removed_at ON memberships
      FOR EACH ROW WHEN (OLD.role = 'removed')
      EXECUTE FUNCTION memberships_revive_by_migrate()""")
    # Firing a trigger checks no EXECUTE privilege (0030), so no role needs it.
    op.execute("REVOKE EXECUTE ON FUNCTION memberships_revive_by_migrate() FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TRIGGER memberships_revive_by_migrate ON memberships")
    op.execute("DROP FUNCTION memberships_revive_by_migrate()")
