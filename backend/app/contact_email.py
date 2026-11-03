"""A business's contact email for clients (ZIF-232): an address is used only once its inbox
confirmed it.

The settings key contact_email holds only the confirmed address, the one in effect; the database
lets no runtime role write it (0087). A typed address waits in contact_email_requests (0086),
which only this module and the mailer name, and is used for nothing until its link is confirmed.
"""

import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth, limits
from app.auth import CurrentOwner, CurrentSession, SignedIn
from app.db import SessionLocal, tenant_context
from app.errors import ApiError, Error
from app.jobs import enqueue

router = APIRouter(prefix="/api", tags=["contact-email"])
logger = logging.getLogger(__name__)

LINK_WINDOW = timedelta(minutes=15)
SECRET = re.compile(r"[A-Za-z0-9_-]{43}")  # what secrets.token_urlsafe(32) makes


DAY = timedelta(days=1)
# How many businesses may mail one inbox a link in a day, so a ring of them cannot flood it; with
# LINKS_PER_ADDRESS_A_DAY each, at most 15 links a day reach an inbox from businesses that never had
# it confirmed.
BUSINESSES_PER_ADDRESS_A_DAY = 3
LINKS_PER_ADDRESS_A_DAY = 5
SENDERS_LOCK = 232  # advisory lock class: claims for one inbox take turns


def sent_key(tenant_id: UUID, address: str) -> str:
    return limits.email_key("contact_email_sent", f"{tenant_id}:{address}")


def inbox_key(address: str) -> str:
    return limits.email_key("contact_email_to", address)


# A business counts toward an inbox's day once a link from it was queued, and for a few minutes
# while a claim's request runs, so a request that fails takes back nothing and holds nothing long.
LIVE = "(linked_at > now() - interval '1 day' OR claimed_at > now() - interval '5 minutes')"
# Rows past their day plus an hour (longer than any runtime transaction lives, so no reader still
# counts one), in batches; claims skip rows another holds. claim() purges as it goes, and the
# worker's timer (sweep_senders) takes the rest, so an inbox nobody mails again keeps nothing.
PURGE_SENDERS = text("""
DELETE FROM contact_email_senders WHERE ctid IN (
  SELECT ctid FROM contact_email_senders
  WHERE claimed_at < now() - interval '25 hours'
    AND coalesce(linked_at < now() - interval '25 hours', true)
  LIMIT :batch FOR UPDATE SKIP LOCKED)
RETURNING 1
""")
SWEEP_BATCH = 1000


def sweep_senders() -> None:
    """The worker's sweep of contact_email_senders: every row past its day, batch by batch."""
    while True:
        with SessionLocal.begin() as session:
            gone = len(session.execute(PURGE_SENDERS, {"batch": SWEEP_BATCH}).all())
        if gone < SWEEP_BATCH:
            return


SENDERS = text(f"""
SELECT count(*) FILTER (WHERE tenant_id <> :t) AS others,
       coalesce(bool_or(tenant_id = :t), false) AS mine
FROM contact_email_senders WHERE inbox_key = :k AND {LIVE}
""")
CLAIM = text("""
INSERT INTO contact_email_senders (inbox_key, tenant_id, claimed_at) VALUES (:k, :t, now())
ON CONFLICT (inbox_key, tenant_id) DO UPDATE SET claimed_at = now()
""")
# The proof the inbox's holder gave: the confirmation record, which only confirm_contact_email
# writes and which lasts as long as the business (0086); the API can neither write nor forge it.
EVER_CONFIRMED = text("SELECT EXISTS (SELECT FROM contact_email_confirmations WHERE email = :e)")


# claim() holds a second pooled connection while it may wait on the inbox's lock, so how many one
# requester may have in flight is capped: per address and /48 by the limiter's own slots
# (limits._slot, ZIF-264), which every limiter call of that address shares, and per business here,
# so one business cannot hold claims from many addresses at once. Concurrent saves for one inbox
# cannot drain the API's pool.
_claims: dict[str, int] = {}
_claims_lock = threading.Lock()


@contextmanager
def _business_slot(tenant_id: UUID) -> Iterator[None]:
    """One of the business's limits.MAX_IN_FLIGHT claim slots for the block, or 429 rate_limited."""
    bucket = f"contact_email_claim:{tenant_id}"
    with _claims_lock:
        if _claims.get(bucket, 0) >= limits.MAX_IN_FLIGHT:
            raise ApiError(429, "rate_limited")
        _claims[bucket] = _claims.get(bucket, 0) + 1
    try:
        yield
    finally:
        with _claims_lock:
            _claims[bucket] -= 1
            if not _claims[bucket]:
                del _claims[bucket]


@contextmanager
def _claim_slot(host: str | None, tenant_id: UUID) -> Iterator[None]:
    # No address (a request without a client): the business's slot alone, as limits.hit() then
    # takes none either.
    with limits._slot(host) if host else nullcontext(), _business_slot(tenant_id):
        yield


def claim(current: SignedIn, email: str, host: str | None) -> None:
    """Make this business one of those mailing email's inbox today, or 429
    contact_email_requests_today when BUSINESSES_PER_ADDRESS_A_DAY others already are.

    Exempt, so others cannot keep a business from an inbox that is its own: a business already
    counted today, one that ever had this address confirmed, and the owner's own sign-in inbox.
    Before the request holds any row lock, in a short transaction of its own (the count spans
    businesses); nothing to give back afterwards, as a claim whose request fails lapses within
    minutes. Counted from rows, one per
    inbox and business, so concurrent requests can neither count a business twice nor take it back.

    Accepted: the refusal tells an owner that three other businesses mailed that inbox in the past
    day, a small oracle that costs each probe a link to the inbox itself.
    """
    with _claim_slot(host, current.tenant_id):
        _claim(current, email)


