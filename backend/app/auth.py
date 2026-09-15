"""Server-side sessions: an opaque random token in a cookie, only its hash in the database."""

import hashlib
import ipaddress
import secrets
from datetime import timedelta
from uuid import UUID

from fastapi import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

ABSOLUTE = timedelta(days=30)
COOKIE = "__Host-session"  # __Host-: Secure, Path=/ and no Domain, so no subdomain can set it


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def create(session: Session, user_id: UUID, *, ip: str | None, user_agent: str | None) -> str:
    """Start a session for user_id in the session's tenant and return the cookie's token.

    `session` is a tenant_context: the membership (and so the tenant and role) comes from it.
    """
    token = secrets.token_urlsafe(32)
    inserted = session.scalar(
        text("""
        INSERT INTO sessions (id_hash, tenant_id, user_id, role, expires_at, ip, user_agent)
        SELECT :id_hash, tenant_id, user_id, role, now() + :lifetime, :ip, :user_agent
        FROM memberships WHERE user_id = :user_id
        RETURNING true
        """),
        {
            "id_hash": hash_token(token),
            "user_id": user_id,
            "lifetime": ABSOLUTE,
            "ip": _inet(ip),
            "user_agent": user_agent[:512] if user_agent else None,
        },
    )
    if inserted is None:
        raise ValueError("the user is not a member of this tenant")
    # ponytail: purges on every sign-in; move to the ZIF-5 sweeper. Rows that went idle stay until
    # their absolute expiry. After the insert and SKIP LOCKED: waiting on rows another transaction
    # holds (a membership being removed) would queue sign-ins or deadlock.
    session.execute(
        text("""
        DELETE FROM sessions WHERE id_hash IN (
            SELECT id_hash FROM sessions WHERE expires_at <= now() FOR UPDATE SKIP LOCKED)
        """)
    )
    return token


def _inet(host: str | None) -> str | None:
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return None
    # Postgres inet has no IPv6 zone ids ("fe80::1%eth0").
    return None if getattr(address, "scope_id", None) else str(address)


def set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=int(ABSOLUTE.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
