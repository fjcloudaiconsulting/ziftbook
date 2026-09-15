"""password_unchanged: sign-in holds the password it checked until its session is stored.

Sign-in checks the password with no database connection held, so a reset could complete in between
and the new session would outlive it. Called in the transaction that stores the session, this sees a
reset that already happened, and makes one that is under way wait until the session is there to be
deleted.

The app role can hold that lock for any user whose hash it has (account_by_email returns it), which
delays their reset but changes and reveals nothing.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

SIGNATURE = "password_unchanged(uuid,text)"


def upgrade() -> None:
    op.execute("""
CREATE FUNCTION password_unchanged(p_user_id uuid, p_hash text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  PERFORM FROM password_credentials c WHERE c.user_id = p_user_id AND c.hash = p_hash FOR SHARE;
  RETURN FOUND;
END $$""")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO ziftbook_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")
