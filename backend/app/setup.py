"""First-run setup (ZIF-70): the first person on a fresh install becomes the platform admin.

Setup is open only while users is empty (needs_setup(), migration 0038) and only to someone holding
the deployment's ZIF_SETUP_TOKEN. Once anyone exists it answers 409 before the token is looked at,
so a token left configured after setup is inert.
"""

import logging
import secrets
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app import accounts, admin, admin_auth, auth, limits, passwords
from app.business_settings import Locale
from app.config import SetupSettings
from app.db import SessionLocal
from app.errors import ApiError, Error

MIN_TOKEN = 32  # characters; a shorter token counts as unset
SETUP_WINDOW = timedelta(hours=1)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["setup"])


class SetupIn(BaseModel):
    token: str = Field(max_length=200)  # never logged
    email: passwords.Email
    password: str
    name: accounts.DisplayNameText
    locale: Locale


class SetupState(BaseModel):
    needs_setup: bool


def needed() -> bool:
    """Whether this install has no user yet. The one place Python asks (D2)."""
    with SessionLocal.begin() as session:
        return bool(session.scalar(text("SELECT needs_setup()")))


def token_state() -> Literal["set", "unset", "weak"]:
    """Read at call time, as turnstile.enabled(). Never the value itself."""
    token = SetupSettings().setup_token.get_secret_value()
    if not token:
        return "unset"
    return "set" if len(token) >= MIN_TOKEN else "weak"


def needed_at_startup(database_url: str) -> bool:
    """needs_setup() on a one-off connection that gives up after ~3 s, for the startup ERROR line.
    False on any database error: startup never waits on, or fails over, this check."""
    engine = create_engine(
        database_url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": 3, "options": "-c statement_timeout=3s"},
    )
    try:
        with engine.connect() as conn:
            return bool(conn.scalar(text("SELECT needs_setup()")))
    except Exception:
        return False
    finally:
        engine.dispose()


@router.get("/setup")
def state(response: Response) -> SetupState:
    """Whether the first-run setup is still to be done. Says nothing about the token."""
    response.headers["Cache-Control"] = "no-store"
    return SetupState(needs_setup=needed())


@router.post(
    "/setup",
    status_code=201,
    responses={status: {"model": Error} for status in (403, 409, 415, 422, 429, 503)},
)
def complete(body: SetupIn, request: Request, response: Response) -> admin_auth.AdminSession:
    """Create the first account as the platform admin, and sign it in to an admin session."""
    ip = request.client.host if request.client else None
    # Per address only: a global key would let a stranger lock the operator out.
    if limits.hit({limits.ip_key("setup", ip): 10}, SETUP_WINDOW):
        raise ApiError(429, "rate_limited")
    # Before the token: once installed, a guess learns nothing about it.
    if not needed():
        raise ApiError(409, "already_set_up")
    if token_state() != "set":
        raise ApiError(503, "setup_not_configured")
    configured = SetupSettings().setup_token.get_secret_value()
    # Bytes, so a non-ASCII or lone-surrogate guess is a plain mismatch, not a 500.
    if not secrets.compare_digest(
        body.token.encode("utf-8", "surrogatepass"), configured.encode("utf-8", "surrogatepass")
    ):
        logger.warning("setup token rejected")
        raise ApiError(403, "invalid_setup_token")
    # No database connection is held while hashing.
    password_hash = passwords.hash_password(passwords.check_new_password(body.password))
    token = secrets.token_urlsafe(32)
    id_hash = auth.hash_token(token)
    ip_address, user_agent = auth.origin(request)
    with SessionLocal.begin() as session:
        user_id = session.scalar(
            text("SELECT complete_setup(:email, :hash, :name, :locale)"),
            {"email": body.email, "hash": password_hash, "name": body.name, "locale": body.locale},
        )
        if user_id is not None:
            # One transaction: the admin, its session and its audit event exist together or not.
            session.execute(
                text("SELECT start_admin_session(:user_id, :hash, :id_hash, :ip, :user_agent)"),
                {
                    "user_id": user_id,
                    "hash": password_hash,
                    "id_hash": id_hash,
                    "ip": ip_address,
                    "user_agent": user_agent,
                },
            )
            admin.record(
                session,
                request,
                "setup_completed",
                actor_user_id=user_id,
                target=f"user:{user_id}",
            )
            signed_in_as = admin_auth.AdminSession.model_validate(
                session.execute(admin_auth.RESOLVE, {"id_hash": id_hash}).one(),
                from_attributes=True,
            )
    if user_id is None:  # someone else finished setup since needed() said otherwise
        raise ApiError(409, "already_set_up")
    admin_auth.set_admin_cookie(response, token)
    response.headers["Cache-Control"] = "no-store"
    return signed_in_as