def _claim(current: SignedIn, email: str) -> None:
    address = limits.address_for_limits(email)
    key = inbox_key(address)
    # A business already at its own limits takes no place: send_link would refuse it after the
    # lock, and its claim would keep others from the inbox meanwhile. Reads only, charged later.
    if any(
        limits.count(k, window) >= cap for k, window, cap in own_limits(current.tenant_id, address)
    ):
        raise ApiError(429, "rate_limited")
    # Reads only, no lock taken. An owner's own sign-in inbox was proven at sign-up, so others
    # cannot keep them from it (it still needs its link confirmed).
    own = current.db.scalar(text("SELECT email FROM users WHERE id = :u"), {"u": current.user_id})
    # The exact address, never folded: a +tag can be another mailbox where a domain has no
    # subaddressing (the limit keys still fold it).
    exempt = (own is not None and own.lower() == email.lower()) or bool(
        current.db.scalar(EVER_CONFIRMED, {"e": email})
    )
    with SessionLocal.begin() as session:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:c, hashtext(:k))"), {"c": SENDERS_LOCK, "k": key}
        )
        session.execute(PURGE_SENDERS, {"batch": 100})
        senders = session.execute(SENDERS, {"k": key, "t": current.tenant_id}).one()
        if not (senders.mine or exempt) and senders.others >= BUSINESSES_PER_ADDRESS_A_DAY:
            raise ApiError(429, "contact_email_requests_today")
        session.execute(CLAIM, {"k": key, "t": current.tenant_id})


def own_limits(tenant_id: UUID, address: str) -> list[tuple[str, timedelta, int]]:
    """This business's own limits on links to address: key, window and cap."""
    return [
        (limits.email_key("contact_email", f"{tenant_id}:{address}"), timedelta(hours=1), 3),
        (f"contact_email:tenant:{tenant_id}", DAY, 10),
        (sent_key(tenant_id, address), DAY, LINKS_PER_ADDRESS_A_DAY),
    ]


def send_link(current: SignedIn, request_id: UUID, email: str) -> None:
    """Queue the link for a request, or 429 when this business has sent too many.

    Called with the request row locked and changed, after claim(). Every key and row here is this
    business's alone, so all of it runs in the request's own transaction: no second connection
    while its rows are locked, and a 429 rolls it back with the change and the rest of the request.
    """
    tenant = current.tenant_id
    address = limits.address_for_limits(email)
    # One call per key: hit() takes one window. Every key counts, whichever is over.
    over = [
        limits.hit({key: cap}, window, db=current.db)
        for key, window, cap in own_limits(tenant, address)
    ]
    if any(over):
        raise ApiError(429, "rate_limited")
    linked = current.db.scalar(
        text(
            "UPDATE contact_email_senders SET linked_at = now()"
            " WHERE inbox_key = :k AND tenant_id = :t RETURNING true"
        ),
        {"k": inbox_key(address), "t": tenant},
    )
    # Defensive, unreachable today: claim() left the row and nothing deletes it within a day.
    if not linked:
        raise ApiError(503, "busy")
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
    link that confirms it; the one in effect stays until then. None clears both.

    Called before any other key of the PUT is saved: claim() must run before the request holds a
    row lock."""
    claimed = email is not None and bool(current.db.scalar(WILL_SEND, {"e": email}))
    if claimed:
        assert email is not None
        claim(current, email, request.client.host if request.client else None)
    row = current.db.execute(
        text("SELECT * FROM change_contact_email(:e, true, :u)"),
        {"e": email, "u": current.user_id},
    ).one()
    if (row.outcome == "requested") != claimed:
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
    email = current.db.scalar(read)  # a read: no lock taken
    if email is None:
        raise ApiError(409, "no_contact_email_request")
    # Before the lock, as for the PUT.
    claim(current, email, request.client.host if request.client else None)
    request_id = current.db.scalar(text("SELECT resend_contact_email(:u)"), {"u": current.user_id})
    if request_id is None:  # cleared or confirmed meanwhile
        raise ApiError(409, "no_contact_email_request")
    if current.db.scalar(read) != email:  # changed meanwhile: the claim was for another address
        raise ApiError(503, "busy")
    send_link(current, request_id, email)
    auth.record(
        current.db,
        request,
        "contact_email_requested",
        actor_user_id=current.user_id,
        target="setting:contact_email",
    )
    response.headers["Cache-Control"] = "no-store"
    return state(current)


@router.delete(
    "/settings/contact-email/request",
    name="cancel",
    responses={s: {"model": Error} for s in (401, 403, 409, 415)},
)
def cancel(current: CurrentOwner, request: Request, response: Response) -> ContactEmailOut:
    """Drop the waiting address: its link stops working, and the address clients see stays."""
    if not current.db.scalar(text("SELECT cancel_contact_email()")):
        raise ApiError(409, "no_contact_email_request")
    auth.record(
        current.db,
        request,
        "contact_email_request_cancelled",
        actor_user_id=current.user_id,
        target="setting:contact_email",
    )
    response.headers["Cache-Control"] = "no-store"
    return state(current)
