"""Platform admins (ZIF-70): their audit record, and the grant/revoke CLI.

    python -m app.admin grant|revoke <email> --reason "<why>"

The CLI connects as ziftbook_migrate (ZIF_MIGRATE_DATABASE_URL): the app role has no grant on
platform_admins. One transaction per run writes the change and its audit event.
"""

import argparse
import json
import logging
import os
import sys
from typing import Literal
from uuid import UUID

from fastapi import Request
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import logs, passwords
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


def main(argv: list[str] | None = None) -> int:
    """0 done (or nothing to do), 1 no account with that email (or any uncaught error, logged
    CRITICAL), 2 usage error (argparse)."""
    parser = argparse.ArgumentParser(prog="python -m app.admin", description=__doc__)
    parser.add_argument("command", choices=("grant", "revoke"))
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
        msg = "platform admin granted" if grant else "platform admin revoked"
        logger.info(msg, extra={"user_id": str(user_id)})
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    logs.configure()
    sys.exit(main())
