"""Server-side sessions: an opaque random token in a cookie, only its hash in the database."""

import hashlib
import ipaddress
import json
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import limits, passwords
from app.db import SessionLocal, join_tenant, tenant_context
from app.errors import ApiError, Error

IDLE = timedelta(days=7)
ABSOLUTE = timedelta(days=30)
TOUCH_EVERY = timedelta(minutes=5)
SIGN_IN_WINDOW = timedelta(minutes=15)
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


Action = Literal[
    "setting_changed",
    "sign_in_succeeded",
    "sign_in_failed",
    "signed_out",
    "signed_out_everywhere",
    "password_reset_completed",
    "business_created",
    "service_created",
    "service_changed",
    "service_workers_changed",
    "member_role_changed",
    "member_removed",
    "member_display_name_changed",
    "working_hours_changed",
    "opening_hours_changed",
    "time_off_created",
    "time_off_changed",
    "time_off_deleted",
    "member_invited",
    "invite_revoked",
    "invite_accepted",
    "client_created",
    "client_changed",
    "consent_recorded",
    "booking_confirmed",
    "booking_declined",
    # ZIF-55. "cancelled_by_merchant", not "cancelled": ZIF-54 adds a client-side cancellation
    # next, and renaming an audit action once rows exist is not a rename. Every value above ends
    # in a past-tense verb, which is why the third is not "booking_no_show": the STATUS is
    # `no_show`, the act this row records is recording one.
    "booking_cancelled_by_merchant",
    "booking_completed",
    "booking_no_show_recorded",
]


def origin(request: Request) -> tuple[str | None, str | None]:
    """The requester's address and browser, from the connection and the header, never from a
    forwarding header (CONTRIBUTING.md, "Client IP"). Shared with the consent record (ZIF-49),
    whose Art. 7(1) evidence must come from the same source an audit event's does."""
    user_agent = request.headers.get("user-agent")
    return (
        _inet(request.client.host if request.client else None),
        user_agent[:512] if user_agent else None,
    )


