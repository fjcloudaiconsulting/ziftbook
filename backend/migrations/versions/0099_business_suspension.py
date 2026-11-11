"""A business can be suspended (ZIF-73).

suspended_at and suspension_reason are both NULL or both set (a CHECK pairs them, caps the reason
at 500 characters and refuses a blank one). Nothing here sets them: the admin routes that do come
separately, with their own privilege.

account_by_email is replaced in place: the oldest membership it picks is the oldest of a business
that is not suspended, falling back to a suspended one only when that is all the person has, so
sign-in refuses them with a clear answer.

The downgrade restores 0045's account_by_email and drops the CHECK and both columns.

Revision ID: 0099
Revises: 0098
"""

from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"


def account_by_email(oldest: str) -> str:
    """0045's body, as CREATE OR REPLACE, with the oldest-membership subquery given."""
    return f"""
CREATE OR REPLACE FUNCTION account_by_email(p_email text)
RETURNS TABLE (user_id uuid, password_hash text, tenant_id uuid)
LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- Sign-in, before a tenant is known: the user, their hash and their oldest membership's tenant.
-- A function-level SET of these settings needs superuser, so they are set and restored here.
DECLARE
  v_sign_in text := current_setting('app.sign_in', true);
  v_tenant text := current_setting('app.tenant_id', true);
BEGIN
  PERFORM set_config('app.sign_in', 'on', true);
  -- tenant_isolation must not raise; this tenant matches no row.
  PERFORM set_config('app.tenant_id', '00000000-0000-0000-0000-000000000000', true);
  RETURN QUERY
    SELECT u.id, c.hash,
           ({oldest})
    FROM users u LEFT JOIN password_credentials c ON c.user_id = u.id
    WHERE u.email = p_email;
  PERFORM set_config('app.sign_in', coalesce(v_sign_in, ''), true);
  PERFORM set_config('app.tenant_id', coalesce(v_tenant, ''), true);
END $$"""


# 0045's, exactly.
OLD_OLDEST = """SELECT m.tenant_id FROM memberships m WHERE m.user_id = u.id AND m.role <> 'removed'
            ORDER BY m.id LIMIT 1"""

NEW_OLDEST = """SELECT m.tenant_id FROM memberships m JOIN tenants t ON t.id = m.tenant_id
            WHERE m.user_id = u.id AND m.role <> 'removed'
            ORDER BY (t.suspended_at IS NOT NULL), m.id LIMIT 1"""


def upgrade() -> None:
    op.execute("""
    ALTER TABLE tenants
      ADD COLUMN suspended_at timestamptz,
      ADD COLUMN suspension_reason text,
      ADD CONSTRAINT ck_tenants_suspension CHECK (
        (suspended_at IS NULL) = (suspension_reason IS NULL)
        AND char_length(suspension_reason) <= 500
        AND btrim(suspension_reason) <> '')
    """)
    op.execute(account_by_email(NEW_OLDEST))


def downgrade() -> None:
    # ZIF-284: lock first, as 0013's downgrade does.
    op.execute("LOCK TABLE tenants IN ACCESS EXCLUSIVE MODE")
    op.execute(account_by_email(OLD_OLDEST))
    op.execute(
        "ALTER TABLE tenants DROP CONSTRAINT ck_tenants_suspension,"
        " DROP COLUMN suspended_at, DROP COLUMN suspension_reason"
    )
