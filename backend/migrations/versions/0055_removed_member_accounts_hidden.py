"""A business no longer reads a removed member's account (ZB-32).

Since ZIF-112 (0045) removing someone ends their membership in place (role 'removed'), but the
users policy tenant_members (0005) and set_own_name's gate (0032, 0045) still counted every
membership. So a business kept reading a removed member's account row (email, name, language) for
as long as the membership stayed, which is for good, and could name a nameless former member. Both
now count only live memberships (role <> 'removed').

No feature needs the row: every read of a member's email or language already filters removed
members, and history names a member by memberships.display_name, the business's own copy. Only
history's fallback to users.name no longer reaches a removed member, so one with no display name
is unnamed, as EventOut documents. Re-inviting revives the same membership (accept_invite, 0045),
so the account is visible again exactly when they are a member again.

No precondition: the release before this one (v0.31.0) reads a removed member's account only
through HISTORY's u.name fallback, whose null its EventOut already documents. Every other read
filters removed members. The policy is altered and set_own_name replaced in place, so neither's
roles, owner nor grants change.

Revision ID: 0055
Revises: 0054
"""

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

LIVE = " AND m.role <> 'removed'"


def set_own_name(gate: str) -> str:
    """0045's set_own_name, as CREATE OR REPLACE, with the filter on its membership gate given.
    Its tenant list keeps 0045's live-membership filter either way."""
    return f"""
CREATE OR REPLACE FUNCTION set_own_name(p_user uuid, p_name text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Gives a nameless person their name; false when they already have one or are not in this business.
DECLARE
  v_sign_in text := current_setting('app.sign_in', true);
  v_tenant text := current_setting('app.tenant_id', true);
  v_tenants uuid[];
  t uuid;
BEGIN
  PERFORM set_config('app.sign_in', '', true);
  UPDATE users u SET name = p_name
  WHERE u.id = p_user AND u.name IS NULL
    AND EXISTS (SELECT 1 FROM memberships m WHERE m.user_id = p_user{gate});
  IF NOT FOUND THEN
    PERFORM set_config('app.sign_in', coalesce(v_sign_in, ''), true);
    RETURN false;
  END IF;
  PERFORM set_config('app.sign_in', 'on', true);
  SELECT array_agg(m.tenant_id) INTO v_tenants FROM memberships m WHERE m.user_id = p_user{LIVE};
  PERFORM set_config('app.sign_in', coalesce(v_sign_in, ''), true);
  FOREACH t IN ARRAY v_tenants LOOP
    PERFORM set_config('app.tenant_id', t::text, true);
    UPDATE memberships SET display_name = p_name WHERE user_id = p_user AND display_name IS NULL;
  END LOOP;
  PERFORM set_config('app.tenant_id', coalesce(v_tenant, ''), true);
  RETURN true;
END $$"""


def upgrade() -> None:
    # The subquery reads memberships under the caller's tenant (FORCE row-level security), as
    # before: only this business's live members.
    op.execute(
        "ALTER POLICY tenant_members ON users "
        "USING (id IN (SELECT user_id FROM memberships WHERE role <> 'removed'))"
    )
    op.execute(set_own_name(LIVE))


def downgrade() -> None:
    op.execute(
        "ALTER POLICY tenant_members ON users USING (id IN (SELECT user_id FROM memberships))"
    )
    op.execute(set_own_name(""))