def record(
    session: Session,
    request: Request,
    action: Action,
    *,
    actor_user_id: UUID | None,
    target: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    """Add an audit event to this transaction, in its tenant (none outside tenant_context).

    target names what the event is about ("user:<id>", "setting:<key>"); details holds what changed,
    such as a setting's old and new value. Never a password, token, cookie, email or personal data.
    """
    ip, user_agent = origin(request)
    # No RETURNING: an event of no tenant isn't visible to the app, not even to the one adding it.
    session.execute(
        text("""
        INSERT INTO audit_events (actor_user_id, action, target, details, ip, user_agent)
        VALUES (:actor_user_id, :action, :target, CAST(:details AS jsonb), :ip, :user_agent)
        """),
        {
            "actor_user_id": actor_user_id,
            "action": action,
            "target": target,
            # Left as SQL NULL, not a JSON null, when the event carries no values.
            "details": None if details is None else json.dumps(details),
            "ip": ip,
            "user_agent": user_agent,
        },
    )


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
        # Its own transaction: the tenant for the endpoint's transaction is only known after it.
        # Accepted: a sign-out or membership removal committing while this runs lets this one
        # request through; the next is rejected.
        with SessionLocal.begin() as session:
            row = session.execute(
                RESOLVE, {"id_hash": hash_token(token), "idle": IDLE, "touch_every": TOUCH_EVERY}
            ).first()
    if row is None:
        raise ApiError(401, "unauthenticated")
    # For the access line: this dependency runs in a worker thread whose context the middleware
    # never sees.
    request.state.tenant_id = row.tenant_id
    with tenant_context(row.tenant_id) as db:
        yield SignedIn(row.user_id, row.tenant_id, row.role, db)


# scope="function": the endpoint's transaction commits before the response is sent, so a failed
# commit is an error response instead of a success that didn't happen.
CurrentSession = Annotated[SignedIn, Depends(signed_in, scope="function")]


def owner(current: CurrentSession) -> SignedIn:
    # A dependency, not a check in the handler: FastAPI runs it before validating the body, so
    # anyone else gets 403 whatever they send.
    if current.role != "owner":
        raise ApiError(403, "owner_only")
    return current


CurrentOwner = Annotated[SignedIn, Depends(owner)]

router = APIRouter(prefix="/api", tags=["session"])


class SessionOut(BaseModel):
    user_id: UUID
    tenant_id: UUID
    member_id: UUID
    role: str
    email: str
    display_name: str | None
    business_name: str
    currency: str


def describe(db: Session, user_id: UUID) -> SessionOut:
    """The signed-in person as the web app shows them; db is a tenant_context for their business."""
    row = db.execute(
        text("""
        SELECT m.user_id, m.tenant_id, m.id AS member_id, m.role, m.display_name, u.email,
               t.name AS business_name, t.currency
        FROM memberships m JOIN users u ON u.id = m.user_id JOIN tenants t ON t.id = m.tenant_id
        WHERE m.user_id = :user_id
        """),
        {"user_id": user_id},
    ).first()
    if row is None:  # the membership was removed while this request was under way
        raise ApiError(401, "unauthenticated")
    return SessionOut.model_validate(row, from_attributes=True)


@router.get("/session", responses={401: {"model": Error}})
def read(current: CurrentSession, response: Response) -> SessionOut:
    # One person's identity: never for a shared cache.
    response.headers["Cache-Control"] = "no-store"
    return describe(current.db, current.user_id)


def start(session: Session, request: Request, user_id: UUID) -> str:
    """Sign user_id in on this browser: whatever session it had ends here, whoever it belonged to.

    session is a tenant_context for the business; returns the new cookie's token.
    """
    if old := request.cookies.get(COOKIE):
        session.execute(
            text("DELETE FROM sessions WHERE id_hash = :id_hash"), {"id_hash": hash_token(old)}
        )
    token = create(
        session,
        user_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    record(session, request, "sign_in_succeeded", actor_user_id=user_id)
    return token


class Credentials(BaseModel):
    email: passwords.Email
    password: str = Field(min_length=1, max_length=passwords.MAX_PASSWORD)


@router.post(
    "/session",
    responses={status: {"model": Error} for status in (401, 403, 415, 422, 429, 503)},
)
def sign_in(credentials: Credentials, request: Request, response: Response) -> SessionOut:
    """Sign in with email and password, into the oldest business the person belongs to.

    An unknown email and a wrong password get the same answer, and cost the same time.
    """
    ip = request.client.host if request.client else None
    # Every attempt counts, before anything else, so a correct password doesn't get past the limit.
    if limits.hit(
        {
            limits.email_key("sign_in", credentials.email): 10,
            limits.ip_key("sign_in", ip): 50,
        },
        SIGN_IN_WINDOW,
    ):
        raise ApiError(429, "rate_limited")
    with SessionLocal.begin() as session:
        account = session.execute(
            text("SELECT * FROM account_by_email(:email)"), {"email": credentials.email}
        ).first()
    # No database connection is held while hashing.
    if not passwords.verify(account.password_hash if account else None, credentials.password):
        # One insert either way; the account, if any, only as its id. Never the email typed.
        with SessionLocal.begin() as session:
            target = f"user:{account.user_id}" if account else None
            record(session, request, "sign_in_failed", actor_user_id=None, target=target)
        raise ApiError(401, "invalid_credentials")
    assert account is not None
    if account.tenant_id is None:
        raise ApiError(403, "no_tenant")
    with tenant_context(account.tenant_id) as session:
        if not session.scalar(
            text("SELECT password_unchanged(:user_id, :hash)"),
            {"user_id": account.user_id, "hash": account.password_hash},
        ):  # reset while it was being checked
            raise ApiError(401, "invalid_credentials")
        try:
            token = start(session, request, account.user_id)
        except ValueError:  # the membership went away since the lookup
            raise ApiError(403, "no_tenant") from None
        signed_in_as = describe(session, account.user_id)
    set_cookie(response, token)
    response.headers["Cache-Control"] = "no-store"
    return signed_in_as


@router.delete("/session", status_code=204, responses={415: {"model": Error}})
def sign_out(request: Request, response: Response) -> None:
    """Sign out this browser. Needs no live session: an expired cookie is cleared all the same."""
    token = request.cookies.get(COOKIE)
    if token:
        with SessionLocal.begin() as session:
            ended = session.execute(
                text("DELETE FROM sessions WHERE id_hash = :id_hash RETURNING tenant_id, user_id"),
                {"id_hash": hash_token(token)},
            ).first()
            if ended:
                join_tenant(session, ended.tenant_id)
                record(session, request, "signed_out", actor_user_id=ended.user_id)
    clear_cookie(response)


@router.delete(
    "/sessions", status_code=204, responses={401: {"model": Error}, 415: {"model": Error}}
)
def sign_out_everywhere(current: CurrentSession, request: Request, response: Response) -> None:
    """Sign the user out of every session, in every tenant."""
    current.db.execute(text("DELETE FROM sessions WHERE user_id = :id"), {"id": current.user_id})
    record(current.db, request, "signed_out_everywhere", actor_user_id=current.user_id)
    clear_cookie(response)
