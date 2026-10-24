"""Only the worker role can issue invite tokens (ZB-08).

An invite link carries a secret whose sha256 is invites.token_hash. Only the email.invite job,
which runs in the worker as ziftbook_worker, sets one (app/mail.py send_invite). The API never
does: creating an invite leaves it NULL, and a resend sets it back to NULL so the old link dies
at once (app/invites.py). ziftbook_app keeps its table-level INSERT and UPDATE on invites, which
until now let it store a hash of its own choosing and so make a working link no email carried.

A trigger refuses a non-NULL token_hash, on INSERT or on an UPDATE that sets it, unless the role
running the statement is ziftbook_worker. A trigger rather than column grants: the API must still
be able to write the column, but only to clear it, and a grant cannot tell NULL from a hash; the
resend is one INSERT ... ON CONFLICT DO UPDATE statement that a grant-based split would have to
move into a new SECURITY DEFINER function. The trigger function is SECURITY INVOKER, so
current_user is the role running the statement; the check refuses every other role, the owner
and superusers included.

Operations: to set a hash by hand, a superuser runs `SET ROLE ziftbook_worker` first, which makes
current_user the worker's; do not disable the trigger, which would let every role through
meanwhile. A data-only restore of invites rows that carry a hash runs as the restoring role and is
refused: it needs --disable-triggers (pg_restore or pg_dump). A full restore is not affected: it
creates the trigger after it loads the data.

Precondition, as for 0051: the worker connects as ziftbook_worker in every environment. A worker
still on ziftbook_app's URL fails every email.invite job after this, so no invite email is sent.

Revision ID: 0053
Revises: 0052
"""

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"


def upgrade() -> None:
    # 42501 by name: the caller lacks the right to set a hash, as a missing grant would say.
    op.execute(f"""
    CREATE FUNCTION invites_token_hash_by_worker() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER {SEARCH_PATH} AS $$
      BEGIN
        IF current_user <> 'ziftbook_worker' THEN
          RAISE EXCEPTION 'only the worker role sets an invite token'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("""
    CREATE TRIGGER invites_token_hash_by_worker BEFORE INSERT OR UPDATE OF token_hash ON invites
      FOR EACH ROW WHEN (NEW.token_hash IS NOT NULL)
      EXECUTE FUNCTION invites_token_hash_by_worker()""")
    # Firing a trigger checks no EXECUTE privilege (0030), so no role needs it.
    op.execute("REVOKE EXECUTE ON FUNCTION invites_token_hash_by_worker() FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TRIGGER invites_token_hash_by_worker ON invites")
    op.execute("DROP FUNCTION invites_token_hash_by_worker()")
