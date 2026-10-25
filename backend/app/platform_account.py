"""A person's own ziftbook account, with or without a business (ZIF-146).

An account session is its own cookie (auth.ACCOUNT_COOKIE) and its own table (0057), never a
business session: no console route reads it, and these routes never read the business one. The app
reaches account_sessions only through definer functions keyed by the session hash.

Logs: ids and outcome codes only; never an email, name, phone, token, hash or address.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import accounts, auth, clients, limits, passwords
from app.accounts import DisplayNameText, LinkRequest
from app.db import SessionLocal
from app.errors import ApiError, Error
from app.services import Price

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
        # The lock order every writer keeps (CONTRIBUTING): password_credentials, then sessions,
        # then account_sessions. The stored hash first: a reset either already won or waits.
        if not session.scalar(
            auth.PASSWORD_UNCHANGED, {"user_id": account.user_id, "hash": account.password_hash}
        ):
            raise ApiError(401, "invalid_credentials", headers=auth.account_cookie_cleared())
        ended_other = end_other_persons_business_session(session, request, account.user_id)
        token = auth.start_account(session, request, account.user_id, account.password_hash)
        if token is None:
            # The previous person's account session is already ended (check_credentials).
            raise ApiError(403, "account_unavailable", headers=auth.account_cookie_cleared())
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


class Business(BaseModel):
    name: str
    slug: str  # the booking page's address: /{locale}/{slug}


class HistoryBooking(BaseModel):
    """One booking this account placed. Never an id (a capability on older payment routes), a note,
    a message or what was typed into the form: tests/test_directory_ban.py pins these fields."""

    business: Business
    starts_at: datetime
    ends_at: datetime
    status: str
    service_name: dict[str, str]  # all three locales, as snapshotted
    price: Price
    worker_display_name: str | None


class HistoryPage(BaseModel):
    bookings: list[HistoryBooking]  # newest first
    next: str | None  # the cursor of the next page; None on the last


HISTORY = text("SELECT * FROM own_bookings(:h, :before_starts_at, :before_key, :limit)")
HISTORY_WINDOW = timedelta(minutes=1)
# Every call walks every business: about 6 ms at 1,000 businesses and 58 ms at 10,000 (measured on
# the dev database, no bookings; ZIF-257 tracks walking only the person's own businesses). Per
# account a minute, and per address, so many cheap accounts behind one address cost no more.
HISTORY_LIMIT = 30
HISTORY_IP_LIMIT = 30


def cursor_of(starts_at: datetime, key: bytes) -> str:
    raw = json.dumps({"s": starts_at.isoformat(), "k": key.hex()}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_cursor(cursor: str) -> tuple[datetime, bytes]:
    """The (starts_at, key) a cursor carries, or 422: it is opaque, and never a booking id."""
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        starts_at = datetime.fromisoformat(raw["s"])
        key = bytes.fromhex(raw["k"])
    except ValueError, KeyError, TypeError, binascii.Error:
        raise ApiError(422, "invalid_request") from None
    if starts_at.tzinfo is None or len(key) != 32:
        raise ApiError(422, "invalid_request")
    return starts_at, key


@router.get(
    "/history",
    name="history",
    dependencies=[Depends(same_origin)],
    responses={s: {"model": Error} for s in (401, 403, 422, 429)},
)
def history(
    current: CurrentAccount,
    request: Request,
    response: Response,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> HistoryPage:
    """Every booking this account placed, at every business, past and upcoming, newest first. Only
    bookings the person placed while signed in: a guest booking made with their address, by anyone,
    never shows. Needs a session started within the last 24 hours (403
    reauthentication_required: sign in again)."""
    private(response)
    if not current.fresh:
        raise ApiError(403, "reauthentication_required")
    ip = request.client.host if request.client else None
    # The address first, in a hit() of its own: an address over its limit charges no account.
    if limits.hit({limits.ip_key("account_history", ip): HISTORY_IP_LIMIT}, HISTORY_WINDOW):
        raise ApiError(429, "rate_limited")
    if limits.hit(
        {limits.email_key("account_history", current.email): HISTORY_LIMIT}, HISTORY_WINDOW
    ):
        raise ApiError(429, "rate_limited")
    before_starts_at, before_key = parse_cursor(cursor) if cursor else (None, None)
    with SessionLocal.begin() as session:
        # Up to limit + 1 from each business: the newest limit + 1 overall are among them.
        rows = session.execute(
            HISTORY,
            {
                "h": current.id_hash,
                "before_starts_at": before_starts_at,
                "before_key": before_key,
                "limit": limit + 1,
            },
        ).all()
    rows = sorted(rows, key=lambda r: (r.starts_at, r.sort_key), reverse=True)
    page = rows[:limit]
    return HistoryPage(
        bookings=[
            HistoryBooking(
                business=Business(name=r.business_name, slug=r.business_slug),
                starts_at=r.starts_at,
                ends_at=r.ends_at,
                status=r.status,
                service_name=r.service_name,
                price=Price(amount_minor=r.price_amount_minor, currency=r.price_currency),
                worker_display_name=r.worker_display_name,
            )
            for r in page
        ],
        next=cursor_of(page[-1].starts_at, page[-1].sort_key) if len(rows) > limit else None,
    )


@router.post(
    "/sign-up",
    name="sign_up",
    status_code=202,
    responses={s: {"model": Error} for s in (403, 415, 422, 429)},
)
def sign_up(details: LinkRequest, request: Request) -> None:
    """Send a link that creates the person's own account, or, if the email has an account (a
    client's, a member's, an owner's), a note to sign in to it. The same answer for every email:
    the email job decides what the inbox gets. Business sign-up's limits and Turnstile action."""
    accounts.send_link("account_sign_up", details, request, limit_as="sign_up")


