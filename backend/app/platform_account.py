"""A person's own ziftbook account, with or without a business (ZIF-146).

An account session is its own cookie (auth.ACCOUNT_COOKIE) and its own table (0057), never a
business session: no console route reads it, and these routes never read the business one. The app
reaches account_sessions only through definer functions keyed by the session hash.

Logs: ids and outcome codes only; never an email, name, phone, token, hash or address.
"""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth, clients
from app.accounts import DisplayNameText
from app.db import SessionLocal
from app.errors import ApiError, Error

router = APIRouter(prefix="/api/account", tags=["platform-account"])


class AccountOut(BaseModel):
    """The signed-in person's own details, to fill in a booking form. Nothing else, ever: the
    directory-ban test pins these three fields."""

    email: str
    name: str | None
    phone: str | None  # E.164


@dataclass(frozen=True)
class AccountSession:
    user_id: UUID
    email: str
    name: str | None
    phone: str | None
    fresh: bool  # started within the last 24 hours (0057)
    id_hash: bytes

    def out(self) -> AccountOut:
        return AccountOut(email=self.email, name=self.name, phone=self.phone)


RESOLVE = text("SELECT * FROM resolve_account_session(:h)")


def account_session(request: Request) -> AccountSession:
    """The live account session on this request, or 401. Never the business session."""
    token = request.cookies.get(auth.ACCOUNT_COOKIE)
    row = None
    if token:
        id_hash = auth.hash_token(token)
        with SessionLocal.begin() as session:
            row = session.execute(RESOLVE, {"h": id_hash}).first()
    if row is None:
        raise ApiError(401, "unauthenticated")
    return AccountSession(row.user_id, row.email, row.name, row.phone, row.fresh, id_hash)


CurrentAccount = Annotated[AccountSession, Depends(account_session)]


def same_origin(request: Request) -> None:
    """Personal data answers only our own pages (Fetch Metadata): 403 cross_site unless
    Sec-Fetch-Site is absent (an old browser, a server) or same-origin. It stops another site's
    navigation, embedding and any future CORS slip; script running on our own origin is the page
    CSP's to stop."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site != "same-origin":
        raise ApiError(403, "cross_site")


def private(response: Response) -> None:
    """One person's data: never for a shared cache, and never served for another cookie."""
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Cookie"


@router.post(
    "/session",
    name="sign_in",
    responses={s: {"model": Error} for s in (401, 403, 415, 422, 429, 503)},
)
def sign_in(credentials: auth.Credentials, request: Request, response: Response) -> AccountOut:
    """Sign in to the account, with or without a business. Business sign-in's limits, challenge and
    failure count (auth.check_credentials): one budget of guesses for a person across both.

    A platform admin gets no account session (403 account_unavailable, after a correct password
    only). This browser's previous account session ends; so does a business session of a different
    person (one browser, one person); one of the same person's stays."""
    account = auth.check_credentials(credentials, request)
    with SessionLocal.begin() as session:
        token = auth.start_account(session, request, account.user_id, account.password_hash)
        if token is None:
            # The previous person's account session is already ended (check_credentials).
            raise ApiError(403, "account_unavailable", headers=auth.account_cookie_cleared())
        ended_other = end_other_persons_business_session(session, request, account.user_id)
        auth.record(
            session,
            request,
            "account_signed_in",
            actor_user_id=account.user_id,
            target=f"user:{account.user_id}",
        )
        row = session.execute(RESOLVE, {"h": auth.hash_token(token)}).one()
    auth.set_account_cookie(response, token)
    if ended_other:
        auth.clear_cookie(response)
    response.headers["Cache-Control"] = "no-store"
    return AccountOut(email=row.email, name=row.name, phone=row.phone)


def end_other_persons_business_session(session: Session, request: Request, user_id: UUID) -> bool:
    """Delete this browser's business session if it belongs to someone other than user_id; True if
    one was deleted. The app role may delete sessions (auth.start does the same)."""
    token = request.cookies.get(auth.COOKIE)
    if not token:
        return False
    ended = session.execute(
        text("DELETE FROM sessions WHERE id_hash = :h AND user_id <> :u RETURNING 1"),
        {"h": auth.hash_token(token), "u": user_id},
    ).first()
    return ended is not None


@router.delete("/session", name="sign_out", status_code=204, responses={415: {"model": Error}})
def sign_out(request: Request, response: Response) -> None:
    """Sign out this browser, of the account and of the business (any sign-out ends both). Needs no
    live session: an expired cookie is cleared all the same."""
    auth.end_browser_sessions(request, response)


@router.delete(
    "/sessions",
    name="sign_out_everywhere",
    status_code=204,
    responses={s: {"model": Error} for s in (401, 415)},
)
def sign_out_everywhere(current: CurrentAccount, request: Request, response: Response) -> None:
    """End every session of this person, of both kinds, on every browser."""
    user_id = current.user_id
    with SessionLocal.begin() as session:
        if not auth.end_every_session(session, user_id, current.id_hash):
            raise ApiError(401, "unauthenticated")  # signed out while this request was under way
        auth.record(
            session,
            request,
            "signed_out_everywhere",
            actor_user_id=user_id,
            target=f"user:{user_id}",
        )
    auth.clear_cookie(response)
    auth.clear_account_cookie(response)


@router.get(
    "",
    name="read",
    dependencies=[Depends(same_origin)],
    responses={s: {"model": Error} for s in (401, 403)},
)
def read(current: CurrentAccount, response: Response) -> AccountOut:
    """The signed-in person's own name, email and phone, to fill in a booking form. Never by a
    typed email."""
    private(response)
    return current.out()


class AccountChange(BaseModel):
    """Only the fields sent change. phone: null clears it; name cannot be cleared."""

    model_config = ConfigDict(strict=True, extra="forbid")
    name: DisplayNameText | None = None
    phone: clients.Phone | None = None


@router.patch(
    "",
    name="update",
    responses={s: {"model": Error} for s in (401, 415, 422)},
)
def update(
    change: AccountChange, current: CurrentAccount, request: Request, response: Response
) -> AccountOut:
    """Change the person's own name and phone. The phone is international (+31...): an account has
    no country to read a local number in. No business's copy changes: each refreshes when the
    person next books there (copy at booking, ZIF-99), and no membership's display name changes."""
    private(response)
    sent = change.model_fields_set
    if "name" in sent and change.name is None:
        raise ApiError(422, "invalid_request")
    phone = None
    if "phone" in sent and change.phone is not None:
        phone = clients.phone_number(change.phone, None)
        if phone is None:
            raise ApiError(422, "phone_invalid")
    with SessionLocal.begin() as session:
        # Only the fields sent are written (code review N3): a name-only edit never writes back a
        # phone read before another tab changed it.
        row = session.execute(
            text("SELECT * FROM set_own_profile(:h, :name, :phone, :set_phone)"),
            {
                "h": current.id_hash,
                "name": change.name if "name" in sent else None,
                "phone": phone,
                "set_phone": "phone" in sent,
            },
        ).first()
        if row is None:  # signed out while this request was under way
            raise ApiError(401, "unauthenticated")
        changed: dict[str, object] = {
            key: "changed"
            for key, before, after in (
                ("name", row.previous_name, row.name),
                ("phone", row.previous_phone, row.phone),
            )
            if before != after
        }
        if changed:
            # The field names only, never the values (as clients.update_client).
            auth.record(
                session,
                request,
                "account_details_changed",
                actor_user_id=current.user_id,
                target=f"user:{current.user_id}",
                details=changed,
            )
    return AccountOut(email=row.email, name=row.name, phone=row.phone)
