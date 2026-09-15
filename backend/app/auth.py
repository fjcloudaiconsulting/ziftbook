"""Server-side sessions: an opaque random token in a cookie, only its hash in the database."""

import hashlib
import ipaddress
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal, tenant_context
from app.errors import ApiError, Error

IDLE = timedelta(days=7)
ABSOLUTE = timedelta(days=30)
TOUCH_EVERY = timedelta(minutes=5)
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


# One statement: the live session (neither idle nor past its absolute expiry), and last_seen_at
# refreshed only when older than TOUCH_EVERY, so an active user doesn't write on every request.
RESOLVE = text("""
WITH live AS (
    SELECT id_hash, tenant_id, user_id, role, last_seen_at FROM sessions
    WHERE id_hash = :id_hash AND expires_at > now() AND last_seen_at > now() - :idle
), touched AS (
    UPDATE sessions SET last_seen_at = now() FROM live
    WHERE sessions.id_hash = live.id_hash AND live.last_seen_at < now() - :touch_every
)
SELECT tenant_id, user_id, role FROM live
""")


@dataclass(frozen=True)
class SignedIn:
    user_id: UUID
    tenant_id: UUID
    role: str
    db: Session  # a tenant_context for the session's tenant


def signed_in(request: Request) -> Iterator[SignedIn]:
    token = request.cookies.get(COOKIE)
    row = None
    if token:
        # Its own transaction, committed now: the refresh sticks even if the endpoint fails.
        with SessionLocal.begin() as session:
            row = session.execute(
                RESOLVE, {"id_hash": hash_token(token), "idle": IDLE, "touch_every": TOUCH_EVERY}
            ).first()
    if row is None:
        raise ApiError(401, "unauthenticated")
    with tenant_context(row.tenant_id) as db:
        yield SignedIn(row.user_id, row.tenant_id, row.role, db)


# scope="function": the endpoint's transaction commits before the response is sent, so a failed
# commit is an error response instead of a success that didn't happen.
CurrentSession = Annotated[SignedIn, Depends(signed_in, scope="function")]

router = APIRouter(prefix="/api", tags=["session"])


class SessionOut(BaseModel):
    user_id: UUID
    tenant_id: UUID
    role: str


@router.get("/session", responses={401: {"model": Error}})
def read(current: CurrentSession) -> SessionOut:
    return SessionOut(user_id=current.user_id, tenant_id=current.tenant_id, role=current.role)


@router.delete("/session", status_code=204)
def sign_out(request: Request, response: Response) -> None:
    """Sign out this browser. Needs no live session: an expired cookie is cleared all the same."""
    token = request.cookies.get(COOKIE)
    if token:
        with SessionLocal.begin() as session:
            session.execute(
                text("DELETE FROM sessions WHERE id_hash = :id_hash"),
                {"id_hash": hash_token(token)},
            )
    clear_cookie(response)


@router.delete("/sessions", status_code=204, responses={401: {"model": Error}})
def sign_out_everywhere(current: CurrentSession, response: Response) -> None:
    """Sign the user out of every session, in every tenant."""
    current.db.execute(text("DELETE FROM sessions WHERE user_id = :id"), {"id": current.user_id})
    clear_cookie(response)
