"""Platform admin sessions (ZIF-70): sign-in, the /api/admin guard, and audited business access.

An admin session is its own cookie and its own table, never a business session: it needs no
membership, and the database fixes its lifetimes (migration 0037). The app reaches admin_sessions
only through definer functions.

Every /api/admin route but sign-in sits on admin_router, whose guard answers 401 before the
handler, or its body, is looked at. ZIF-70 ships no data route: tests/test_admin_routes.py pins the
list until ZIF-71's second factor exists.
"""

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import admin, auth, limits, passwords
from app.db import SessionLocal, tenant_context
from app.errors import ApiError, Error

ADMIN_COOKIE = "__Host-admin-session"
# The cookie's max_age only: the database decides when a session ends (0037).
ADMIN_ABSOLUTE = timedelta(hours=8)

RESOLVE = text("SELECT * FROM resolve_admin_session(:id_hash)")


class AdminSession(BaseModel):
    user_id: UUID
    email: str
    name: str | None


def set_admin_cookie(response: Response, token: str) -> None:
    # SameSite=Strict, unlike the business cookie: no admin action starts from another site's link.
    response.set_cookie(
        ADMIN_COOKIE,
        token,
        max_age=int(ADMIN_ABSOLUTE.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_admin_cookie(response: Response) -> None:
    response.delete_cookie(ADMIN_COOKIE, path="/", secure=True, httponly=True, samesite="strict")


def admin_signed_in(request: Request) -> AdminSession:
    token = request.cookies.get(ADMIN_COOKIE)
    row = None
    if token:
        # No db session is yielded: there is no admin tenant. business() opens one per business.
        with SessionLocal.begin() as session:
            row = session.execute(RESOLVE, {"id_hash": auth.hash_token(token)}).first()
    if row is None:
        raise ApiError(401, "unauthenticated")
    request.state.admin_user_id = row.user_id  # for the access line
    return AdminSession.model_validate(row, from_attributes=True)


CurrentAdmin = Annotated[AdminSession, Depends(admin_signed_in)]

# Every route here is guarded by the router itself, so one added later can't forget it.
admin_router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(admin_signed_in)]
)
# Only sign-in: nobody holds an admin session before it.
admin_sign_in_router = APIRouter(prefix="/api/admin", tags=["admin"])


@admin_sign_in_router.post(
    "/session",
    responses={status: {"model": Error} for status in (401, 415, 422, 429, 503)},
)
def sign_in(credentials: auth.Credentials, request: Request, response: Response) -> AdminSession:
    """Sign a platform admin in. An unknown email, a wrong password and someone who is not an admin
    get the same answer, after the same work."""
    ip = request.client.host if request.client else None
    # Every attempt counts, before anything else, so a correct password doesn't get past the limit.
    if limits.hit(
        {
            limits.email_key("admin_sign_in", credentials.email): 5,
            limits.ip_key("admin_sign_in", ip): 20,
        },
        auth.SIGN_IN_WINDOW,
    ):
        raise ApiError(429, "rate_limited")
    with SessionLocal.begin() as session:
        account = session.execute(
            text("SELECT * FROM account_by_email(:email)"), {"email": credentials.email}
        ).first()
    # No database connection is held while hashing; an unknown email costs the same hash.
    verified = passwords.verify(account.password_hash if account else None, credentials.password)
    token = secrets.token_urlsafe(32)
    id_hash = auth.hash_token(token)
    ip_address, user_agent = auth.origin(request)
    with SessionLocal.begin() as session:
        # Called on every path, the hash only when it was just verified: a failure does the same
        # work whichever check failed, so timing tells no one who is an admin.
        started = session.scalar(
            text("SELECT start_admin_session(:user_id, :hash, :id_hash, :ip, :user_agent)"),
            {
                "user_id": account.user_id if account else None,
                "hash": account.password_hash if account and verified else None,
                "id_hash": id_hash,
                "ip": ip_address,
                "user_agent": user_agent,
            },
        )
        if not started:
            target = f"user:{account.user_id}" if account else None
            admin.record(
                session, request, "admin_sign_in_failed", actor_user_id=None, target=target
            )
        else:
            # One transaction with the start: a session row never exists without its audit event.
            assert account is not None
            # Whatever admin session this browser had ends here, as auth.start does.
            if old := request.cookies.get(ADMIN_COOKIE):
                session.execute(
                    text("SELECT end_admin_session(:id_hash)"), {"id_hash": auth.hash_token(old)}
                )
            admin.record(session, request, "admin_signed_in", actor_user_id=account.user_id)
            signed_in_as = AdminSession.model_validate(
                session.execute(RESOLVE, {"id_hash": id_hash}).one(), from_attributes=True
            )
    if not started:
        raise ApiError(401, "invalid_credentials")
    set_admin_cookie(response, token)
    response.headers["Cache-Control"] = "no-store"
    return signed_in_as


@admin_router.get("/session", responses={401: {"model": Error}})
def read(current: CurrentAdmin, response: Response) -> AdminSession:
    # One person's identity: never for a shared cache.
    response.headers["Cache-Control"] = "no-store"
    return current


@admin_router.delete(
    "/session", status_code=204, responses={401: {"model": Error}, 415: {"model": Error}}
)
def sign_out(current: CurrentAdmin, request: Request, response: Response) -> None:
    """Sign this browser's admin session out. Guarded, unlike business sign-out: an expired cookie
    gets 401, so no route under /api/admin answers 2xx without an admin session."""
    with SessionLocal.begin() as session:
        ended = session.scalar(
            text("SELECT end_admin_session(:id_hash)"),
            {"id_hash": auth.hash_token(request.cookies[ADMIN_COOKIE])},
        )
        if ended:
            admin.record(session, request, "admin_signed_out", actor_user_id=ended)
    clear_admin_cookie(response)


@contextmanager
def business(
    current: AdminSession, request: Request, tenant_id: UUID, *, target: str
) -> Iterator[Session]:
    """One business's data, for an admin read: a tenant_context, with the read recorded first.

    The record is in the same transaction and before the yield: if it fails the read never runs,
    and a read that fails leaves no record. One business per transaction. 404 for an unknown one.
    """
    with tenant_context(tenant_id) as session:
        # No row-level security on tenants: this is a plain existence check.
        if session.scalar(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": tenant_id}) is None:
            raise ApiError(404, "not_found")
        admin.record(
            session,
            request,
            "business_read",
            actor_user_id=current.user_id,
            tenant_id=tenant_id,
            target=target,
        )
        request.state.tenant_id = tenant_id
        yield session
