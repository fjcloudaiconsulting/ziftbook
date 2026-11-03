"""Server-side sessions: an opaque random token in a cookie, only its hash in the database."""

import hashlib
import ipaddress
import json
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, Literal, get_args
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app import limits, passwords, turnstile
from app.db import SessionLocal, join_tenant, tenant_context
from app.errors import ApiError, Error
from app.locales import Locale

IDLE = timedelta(days=7)
ABSOLUTE = timedelta(days=30)
TOUCH_EVERY = timedelta(minutes=5)
SIGN_IN_WINDOW = timedelta(minutes=15)
# ZB-01: the hard cap on an account is per address (IPv6 /64), so nobody elsewhere can use it up;
# the account's failures from anywhere ask for a challenge instead of locking it. Admin sign-in
# spends the same attempts (attempt_key): one address gets this many at an email across both.
PER_ADDRESS_LIMIT = 10
# ZB-18: one more than one address gets across both sign-in routes, so one address alone never
# challenges an account through them. Admin sign-in's too: an admin's password works on both
# routes, both challenges count both routes' failures, and a threshold of its own for admins would
# tell who is one.
FAILURES_BEFORE_CHALLENGE = PER_ADDRESS_LIMIT + 1
# Every action that counts wrong passwords (count_failure). Each challenge adds them all up, for
# every email alike: guesses spread over the routes get no further than one route allows.
PasswordAction = Literal["sign_in", "admin_sign_in"]
PASSWORD_ACTIONS: tuple[PasswordAction, ...] = get_args(PasswordAction)
COOKIE = "__Host-session"  # __Host-: Secure, Path=/ and no Domain, so no subdomain can set it


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def create(session: Session, user_id: UUID, *, ip: str | None, user_agent: str | None) -> str:
    """Start a session for user_id in the session's tenant and return the cookie's token.

    `session` is a tenant_context: the membership (and so the tenant and role) comes from it.
    """
    token = secrets.token_urlsafe(32)
    # FOR SHARE: a sign-in racing a removal (target() holds the row FOR UPDATE) waits, then
    # re-reads the row; an ended one yields no row, so the caller's ValueError answers 403
    # no_tenant instead of a foreign key 500. The filter alone is not enough: an unlocked read sees
    # the old row.
    inserted = session.scalar(
        text("""
        INSERT INTO sessions (id_hash, tenant_id, user_id, role, expires_at, ip, user_agent)
        SELECT :id_hash, tenant_id, user_id, role, now() + :lifetime, :ip, :user_agent
        FROM memberships WHERE user_id = :user_id AND role <> 'removed' FOR SHARE
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
    "user_name_set",
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
    # ZIF-57: a member books or moves on the calendar.
    "booking_created",
    "booking_rescheduled",
    # ZIF-158: an owner connects the business to Stripe, first time only.
    "payment_account_created",
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


def resolve(request: Request) -> Row[Any]:
    """The request's session (tenant_id, user_id, role), or 401."""
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
    return row


def signed_in(request: Request) -> Iterator[SignedIn]:
    row = resolve(request)
    with tenant_context(row.tenant_id) as db:
        yield SignedIn(row.user_id, row.tenant_id, row.role, db)


# scope="function": the endpoint's transaction commits before the response is sent, so a failed
# commit is an error response instead of a success that didn't happen.
# Any signed-in person, named or not: only what a nameless person needs (read the session, give a
# name, sign out everywhere) takes this. Everything else takes CurrentSession.
AnySession = Annotated[SignedIn, Depends(signed_in, scope="function")]


def named(current: AnySession) -> SignedIn:
    # Deny by default (ZIF-131): read on every request, so a name removed while a session is live
    # gates it at once, and anything but a definite True (no row, NULL) denies.
    if (
        current.db.scalar(
            text("SELECT name IS NOT NULL /* name gate */ FROM users WHERE id = :u"),
            {"u": current.user_id},
        )
        is not True
    ):
        raise ApiError(403, "name_required")
    return current


CurrentSession = Annotated[SignedIn, Depends(named)]


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
    name: str | None  # the person's own; None until they give it (ZIF-131)
    locale: Locale | None  # the person's own language (ZIF-102); None: they chose none
    business_name: str
    currency: str
    slug: str  # ZIF-145: the console builds the public booking-page address from this


def describe(db: Session, user_id: UUID) -> SessionOut:
    """The signed-in person as the web app shows them; db is a tenant_context for their business."""
    row = db.execute(
        text("""
        SELECT m.user_id, m.tenant_id, m.id AS member_id, m.role, m.display_name, u.email, u.name,
               u.locale, t.name AS business_name, t.currency, t.slug
        FROM memberships m JOIN users u ON u.id = m.user_id JOIN tenants t ON t.id = m.tenant_id
        WHERE m.user_id = :user_id AND m.role <> 'removed'
        """),
        {"user_id": user_id},
    ).first()
    if row is None:  # the membership was removed while this request was under way
        raise ApiError(401, "unauthenticated")
    return SessionOut.model_validate(row, from_attributes=True)


@router.get("/session", responses={401: {"model": Error}})
def read(current: AnySession, response: Response) -> SessionOut:
    # One person's identity: never for a shared cache.
    response.headers["Cache-Control"] = "no-store"
    return describe(current.db, current.user_id)


def start(session: Session, request: Request, user_id: UUID) -> str:
    """Sign user_id in on this browser: whatever session it had ends here, whoever it belonged to.

    session is a tenant_context for the business; returns the new cookie's token.
    """
    # create() first: it may wait on the membership (a removal in flight), and must do so holding
    # no session row, or it deadlocks against members.remove deleting that row.
    token = create(
        session,
        user_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    if old := request.cookies.get(COOKIE):
        session.execute(
            text("DELETE FROM sessions WHERE id_hash = :id_hash"), {"id_hash": hash_token(old)}
        )
    record(session, request, "sign_in_succeeded", actor_user_id=user_id)
    return token


class Credentials(BaseModel):
    email: passwords.Email
    password: str = Field(min_length=1, max_length=passwords.MAX_PASSWORD)
    # Only looked at once the account has FAILURES_BEFORE_CHALLENGE recent failures (ZB-01).
    turnstile_token: Annotated[str, StringConstraints(max_length=turnstile.MAX_TOKEN)] | None = None


def limit_attempt(
    email: str, address_key: str, turnstile_token: str | None, ip: str | None
) -> None:
    """Sign-in's checks on a password attempt for email, after the caller's own address limit.

    At most PER_ADDRESS_LIMIT attempts (attempt_key) from address_key, the caller's default ip_key:
    IPv6 per /64, not the address limit's /48, which is shared too widely to key one person's
    sign-in on. Once the account has FAILURES_BEFORE_CHALLENGE recent wrong passwords from
    anywhere, admin sign-in included (count_failure), a Turnstile challenge, never a lock. The
    failures are only read here: an attempt refused before its password is checked adds none.
    """
    if limits.hit({attempt_key(email, address_key): PER_ADDRESS_LIMIT}, SIGN_IN_WINDOW):
        raise ApiError(429, "rate_limited")
    require_challenge("sign_in", email, turnstile_token, ip, FAILURES_BEFORE_CHALLENGE)


def attempt_key(email: str, address_key: str) -> str:
    """The key of the PER_ADDRESS_LIMIT password attempts at email from address_key (an ip_key).
    Admin sign-in charges business sign-in's, address_key ip_key("sign_in", ip), with its own."""
    return limits.email_key("sign_in", f"{email}|{address_key}")


def require_challenge(
    action: str, email: str, turnstile_token: str | None, ip: str | None, threshold: int
) -> None:
    """429 challenge_required once email's recent failures (count_failure), summed over
    PASSWORD_ACTIONS, reach threshold, unless turnstile_token passes for action. Only reads the
    failures."""
    failures = limits.count_all(
        [limits.email_key(f"{counted}_failed", email) for counted in PASSWORD_ACTIONS],
        SIGN_IN_WINDOW,
    )
    # No token is refused before verify(): with Turnstile switched off, verify() passes anything.
    if failures >= threshold and (
        not turnstile_token or not turnstile.verify(turnstile_token, ip, action=action)
    ):
        raise ApiError(429, "challenge_required")


def count_failure(
    email: str, action: PasswordAction = "sign_in", threshold: int = FAILURES_BEFORE_CHALLENGE
) -> bool:
    """A wrong password for email (an unknown email too): one more towards its challenge. Only a
    PASSWORD_ACTIONS action: any other would count toward no challenge.

    True when this failure is the one that reaches threshold.
    """
    return limits.charge(limits.email_key(f"{action}_failed", email), SIGN_IN_WINDOW) == threshold


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
    # Alone: an address over its limit charges no account's key.
    if limits.hit(
        {limits.ip_key("sign_in", ip, v6_prefix=limits.AUTH_V6_PREFIX): 50}, SIGN_IN_WINDOW
    ):
        raise ApiError(429, "rate_limited")
    limit_attempt(credentials.email, limits.ip_key("sign_in", ip), credentials.turnstile_token, ip)
    with SessionLocal.begin() as session:
        account = session.execute(
            text("SELECT * FROM account_by_email(:email)"), {"email": credentials.email}
        ).first()
    # No database connection is held while hashing.
    if not passwords.verify(account.password_hash if account else None, credentials.password):
        count_failure(credentials.email)
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
def sign_out_everywhere(current: AnySession, request: Request, response: Response) -> None:
    """Sign the user out of every session, in every tenant."""
    current.db.execute(text("DELETE FROM sessions WHERE user_id = :id"), {"id": current.user_id})
    record(current.db, request, "signed_out_everywhere", actor_user_id=current.user_id)
    clear_cookie(response)
