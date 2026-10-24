"""Platform admins (ZIF-70): their audit record, and the grant/revoke/enrol CLI.

    python -m app.admin grant|revoke|enrol <email> --reason "<why>"

The CLI connects as ziftbook_migrate (ZIF_MIGRATE_DATABASE_URL): the app role has no grant on
platform_admins or second_factors. One transaction per run writes the change and its audit event.
enrol (ZIF-71) wipes the admin's second factor, ends their admin sessions and shows a one-hour
enrolment ticket on the terminal.
"""

import argparse
import json
import logging
import os
import sys
from typing import Literal
from uuid import UUID

from fastapi import Request
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import logs, passwords, totp
from app.auth import origin

logger = logging.getLogger("app.admin")  # not __name__: "__main__" under python -m

Action = Literal[
    "admin_granted",
    "admin_revoked",
    "setup_completed",
    "admin_signed_in",
    "admin_sign_in_failed",
    "admin_signed_out",
    "business_read",
    "totp_enrolment_issued",
    "totp_enrolment_failed",
    "totp_enrolled",
    "second_factor_passed",
    "second_factor_failed",
    "second_factor_locked",
    "second_factor_unlocked",
    "recovery_code_used",
]
# A write needs a reason: the database refuses one without (platform_audit_events.reason_on_write).
KIND: dict[Action, Literal["auth", "read", "write"]] = {
    "admin_granted": "write",
    "admin_revoked": "write",
    "setup_completed": "auth",
    "admin_signed_in": "auth",
    "admin_sign_in_failed": "auth",
    "admin_signed_out": "auth",
    "business_read": "read",
    "totp_enrolment_issued": "write",
    "totp_enrolment_failed": "auth",
    "totp_enrolled": "auth",
    "second_factor_passed": "auth",
    "second_factor_failed": "auth",
    "second_factor_locked": "auth",
    "second_factor_unlocked": "auth",
    "recovery_code_used": "auth",
}


