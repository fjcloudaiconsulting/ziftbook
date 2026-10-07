"""Creating an account: sign up with an email, then complete it from the emailed link."""

import unicodedata
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Request, Response
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints
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


# Glyphs that draw nothing: the Hangul fillers (category Lo, so a letter test alone passes them)
# and the Braille blank (So).
BLANK_GLYPHS = frozenset("\u115f\u1160\u3164\uffa0\u2800")


def printable(name: str) -> str:
    # No control, format (zero-width) or unassigned characters: a name that looks empty, or that
    # the database refuses (NUL), is a 422, not a blank business or a 500. Zl/Zp (U+2028, U+2029)
    # are refused too: without them a business name could put a line break into an emailed invite.
    if any(
        unicodedata.category(c).startswith("C") or unicodedata.category(c) in ("Zl", "Zp")
        for c in name
    ):
        raise ValueError("unprintable characters")
    # Looks empty: only blank glyphs, whitespace and combining marks. One such glyph between real
    # text is fine, so a stored description with spacer lines still saves.
    if all(
        c in BLANK_GLYPHS or c.isspace() or unicodedata.category(c).startswith("M") for c in name
    ):
        raise ValueError("blank-looking text")
    return name


def named(name: str) -> str:
    # printable, plus: no blank glyph anywhere, and at least one letter or number, so a name is
    # never emoji-only, marks-only or empty-looking. For names only: free text keeps "🏖".
    printable(name)
    if BLANK_GLYPHS & set(name) or not any(unicodedata.category(c)[0] in "LN" for c in name):
        raise ValueError("a name needs a letter or number and no blank glyphs")
    return name


BusinessName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    AfterValidator(named),
]


# A person's name: a member's display name (app/members.py) and the platform name given at sign-up,
# invite and first sign-in. Defined here because members imports this module, not the reverse.
# ponytail: printable() blocks C* and Zl/Zp, so ZWJ, RLO and BOM are 422; strip_whitespace is what
# empties an NBSP-only name (and min_length then refuses it); named() refuses the blank glyphs
# (U+2800 Braille blank, U+3164 Hangul filler and kin) and a name with no letter or number. It does
# not block 60 combining marks (Zalgo) after a letter or homoglyphs. Accepted: a display name takes
# an authenticated member, damages only that business's own page, and any owner can overwrite it; a
# platform name (users.name) is set once by its own person, so no owner can overwrite it, but it
# only shows to their own businesses' teams and clients. Add a normalisation/blocklist only if a
# real person is hit.
DisplayNameText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=60),
    AfterValidator(named),
]


class CompleteSignUp(BaseModel):
    token: str = Field(min_length=1, max_length=100)  # from the link's fragment
    password: str
    name: DisplayNameText  # the person's own; never logged, never in an event
    business_name: BusinessName
    country: Country


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
    country = details.country
    defaults = COUNTRIES[country]
    with SessionLocal.begin() as session:
        created = session.execute(
            text("""SELECT * FROM complete_sign_up(
                    :hash, :password_hash, :business_name, :country, :currency, :name)"""),
            {
                "hash": digest,
                "password_hash": password_hash,
                "business_name": details.business_name,
                "country": country,
                "currency": defaults.currency,
                "name": details.name,
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


class NameIn(BaseModel):
    # Its own model, field by field: nothing else a client sends is accepted.
    model_config = ConfigDict(strict=True, extra="forbid")
    name: DisplayNameText


@router.put(
    "/session/name",
    responses={s: {"model": Error} for s in (401, 409, 415, 422)},
)
def give_name(
    body: NameIn, current: auth.AnySession, request: Request, response: Response
) -> auth.SessionOut:
    """A person from before registration asked for a name gives theirs, once. The name fills in
    their unset display names in every business (set_own_name); it is never an event detail."""
    response.headers["Cache-Control"] = "no-store"
    if not current.db.scalar(
        text("SELECT set_own_name(:u, :name)"), {"u": current.user_id, "name": body.name}
    ):
        raise ApiError(409, "name_already_set")
    auth.record(
        current.db,
        request,
        "user_name_set",
        actor_user_id=current.user_id,
        target=f"user:{current.user_id}",
    )
    return auth.describe(current.db, current.user_id)


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
