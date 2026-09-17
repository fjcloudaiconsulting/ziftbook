"""A business's members, listed and changed by its owners."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from psycopg.errors import CheckViolation
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import auth
from app.auth import CurrentOwner, SignedIn
from app.errors import ApiError, Error

Role = Literal["owner", "worker"]

router = APIRouter(prefix="/api", tags=["members"])


class MemberOut(BaseModel):
    member_id: UUID
    user_id: UUID
    email: str
    role: Role


class RoleChange(BaseModel):
    # Its own model, field by field: nothing a client sends can name a user or a business.
    model_config = ConfigDict(strict=True, extra="forbid")
    role: Role


MEMBERS = """
SELECT m.id AS member_id, m.user_id, u.email, m.role
FROM memberships m JOIN users u ON u.id = m.user_id
"""


@router.get("/members", name="list", responses={s: {"model": Error} for s in (401, 403)})
def list_members(current: CurrentOwner, response: Response) -> list[MemberOut]:
    """Every member of this business, in a stable order. Row-level security limits it to the
    business; the email is personal data, so only an owner sees it."""
    rows = current.db.execute(text(MEMBERS + " ORDER BY m.id")).all()
    response.headers["Cache-Control"] = "no-store"
    return [MemberOut.model_validate(row, from_attributes=True) for row in rows]


def target(current: SignedIn, member_id: UUID) -> MemberOut:
    """The member to change, locked together with the caller's own membership.

    401 if the caller's membership went while the request was under way, 403 if they are no longer
    an owner, 404 outside this business, 403 for the caller's own membership.
    """
    # One statement, rows locked in id order: two owners acting on each other take the two locks
    # in the same order (two statements, or an unordered one, deadlock). The second request waits,
    # then sees the first one's result: without the caller's lock, two owners removing each other
    # would both pass (the trigger's no-member-left exemption) and leave the business empty.
    # FOR UPDATE, not NO KEY UPDATE: a sign-in's session insert (its foreign key check takes KEY
    # SHARE) waits instead of slipping in between deleting sessions and changing the role.
    # OF m: users has no UPDATE policy, so locking it would return no row.
    rows = current.db.execute(
        text(
            MEMBERS
            + """
        WHERE m.id = :id OR m.user_id = :me
        ORDER BY m.id FOR UPDATE OF m
        """
        ),
        {"id": member_id, "me": current.user_id},
    ).all()
    me = next((r for r in rows if r.user_id == current.user_id), None)
    if me is None:  # removed while this request was under way
        raise ApiError(401, "unauthenticated")
    if me.role != "owner":  # demoted while this request was under way
        raise ApiError(403, "owner_only")
    found = next((r for r in rows if r.member_id == member_id), None)
    if found is None:
        raise ApiError(404, "not_found")
    if found.user_id == current.user_id:
        raise ApiError(403, "own_membership")
    return MemberOut.model_validate(found, from_attributes=True)


def member_user(current: SignedIn, member_id: UUID, *, lock: bool) -> UUID:
    """The user behind a member of this business; 404 for any other id.

    Shared with the working hours (ZIF-46) and time off (ZIF-47) endpoints, which reuse this lookup
    instead of writing their own.
    """
    # NO KEY UPDATE: a sign-in's session insert (a foreign key check, KEY SHARE) isn't blocked, and
    # target()'s FOR UPDATE on the same row waits for us, or we for it.
    locking = " FOR NO KEY UPDATE" if lock else ""
    user_id: UUID | None = current.db.scalar(
        text("SELECT user_id FROM memberships WHERE id = :id" + locking), {"id": member_id}
    )
    if user_id is None:
        raise ApiError(404, "not_found")
    return user_id


def guarded(current: SignedIn, statement: str, member_id: UUID, **values: object) -> None:
    """Run a change the keep_an_owner trigger may refuse; its refusal is a 409."""
    try:
        current.db.execute(text(statement), {"id": member_id, **values})
    except IntegrityError as error:
        # Only the trigger's refusal: any other integrity error stays a 500.
        if (
            isinstance(error.orig, CheckViolation)
            and error.orig.diag.constraint_name == "last_owner"
        ):
            raise ApiError(409, "last_owner") from None
        raise


@router.patch(
    "/members/{member_id}",
    name="update",
    responses={s: {"model": Error} for s in (401, 403, 404, 409, 415, 422)},
)
def change_role(
    member_id: UUID, change: RoleChange, current: CurrentOwner, request: Request, response: Response
) -> MemberOut:
    """Make a member an owner or a worker. They are signed out of this business, so their next
    request carries the new role; their sessions in other businesses stay."""
    member = target(current, member_id)
    response.headers["Cache-Control"] = "no-store"
    if member.role == change.role:
        return member  # nothing changes: no sign-out, no event
    # First: the sessions foreign key refuses a role their sessions don't carry. sessions has no
    # row-level security, so without tenant_id this would sign them out of every business.
    current.db.execute(
        text("DELETE FROM sessions WHERE tenant_id = :tenant_id AND user_id = :user_id"),
        {"tenant_id": current.tenant_id, "user_id": member.user_id},
    )
    guarded(
        current, "UPDATE memberships SET role = :role WHERE id = :id", member_id, role=change.role
    )
    auth.record(
        current.db,
        request,
        "member_role_changed",
        actor_user_id=current.user_id,
        target=f"user:{member.user_id}",
        details={"old": member.role, "new": change.role},
    )
    return member.model_copy(update={"role": change.role})


@router.delete(
    "/members/{member_id}",
    name="delete",
    status_code=204,
    responses={s: {"model": Error} for s in (401, 403, 404, 409, 415, 422)},
)
def remove(member_id: UUID, current: CurrentOwner, request: Request) -> None:
    """Remove a member from this business. Their sessions here end with the membership."""
    member = target(current, member_id)
    guarded(current, "DELETE FROM memberships WHERE id = :id", member_id)
    auth.record(
        current.db,
        request,
        "member_removed",
        actor_user_id=current.user_id,
        target=f"user:{member.user_id}",
    )