def record(
    session: Session,
    request: Request | None,
    action: Action,
    *,
    actor_user_id: UUID | None,
    tenant_id: UUID | None = None,
    target: str | None = None,
    reason: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    """Add a platform audit event to this transaction.

    As auth.record: never a password, token, cookie, email or personal data in target, reason or
    details. No RETURNING: the app role cannot read the table.
    """
    ip, user_agent = origin(request) if request else (None, None)
    session.execute(
        text("""
        INSERT INTO platform_audit_events
          (actor_user_id, action, kind, tenant_id, target, reason, details, ip, user_agent)
        VALUES (:actor_user_id, :action, :kind, :tenant_id, :target, :reason,
                CAST(:details AS jsonb), :ip, :user_agent)
        """),
        {
            "actor_user_id": actor_user_id,
            "action": action,
            "kind": KIND[action],
            "tenant_id": tenant_id,
            "target": target,
            "reason": reason,
            "details": None if details is None else json.dumps(details),
            "ip": ip,
            "user_agent": user_agent,
        },
    )


def _reason(value: str) -> str:
    # The same rule as the reason_on_write CHECK, so a bad reason is a usage error, not a traceback.
    if not value.strip() or len(value) > 500:
        raise argparse.ArgumentTypeError("give a reason of 1 to 500 characters")
    return value


ISSUE_TICKET = text("""
INSERT INTO second_factors (user_id, ticket_hash, ticket_expires_at)
VALUES (:u, sha256(:ticket_proof), now() + interval '1 hour')
ON CONFLICT (user_id) DO UPDATE SET
  ticket_hash = excluded.ticket_hash, ticket_expires_at = excluded.ticket_expires_at,
  secret_ct = NULL, proof_hash = NULL, confirmed_at = NULL, last_step = 0, failed_attempts = 0,
  recovery_code_hashes = '{}'
""")


TTY = "/dev/tty"


class NoTerminal(Exception):
    """There is no controlling terminal to show the enrolment ticket on."""


def show_ticket(ticket: str) -> None:
    """Write the ticket to the operator's terminal, not stdout: in a container stdout is the log
    stream, and logs ship off the host."""
    try:
        with open(TTY, "w") as tty:
            tty.write(f"Enrolment ticket, valid for 1 hour: {ticket}\n")
    except OSError as error:
        raise NoTerminal from error


def enrol(engine: Engine, email: str, reason: str) -> int:
    """Wipe the admin's second factor, end their admin sessions and show a one-hour enrolment
    ticket on the terminal. With no terminal nothing is issued: exit 2."""
    ticket = totp.new_code()
    try:
        return _enrol(engine, email, reason, ticket)
    except NoTerminal:
        print("enrolment needs a terminal: run it with -it", file=sys.stderr)
        return 2


def _enrol(engine: Engine, email: str, reason: str, ticket: str) -> int:
    with Session(engine) as session, session.begin():
        user_id = session.scalar(
            text(
                "SELECT u.id FROM users u JOIN platform_admins a ON a.user_id = u.id "
                "WHERE u.email = :e"
            ),
            {"e": email},
        )
        if user_id is None:
            logger.warning("second factor enrolment refused", extra={"outcome": "not_admin"})
            return 1
        # second_factors first, admin_sessions second: the order the 0039 functions lock them in,
        # so a run racing an admin's code can't deadlock (40P01).
        session.execute(
            ISSUE_TICKET, {"u": user_id, "ticket_proof": totp.proof(totp.normalise(ticket))}
        )
        session.execute(text("DELETE FROM admin_sessions WHERE user_id = :u"), {"u": user_id})
        record(
            session,
            None,
            "totp_enrolment_issued",
            actor_user_id=None,
            target=f"user:{user_id}",
            reason=reason,
            details={"via": "cli"},
        )
        show_ticket(ticket)  # before the commit: no terminal raises and rolls everything back
    logger.info("second factor enrolment issued", extra={"user_id": str(user_id)})
    return 0


def main(argv: list[str] | None = None) -> int:
    """0 done (or nothing to do), 1 no account with that email, or for enrol no admin with it (or
    any uncaught error, logged CRITICAL), 2 usage error (argparse) or, for enrol, no terminal."""
    parser = argparse.ArgumentParser(prog="python -m app.admin", description=__doc__)
    parser.add_argument("command", choices=("grant", "revoke", "enrol"))
    parser.add_argument("email")
    parser.add_argument("--reason", required=True, type=_reason, help="why, for the audit log")
    args = parser.parse_args(argv)
    try:
        email = passwords.normalise_email(args.email)
    except ValueError:
        parser.error("not an email address")

    engine = create_engine(
        os.environ["ZIF_MIGRATE_DATABASE_URL"], poolclass=NullPool, hide_parameters=True
    )
    try:
        if args.command == "enrol":
            return enrol(engine, email, args.reason)
        with Session(engine) as session, session.begin():
            user_id = session.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": email})
            if user_id is None:
                logger.warning("platform admin unchanged", extra={"outcome": "no_account"})
                return 1
            grant = args.command == "grant"
            if grant:
                changed = session.scalar(
                    text(
                        "INSERT INTO platform_admins (user_id) VALUES (:u) "
                        "ON CONFLICT (user_id) DO NOTHING RETURNING user_id"
                    ),
                    {"u": user_id},
                )
            else:
                changed = session.scalar(
                    text("DELETE FROM platform_admins WHERE user_id = :u RETURNING user_id"),
                    {"u": user_id},
                )
            if changed is None:
                outcome = "already_admin" if grant else "not_admin"
                logger.info(
                    "platform admin unchanged", extra={"outcome": outcome, "user_id": str(user_id)}
                )
                return 0
            action: Action = "admin_granted" if grant else "admin_revoked"
            record(
                session,
                None,
                action,
                actor_user_id=None,
                target=f"user:{user_id}",
                reason=args.reason,
                details={"via": "cli"},
            )
        msg = (
            "platform admin granted; next: python -m app.admin enrol"
            if grant
            else "platform admin revoked"
        )
        logger.info(msg, extra={"user_id": str(user_id)})
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    logs.configure()
    sys.exit(main())