class AccountSignUpIn(BaseModel):
    token: str = Field(min_length=1, max_length=100)  # from the link's fragment
    password: str
    name: DisplayNameText  # never logged, never in an event
    phone: clients.Phone | None = None  # international (+31...): an account has no country


@router.post(
    "/sign-up/complete",
    name="complete_sign_up",
    status_code=201,
    responses={s: {"model": Error} for s in (400, 409, 415, 422, 503)},
)
def complete_sign_up(details: AccountSignUpIn, request: Request, response: Response) -> AccountOut:
    """Create the person's account from an account sign-up link, with no business, and sign them in
    to it. This browser's previous account session ends, and a business session of a different
    person (one browser, one person)."""
    from app import setup  # here: setup imports accounts, which this module imports

    # Before the link is touched, so it still works once setup is done (as business sign-up).
    if setup.needed():
        raise ApiError(409, "setup_required")
    password = passwords.check_new_password(details.password)
    phone = None
    if details.phone is not None:
        phone = clients.phone_number(details.phone, None)
        if phone is None:  # before the link is touched: it stays usable
            raise ApiError(422, "phone_invalid")
    digest = accounts.live_token(details.token, "account_sign_up")
    password_hash = passwords.hash_password(password)
    with SessionLocal.begin() as session:
        created = session.execute(
            text("SELECT * FROM complete_account_sign_up(:h, :p, :n, :phone)"),
            {"h": digest, "p": password_hash, "n": details.name, "phone": phone},
        ).one()
        if created.outcome == "created":
            auth.record(
                session,
                request,
                "account_created",
                actor_user_id=created.user_id,
                target=f"user:{created.user_id}",
            )
    # Outside the transaction: raising inside would roll back using up the link.
    if created.outcome == "invalid_token":
        raise ApiError(400, "invalid_token")
    if created.outcome == "already_registered":
        raise ApiError(409, "account_exists")
    auth.end_previous_account_session(request)
    # Two steps: if this one fails, the account exists and the person signs in normally.
    with SessionLocal.begin() as session:
        # The lock order every writer keeps (CONTRIBUTING): password_credentials, then sessions,
        # then account_sessions.
        if not session.scalar(
            auth.PASSWORD_UNCHANGED, {"user_id": created.user_id, "hash": password_hash}
        ):  # reset since it was created: the person signs in with the new password
            raise ApiError(401, "invalid_credentials", headers=auth.account_cookie_cleared())
        ended_other = end_other_persons_business_session(session, request, created.user_id)
        token = auth.start_account(session, request, created.user_id, password_hash)
        assert token is not None  # a new account is no platform admin
        auth.record(
            session,
            request,
            "account_signed_in",
            actor_user_id=created.user_id,
            target=f"user:{created.user_id}",
        )
        row = session.execute(RESOLVE, {"h": auth.hash_token(token)}).one()
    auth.set_account_cookie(response, token)
    if ended_other:
        auth.clear_cookie(response)
    response.headers["Cache-Control"] = "no-store"
    return AccountOut(email=row.email, name=row.name, phone=row.phone)
