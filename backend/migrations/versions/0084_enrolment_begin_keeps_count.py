"""Beginning an admin's TOTP enrolment again keeps its count of wrong confirm codes (ZIF-262).

begin_totp_enrolment set second_factors.failed_attempts back to 0, so entering the setup ticket
again gave ten fresh guesses at the pending secret, as often as wanted within the ticket's hour, and
lifted a lock. Now begin keeps the count; 10 wrong confirm codes lock the enrolment until the CLI's
new ticket (`python -m app.admin enrol`), which resets it. Begin on a stopped enrolment answers
invalid_ticket and stores no new secret, so the web app shows no QR code that could never confirm.
Takes no table lock.

Replaced in place: same arguments and result, so its owner and grants stay; restated anyway. The
downgrade restores 0083's body and leaves every count as it is.

Revision ID: 0084
Revises: 0083
"""

from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None

SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"

FUNCTIONS = {
    "begin_totp_enrolment(bytea,bytea,bytea,bytea)": f"""
CREATE OR REPLACE FUNCTION begin_totp_enrolment(
  p_id_hash bytea, p_ticket_proof bytea, p_secret_ct bytea, p_proof bytea)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- 'begun' | 'invalid_ticket' | 'already_enrolled'. Stores a new, unconfirmed secret for the session
-- user, given the CLI's ticket. Keeps the count of wrong confirm codes: beginning again refills
-- no guesses and lifts no lock, only the CLI's new ticket does (ZIF-262). Voids the attempts in
-- flight: none is checked against the new secret (0083), and that lowers no count.
DECLARE v_user uuid := live_admin_user(p_id_hash);
BEGIN
  -- Never overwrites a confirmed secret: re-enrolling goes through the CLI.
  IF EXISTS (SELECT FROM second_factors f WHERE f.user_id = v_user AND f.confirmed_at IS NOT NULL)
  THEN
    RETURN 'already_enrolled';
  END IF;
  UPDATE second_factors f SET secret_ct = p_secret_ct, proof_hash = sha256(p_proof),
                              last_step = 0, charges = '{{}}'
  WHERE f.user_id = v_user AND f.ticket_hash = sha256(p_ticket_proof)
    AND f.ticket_expires_at > now()
    -- Stopped: no new secret that could never be confirmed (ZIF-262). invalid_ticket sends the
    -- admin for a new ticket.
    AND f.failed_attempts < 10;
  -- The ticket is not used up here: an abandoned QR can be restarted within the hour.
  RETURN CASE WHEN FOUND THEN 'begun' ELSE 'invalid_ticket' END;
END $$""",
}

# 0083's body, verbatim, for the downgrade.
FUNCTIONS_0083 = {
    "begin_totp_enrolment(bytea,bytea,bytea,bytea)": f"""
CREATE OR REPLACE FUNCTION begin_totp_enrolment(
  p_id_hash bytea, p_ticket_proof bytea, p_secret_ct bytea, p_proof bytea)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER {SEARCH_PATH} AS $$
-- 'begun' | 'invalid_ticket' | 'already_enrolled'. Stores a new, unconfirmed secret for the session
-- user, given the CLI's ticket. Voids the attempts in flight: none is checked against the new
-- secret (0083).
DECLARE v_user uuid := live_admin_user(p_id_hash);
BEGIN
  -- Never overwrites a confirmed secret: re-enrolling goes through the CLI.
  IF EXISTS (SELECT FROM second_factors f WHERE f.user_id = v_user AND f.confirmed_at IS NOT NULL)
  THEN
    RETURN 'already_enrolled';
  END IF;
  UPDATE second_factors f SET secret_ct = p_secret_ct, proof_hash = sha256(p_proof),
                              last_step = 0, failed_attempts = 0, charges = '{{}}'
  WHERE f.user_id = v_user AND f.ticket_hash = sha256(p_ticket_proof)
    AND f.ticket_expires_at > now();
  -- The ticket is not used up here: an abandoned QR can be restarted within the hour.
  RETURN CASE WHEN FOUND THEN 'begun' ELSE 'invalid_ticket' END;
END $$""",
}


def install(functions: dict[str, str]) -> None:
    for signature, definition in functions.items():
        op.execute(definition)
        # Unchanged by CREATE OR REPLACE; restated so the grant is plain to see. REVOKE first.
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ziftbook_app")


def upgrade() -> None:
    install(FUNCTIONS)


def downgrade() -> None:
    install(FUNCTIONS_0083)
