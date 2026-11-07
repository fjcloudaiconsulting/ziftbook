"""Whether an admin's TOTP enrolment is stopped, for begin to say so (ZIF-275).

Since 0084 begin_totp_enrolment answers invalid_ticket on a stopped enrolment (10 wrong confirm
codes), so the web app showed "wrong ticket" for a ticket that was right. totp_enrolment_locked
tells the two apart: true only for the session user's own unconfirmed factor, with this live
ticket, stopped. The API asks it after begin refused a ticket and answers 403 enrol_locked. Only a
holder of the half session and the ticket learns it, and confirm already tells them as much.

A new function rather than another answer from begin: the release still running during the deploy
treats any answer it doesn't know from begin as begun and would show a QR code for a secret never
stored. Read-only: no count, ticket or expiry changes. The downgrade drops it.

Revision ID: 0095
Revises: 0094
"""

from alembic import op

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"
SIGNATURE = "totp_enrolment_locked(bytea,bytea)"

FUNCTION = f"""
CREATE FUNCTION totp_enrolment_locked(p_id_hash bytea, p_ticket_proof bytea)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER {SEARCH_PATH} AS $$
  SELECT EXISTS (
    SELECT FROM second_factors f
    WHERE f.user_id = live_admin_user(p_id_hash) AND f.confirmed_at IS NULL
      AND f.ticket_hash = sha256(p_ticket_proof) AND f.ticket_expires_at > now()
      AND f.failed_attempts >= 10)
$$"""


def upgrade() -> None:
    op.execute(FUNCTION)
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO ziftbook_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")
