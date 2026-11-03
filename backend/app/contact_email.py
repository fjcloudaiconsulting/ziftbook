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
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth, limits
from app.auth import CurrentOwner, CurrentSession, SignedIn
from app.db import tenant_context
from app.errors import ApiError, Error
from app.jobs import enqueue

router = APIRouter(prefix="/api", tags=["contact-email"])
logger = logging.getLogger(__name__)

LINK_WINDOW = timedelta(minutes=15)
SECRET = re.compile(r"[A-Za-z0-9_-]{43}")  # what secrets.token_urlsafe(32) makes


DAY = timedelta(days=1)
# Links one business may send one inbox in a day, and how many businesses may mail one inbox in a
# day: at most 15 a day reach an inbox, however many businesses join in.
LINKS_PER_ADDRESS_A_DAY = 5
BUSINESSES_PER_ADDRESS_A_DAY = 3


def sent_key(tenant_id: UUID, address: str) -> str:
    return limits.email_key("contact_email_sent", f"{tenant_id}:{address}")


def businesses_key(address: str) -> str:
    return limits.email_key("contact_email_businesses", address)


def charge_shared(tenant_id: UUID, email: str) -> list[str]:
    """Count a link to email against what businesses share, or 429; the keys counted, to give back
    with limits.refund when no link is queued after all.

    Before the request row is locked, in limits' own transactions: other businesses contend for
    these rows, and a request holding its locks must not wait for a second connection. Counted per
    business, so other businesses never use up this one's links to an inbox; a business's first
    link of the day to it also counts toward the businesses that mail it. A refusal counts nothing.
    """
    address = limits.address_for_limits(email)
    sent = sent_key(tenant_id, address)
    counted = [sent]
    try:
        links = limits.charge(sent, DAY)  # exact, so exactly one call sees the first link
        if links > LINKS_PER_ADDRESS_A_DAY:
            raise ApiError(429, "rate_limited")
        if links == 1:
            over = limits.hit({businesses_key(address): BUSINESSES_PER_ADDRESS_A_DAY}, DAY)
            counted.append(businesses_key(address))
            if over:
                raise ApiError(429, "rate_limited")
    except Exception:
        limits.refund(counted, DAY)
        raise
    return counted


def send_link(current: SignedIn, request_id: UUID, email: str) -> None:
    """Queue the link for a request, or 429 when this business has sent too many.

    Called with the request row locked and changed. These keys are this business's alone, so they
    are counted in the request's own transaction: no second connection while the row is locked,
    and a 429 rolls the count back with the change and the rest of the request.
    """
    tenant = current.tenant_id
    address = limits.address_for_limits(email)
    # Two calls: hit() takes one window. Both count, whichever is over.
    hourly = limits.hit(
        {limits.email_key("contact_email", f"{tenant}:{address}"): 3},
        timedelta(hours=1),
        db=current.db,
    )
    daily = limits.hit({f"contact_email:tenant:{tenant}": 10}, DAY, db=current.db)
    if hourly or daily:
        raise ApiError(429, "rate_limited")
    enqueue(
        current.db,
        "email.contact_email",
        f"email.contact_email:{tenant}:{request_id}",
        {"request_id": str(request_id)},
        tenant_id=tenant,
    )


# What change_contact_email(email, true) will do, read without a lock: only to know whether to
# count a link before the lock is taken.
WILL_SEND = text("""
SELECT :e IS DISTINCT FROM (SELECT value #>> '{}' FROM settings WHERE key = 'contact_email')
   AND NOT coalesce((SELECT email = :e AND expires_at > now() FROM contact_email_requests), false)
""")


def tell_owners(session: Session, tenant_id: UUID) -> None:
    """Mail every owner that the address clients see changed, in the change's transaction: an
    inbox someone else confirmed would otherwise keep client replies from them unnoticed."""
    for user_id in session.scalars(text("SELECT user_id FROM memberships WHERE role = 'owner'")):
        enqueue(
            session,
            "email.send",
            f"email.send:{tenant_id}:contact_email_changed:{user_id}:{uuid4()}",
            {"recipient_id": str(user_id), "template": "contact_email_changed"},
            tenant_id=tenant_id,
        )


def change(current: SignedIn, request: Request, email: str | None) -> None:
    """An owner's PUT of contact_email: a new address waits for its confirmation, and is sent the
    link that confirms it; the one in effect stays until then. None clears both."""
    counted = []
    if email is not None and current.db.scalar(WILL_SEND, {"e": email}):
        counted = charge_shared(current.tenant_id, email)
    try:
        row = current.db.execute(
            text("SELECT * FROM change_contact_email(:e, true, :u)"),
            {"e": email, "u": current.user_id},
        ).one()
        if (row.outcome == "requested") != bool(counted):
            # Another save changed the address between the read and the lock: try again.
            raise ApiError(503, "busy")
        if row.outcome == "requested":
            assert email is not None  # only a clear has none
            send_link(current, row.request_id, email)
            # No details: nobody has confirmed the address, and it may be a third party's.
            auth.record(
                current.db,
                request,
                "contact_email_requested",
                actor_user_id=current.user_id,
                target="setting:contact_email",
            )
            counted = []  # queued: the link counts
    finally:
        if counted:
            limits.refund(counted, DAY)
    if row.outcome == "cleared" and row.previous is not None:
        auth.record(
            current.db,
            request,
            "setting_changed",
            actor_user_id=current.user_id,
            target="setting:contact_email",
            details={"old": row.previous, "new": None},
        )
        tell_owners(current.db, current.tenant_id)


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
        tell_owners(session, tenant_id)
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


@router.post(
    "/settings/contact-email/confirmation",
    name="resend",
    responses={s: {"model": Error} for s in (401, 403, 409, 415, 429, 503)},
)
def resend(current: CurrentOwner, request: Request, response: Response) -> ContactEmailOut:
    """Mail the waiting address a new link: the old one stops working at once."""
    read = text("SELECT email FROM contact_email_requests")
    email = current.db.scalar(read)
    if email is None:
        raise ApiError(409, "no_contact_email_request")
    counted = charge_shared(current.tenant_id, email)  # before the lock, as for the PUT
    try:
        request_id = current.db.scalar(text("SELECT resend_contact_email()"))
        if request_id is None:  # cleared or confirmed meanwhile
            raise ApiError(409, "no_contact_email_request")
        if current.db.scalar(read) != email:  # changed meanwhile: its link was counted for another
            raise ApiError(503, "busy")
        send_link(current, request_id, email)
        auth.record(
            current.db,
            request,
            "contact_email_requested",
            actor_user_id=current.user_id,
            target="setting:contact_email",
        )
        counted = []
    finally:
        if counted:
            limits.refund(counted, DAY)
    response.headers["Cache-Control"] = "no-store"
    return state(current)
