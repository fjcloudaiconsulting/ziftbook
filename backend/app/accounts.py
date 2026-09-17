"""Creating an account: sign up with an email, then complete it from the emailed link."""

import unicodedata
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Request, Response
from pydantic import AfterValidator, BaseModel, Field, StringConstraints
from sqlalchemy import text

from app import auth, business_settings, limits, passwords
from app.business_settings import BusinessSettings, Locale
from app.countries import COUNTRIES, Country
from app.db import SessionLocal, join_tenant, tenant_context
from app.errors import ApiError, Error
from app.jobs import enqueue

LINK_WINDOW = timedelta(hours=1)

router = APIRouter(prefix="/api", tags=["account"])


class LinkRequest(BaseModel):
    email: passwords.Email
    locale: Locale  # the page the person asked on; the email's language if the account has none


def send_link(purpose: str, details: LinkRequest, request: Request) -> None:
    """Queue the email for a link. The same answer and the same work whether or not the email has an
    account: the email job decides what the inbox gets."""
    ip = request.client.host if request.client else None
    if limits.hit(
        {limits.email_key(purpose, details.email): 3, limits.ip_key(purpose, ip): 10}, LINK_WINDOW
    ):
        raise ApiError(429, "rate_limited")
    with SessionLocal.begin() as session:
        token_id = session.scalar(
            text("SELECT start_email_token(:purpose, :email, :locale)"),
            {"purpose": purpose, "email": details.email, "locale": details.locale},
        )
        enqueue(session, "email.token", f"email.token:{token_id}", {"token_id": str(token_id)})


@router.post("/sign-up", status_code=202, responses={s: {"model": Error} for s in (415, 422, 429)})
def sign_up(details: LinkRequest, request: Request) -> None:
    """Send a link that sets up a business, or, if the email has an account, a note to sign in."""
    send_link("sign_up", details, request)


def printable(name: str) -> str:
    # No control, format (zero-width) or unassigned characters: a name that looks empty, or that
    # the database refuses (NUL), is a 422, not a blank business or a 500. Zl/Zp (U+2028, U+2029)
    # are refused too: without them a business name could put a line break into an emailed invite.
    if any(
        unicodedata.category(c).startswith("C") or unicodedata.category(c) in ("Zl", "Zp")
        for c in name
    ):
        raise ValueError("unprintable characters")
    return name


BusinessName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    AfterValidator(printable),
]


class CompleteSignUp(BaseModel):
    token: str = Field(min_length=1, max_length=100)  # from the link's fragment
    password: str
    business_name: BusinessName
    # None until the sign-up page sends one (a later PR makes it required): the Netherlands.
    country: Country | None = None


def live_token(token: str, purpose: str) -> bytes:
    """The token's hash if it can still be used. Checked before any password is hashed, so a dead or
    made-up link costs nothing."""
    digest = auth.hash_token(token)
    with SessionLocal.begin() as session:
        if not session.scalar(
            text("SELECT email_token_live(:hash, :purpose)"), {"hash": digest, "purpose": purpose}
        ):
            raise ApiError(400, "invalid_token")
    return digest


@router.post(
    "/sign-up/complete",
    status_code=201,
    responses={s: {"model": Error} for s in (400, 409, 415, 422, 503)},
)
def complete_sign_up(
    details: CompleteSignUp, request: Request, response: Response
) -> auth.SessionOut:
    """Create the business and its owner from a sign-up link, and sign the owner in."""
    password = passwords.check_new_password(details.password)
    digest = live_token(details.token, "sign_up")
    password_hash = passwords.hash_password(password)
    country = details.country or "NL"
    defaults = COUNTRIES[country]
    with SessionLocal.begin() as session:
        created = session.execute(
            text("""SELECT * FROM complete_sign_up(
                    :hash, :password_hash, :business_name, :country, :currency)"""),
            {
                "hash": digest,
                "password_hash": password_hash,
                "business_name": details.business_name,
                "country": country,
                "currency": defaults.currency,
            },
        ).one()
        if created.outcome == "created":
            join_tenant(session, created.tenant_id)
            # Validated by the registry; only what the country decides, others keep their default.
            starting = BusinessSettings(timezone=defaults.timezone, language=defaults.language)
            for key, value in starting.model_dump(exclude_unset=True).items():
                business_settings.save(session, key, value)
            auth.record(session, request, "business_created", actor_user_id=created.user_id)
    # Outside the transaction: raising inside would roll back using up the link.
    if created.outcome == "invalid_token":  # used up since the liveness check
        raise ApiError(400, "invalid_token")
    if created.outcome == "already_registered":
        raise ApiError(409, "account_exists")
    # Two steps: if this one fails, the account exists and the person signs in normally.
    with tenant_context(created.tenant_id) as session:
        token = auth.start(session, request, created.user_id)
        signed_in_as = auth.describe(session, created.user_id)
    auth.set_cookie(response, token)
    response.headers["Cache-Control"] = "no-store"
    return signed_in_as


@router.post(
    "/password-reset", status_code=202, responses={s: {"model": Error} for s in (415, 422, 429)}
)
def request_password_reset(details: LinkRequest, request: Request) -> None:
    """Send a reset link; for an unknown email the email job sends nothing, after this answer."""
    send_link("password_reset", details, request)


class CompleteReset(BaseModel):
    token: str = Field(min_length=1, max_length=100)  # from the link's fragment
    password: str


@router.post(
    "/password-reset/complete",
    status_code=204,
    responses={s: {"model": Error} for s in (400, 415, 422, 503)},
)
def complete_password_reset(details: CompleteReset, request: Request, response: Response) -> None:
    """Set a new password from a reset link. The person is signed out everywhere, and so is this
    browser, whoever was signed in on it; they sign in again with the new password."""
    password = passwords.check_new_password(details.password)
    digest = live_token(details.token, "password_reset")
    password_hash = passwords.hash_password(password)
    with SessionLocal.begin() as session:
        user_id = session.scalar(
            text("SELECT reset_password(:hash, :password_hash)"),
            {"hash": digest, "password_hash": password_hash},
        )
        if user_id:
            target = f"user:{user_id}"
            auth.record(
                session, request, "password_reset_completed", actor_user_id=None, target=target
            )
    # Outside the transaction: a link for an account that's gone stays used up.
    if not user_id:
        raise ApiError(400, "invalid_token")
    # Its own transaction: deleting this browser's session inside the reset's deadlocks with a
    # sign-in that holds the password row and deletes the same session.
    auth.sign_out(request, response)
