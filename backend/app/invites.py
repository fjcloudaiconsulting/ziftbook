"""Invites: an owner asks someone, by email, to join their business as a worker."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app import auth, limits, passwords
from app.auth import CurrentOwner
from app.db import SessionLocal, tenant_context
from app.errors import ApiError, Error
from app.jobs import enqueue

router = APIRouter(prefix="/api", tags=["invites"])

LINK_WINDOW = timedelta(minutes=15)
SECRET = re.compile(r"[A-Za-z0-9_-]{43}")  # what secrets.token_urlsafe(32) makes


class NewInvite(BaseModel):
    # Nothing else: the role is always worker (promote afterwards).
    model_config = ConfigDict(extra="forbid")
    email: passwords.Email


class InviteOut(BaseModel):
    id: UUID
    email: str
    expires_at: datetime
    expired: bool


class InviteToken(BaseModel):
    token: str = Field(min_length=1, max_length=100)  # the link's fragment: <tenant_id>.<secret>


class InviteDetails(BaseModel):
    business_name: str
    email: str
    has_account: bool


class AcceptInvite(InviteToken):
    password: str


INVITES = "SELECT id, email, expires_at, expires_at <= now() AS expired FROM invites"


@router.post(
    "/invites",
    name="create",
    status_code=201,
    responses={s: {"model": Error} for s in (401, 403, 409, 415, 422, 429)},
)
def create(
    invite: NewInvite, current: CurrentOwner, request: Request, response: Response
) -> InviteOut:
    """Invite someone as a worker, or send a new link to someone already invited: the old link
    stops working and a fresh week starts."""
    tenant = current.tenant_id
    # Two calls: hit() takes one window. Both count, whichever is over.
    per_email = limits.hit(
        {limits.email_key("invite", f"{tenant}:{invite.email}"): 3}, timedelta(hours=1)
    )
    per_business = limits.hit({f"invite:tenant:{tenant}": 50}, timedelta(days=1))
    if per_email or per_business:
        raise ApiError(429, "rate_limited")
    # users' policy shows only this business's members: this finds no account anywhere else.
    if current.db.scalar(
        text("SELECT true FROM memberships m JOIN users u ON u.id = m.user_id WHERE u.email = :e"),
        {"e": invite.email},
    ):
        raise ApiError(409, "already_member")
    # One statement. A resend gets a new id, so its job has a new dedupe key and a queued job for
    # the old id finds nothing; the old hash goes, so the old link dies now, not when the job runs.
    row = current.db.execute(
        text("""
        INSERT INTO invites AS i (tenant_id, email, expires_at)
        VALUES (current_setting('app.tenant_id')::uuid, :e, now() + interval '7 days')
        ON CONFLICT (tenant_id, email) DO UPDATE
          SET id = excluded.id, token_hash = NULL, expires_at = excluded.expires_at
        RETURNING i.id, i.email, i.expires_at, false AS expired
        """),
        {"e": invite.email},
    ).one()
    enqueue(
        current.db,
        "email.invite",
        f"email.invite:{tenant}:{row.id}",
        {"invite_id": str(row.id)},
        tenant_id=tenant,
    )
    auth.record(
        current.db,
        request,
        "member_invited",
        actor_user_id=current.user_id,
        target=f"invite:{row.id}",
    )
    response.headers["Cache-Control"] = "no-store"
    return InviteOut.model_validate(row, from_attributes=True)


@router.get("/invites", name="list", responses={s: {"model": Error} for s in (401, 403)})
def list_invites(current: CurrentOwner, response: Response) -> list[InviteOut]:
    """Every pending or expired invite of this business, oldest first. No token hash, no job
    state: a resend moves an invite to the end (it gets a new id)."""
    rows = current.db.execute(text(INVITES + " ORDER BY id")).all()
    response.headers["Cache-Control"] = "no-store"
    return [InviteOut.model_validate(row, from_attributes=True) for row in rows]


@router.delete(
    "/invites/{invite_id}",
    name="delete",
    status_code=204,
    responses={s: {"model": Error} for s in (401, 403, 404, 415, 422)},
)
def revoke(invite_id: UUID, current: CurrentOwner, request: Request) -> None:
    """Withdraw an invite: its link stops working, and an email not yet sent never goes."""
    deleted = current.db.scalar(
        text("DELETE FROM invites WHERE id = :id RETURNING true"), {"id": invite_id}
    )
    if deleted is None:
        raise ApiError(404, "not_found")  # another business's id is invisible, so the same answer
    auth.record(
        current.db,
        request,
        "invite_revoked",
        actor_user_id=current.user_id,
        target=f"invite:{invite_id}",
    )


@dataclass(frozen=True)
class Found:
    tenant_id: UUID
    digest: bytes
    email: str
    business_name: str
    user_id: UUID | None  # the account the email already has
    password_hash: str | None


def find(token: str) -> Found:
    """The live invite a link names, or 400 invalid_token, the same for every way it can fail."""
    tenant, _, secret = token.partition(".")
    try:
        tenant_id = UUID(tenant)
    except ValueError:
        raise ApiError(400, "invalid_token") from None
    # The exact shape we mint: also keeps a lone surrogate away from hash_token's encode.
    if not SECRET.fullmatch(secret):
        raise ApiError(400, "invalid_token")
    digest = auth.hash_token(secret)
    # The link's business, not the invite's: a token only works under the id it was sent with.
    with tenant_context(tenant_id) as session:
        invite = session.execute(
            text("""
            SELECT i.email, t.name AS business_name FROM invites i
            JOIN tenants t ON t.id = i.tenant_id
            WHERE i.token_hash = :h AND i.expires_at > now()
            """),
            {"h": digest},
        ).first()
        if invite is None:
            raise ApiError(400, "invalid_token")
        # users' policy hides non-members; the sign-in lookup restores this transaction's tenant.
        # Its own statement: it changes app.tenant_id while it runs.
        account = session.execute(
            text("SELECT user_id, password_hash FROM account_by_email(:e)"), {"e": invite.email}
        ).first()
    return Found(
        tenant_id,
        digest,
        invite.email,
        invite.business_name,
        account.user_id if account else None,
        account.password_hash if account else None,
    )


@router.post(
    "/invites/lookup",
    name="lookup",
    responses={s: {"model": Error} for s in (400, 415, 422, 429)},
)
def lookup(body: InviteToken, request: Request, response: Response) -> InviteDetails:
    """What an invite link is for, so the page can ask for a new password or the existing one."""
    ip = request.client.host if request.client else None
    if limits.hit({limits.ip_key("invite_lookup", ip): 10}, LINK_WINDOW):
        raise ApiError(429, "rate_limited")
    found = find(body.token)
    response.headers["Cache-Control"] = "no-store"
    return InviteDetails(
        business_name=found.business_name, email=found.email, has_account=found.user_id is not None
    )


@router.post(
    "/invites/accept",
    name="accept",
    status_code=201,
    responses={s: {"model": Error} for s in (400, 401, 409, 415, 422, 429, 503)},
)
def accept(body: AcceptInvite, request: Request, response: Response) -> auth.SessionOut:
    """Join the business from an invite link, creating the account or proving the existing one's
    password, and sign in to that business.

    An existing account is signed into this business now, but its next plain sign-in still lands
    in its oldest business (known gap, ZIF-81). A newly created account has no language of its
    own, so its emails use the business's language until it signs in and sets one.
    """
    ip = request.client.host if request.client else None
    if limits.hit({limits.ip_key("invite_accept", ip): 10}, LINK_WINDOW):
        raise ApiError(429, "rate_limited")
    found = find(body.token)  # before any hashing: a dead link costs nothing
    password_hash = None
    if found.user_id is None:
        # A weak password is a 422 and the link stays usable.
        password_hash = passwords.hash_password(passwords.check_new_password(body.password))
    else:
        # The sign-in count: an invite link is no way around the per-account limit.
        if limits.hit({limits.email_key("sign_in", found.email): 10}, auth.SIGN_IN_WINDOW):
            raise ApiError(429, "rate_limited")
        # Sign-in's rules: length only, and no database connection held while verifying.
        if len(body.password) > passwords.MAX_PASSWORD or not passwords.verify(
            found.password_hash, body.password
        ):
            with SessionLocal.begin() as session:
                auth.record(
                    session,
                    request,
                    "sign_in_failed",
                    actor_user_id=None,
                    target=f"user:{found.user_id}",
                )
            raise ApiError(401, "invalid_credentials")
    with tenant_context(found.tenant_id) as session:
        if found.user_id is not None and not session.scalar(
            text("SELECT password_unchanged(:u, :h)"),
            {"u": found.user_id, "h": found.password_hash},
        ):  # reset while it was being checked
            raise ApiError(401, "invalid_credentials")
        result = session.execute(
            text("SELECT * FROM accept_invite(:h, :p)"), {"h": found.digest, "p": password_hash}
        ).one()
        if result.outcome == "accepted":
            auth.record(
                session,
                request,
                "invite_accepted",
                actor_user_id=result.account_id,
                target=f"user:{result.account_id}",
            )
            token = auth.start(session, request, result.account_id)
            signed_in_as = auth.describe(session, result.account_id)
    # Outside the transaction: already_member keeps the invite used up.
    if result.outcome == "invalid_token":  # used, revoked or resent since find()
        raise ApiError(400, "invalid_token")
    if result.outcome in ("account_exists", "already_member"):
        raise ApiError(409, result.outcome)
    assert result.outcome == "accepted"
    auth.set_cookie(response, token)
    response.headers["Cache-Control"] = "no-store"
    return signed_in_as
