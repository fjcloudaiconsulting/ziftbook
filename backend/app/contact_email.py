"""A business's contact email for clients (ZIF-232): an address is used only once its inbox
confirmed it.

The settings key contact_email holds only the confirmed address, the one in effect; the database
lets no runtime role write it (0087). A typed address waits in contact_email_requests (0086),
which only this module and the mailer name, and is used for nothing until its link is confirmed.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app import auth, limits
from app.auth import CurrentSession, SignedIn
from app.db import tenant_context
from app.errors import ApiError, Error

router = APIRouter(prefix="/api", tags=["contact-email"])
logger = logging.getLogger(__name__)

LINK_WINDOW = timedelta(minutes=15)
SECRET = re.compile(r"[A-Za-z0-9_-]{43}")  # what secrets.token_urlsafe(32) makes


def change(current: SignedIn, request: Request, email: str | None) -> None:
    """An owner's PUT of contact_email: a new address waits for its confirmation, the one in effect
    stays until then; None clears both."""
    row = current.db.execute(
        text("SELECT * FROM change_contact_email(:e, false, :u)"),
        {"e": email, "u": current.user_id},
    ).one()
    if row.outcome == "requested":
        # No details: nobody has confirmed the address, and it may be a third party's.
        auth.record(
            current.db,
            request,
            "contact_email_requested",
            actor_user_id=current.user_id,
            target="setting:contact_email",
        )
    elif row.outcome == "cleared" and row.previous is not None:
        auth.record(
            current.db,
            request,
            "setting_changed",
            actor_user_id=current.user_id,
            target="setting:contact_email",
            details={"old": row.previous, "new": None},
        )


class ContactEmailToken(BaseModel):
    token: str = Field(min_length=1, max_length=100)  # the link's fragment: <tenant_id>.<secret>


@router.post(
    "/contact-email/confirm",
    name="confirm",
    status_code=204,
    responses={s: {"model": Error} for s in (400, 415, 422, 429)},
)
def confirm(body: ContactEmailToken, request: Request, response: Response) -> None:
    """Confirm a business's contact email from the link mailed to it: the address is now the one
    clients see. The same 400 for every dead link, and nothing about the business or the address
    either way."""
    ip = request.client.host if request.client else None
    # Before any lookup: a guess costs no database transaction.
    if limits.hit(
        {limits.ip_key("contact_email_confirm", ip, v6_prefix=limits.AUTH_V6_PREFIX): 10},
        LINK_WINDOW,
    ):
        raise ApiError(429, "rate_limited")
    tenant, _, secret = body.token.partition(".")
    try:
        tenant_id = UUID(tenant)
    except ValueError:
        raise ApiError(400, "invalid_token") from None
    # The exact shape we mint: also keeps a lone surrogate away from hash_token's encode.
    if not SECRET.fullmatch(secret):
        raise ApiError(400, "invalid_token")
    # The link's business, not the request's: a token only works under the id it was sent with.
    with tenant_context(tenant_id) as session:
        row = session.execute(
            text("SELECT confirmed, previous FROM confirm_contact_email(:h)"),
            {"h": auth.hash_token(secret)},
        ).first()
        if row is None:
            raise ApiError(400, "invalid_token")
        # No actor: the inbox's holder need not be a member. The row keeps their IP and browser as
        # the consent's evidence; owners see neither (an event's IP shows only to its own actor).
        auth.record(
            session,
            request,
            "setting_changed",
            actor_user_id=None,
            target="setting:contact_email",
            details={"old": row.previous, "new": row.confirmed},
        )
    # After the commit: a confirm that failed to commit is not logged as done.
    logger.info("contact email confirmed", extra={"tenant_id": str(tenant_id)})
    response.headers["Cache-Control"] = "no-store"


class ContactEmailOut(BaseModel):
    """Which address clients see, and which waits for its inbox to confirm it."""

    # none: no address. confirmed: clients see `confirmed`, nothing waits. unconfirmed: an address
    # waits with no live link (never sent, or link_expired). sent: a link is queued or out.
    state: Literal["none", "unconfirmed", "sent", "confirmed"]
    confirmed: str | None  # what clients see now, whatever the state
    requested: str | None  # the waiting address; shown to owners only
    link_expired: bool
    expires_at: datetime | None


STATE = text("""
SELECT (SELECT value #>> '{}' FROM settings WHERE key = 'contact_email') AS confirmed,
       r.email, r.expires_at, coalesce(r.expires_at > now(), false) AS live
FROM (SELECT 1) one LEFT JOIN contact_email_requests r ON true
""")


def state(current: SignedIn) -> ContactEmailOut:
    row = current.db.execute(STATE).one()
    if row.email is None:
        kind: Literal["none", "unconfirmed", "sent", "confirmed"] = (
            "none" if row.confirmed is None else "confirmed"
        )
    else:
        kind = "sent" if row.live else "unconfirmed"
    return ContactEmailOut(
        state=kind,
        confirmed=row.confirmed,
        # Nobody has confirmed it, and it may be a third party's: owners only.
        requested=row.email if current.role == "owner" else None,
        link_expired=row.expires_at is not None and not row.live,
        expires_at=row.expires_at,
    )


@router.get("/settings/contact-email", name="read", responses={401: {"model": Error}})
def read_state(current: CurrentSession, response: Response) -> ContactEmailOut:
    response.headers["Cache-Control"] = "no-store"
    return state(current)
