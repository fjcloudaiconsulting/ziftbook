"""A business's members: listed, promoted, demoted and removed by its owners, and each member's
display name set by an owner or by that member."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from psycopg.errors import CheckViolation
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import auth
from app.accounts import DisplayNameText
from app.auth import CurrentOwner, CurrentSession, SignedIn
from app.errors import ApiError, Error

Role = Literal["owner", "worker"]

# migration 0026; bookings.py's B5 branches match it
BOOKINGS_WORKER = "fk_bookings_tenant_id_worker_id_memberships"

router = APIRouter(prefix="/api", tags=["members"])


class MemberOut(BaseModel):
    member_id: UUID
    user_id: UUID
    email: str
    role: Role
    display_name: str | None


class RoleChange(BaseModel):
    # Its own model, field by field: nothing a client sends can name a user or a business.
    model_config = ConfigDict(strict=True, extra="forbid")
    role: Role


class DisplayNameChange(BaseModel):
    # Its own model, field by field: nothing a client sends can name a user or a business.
    model_config = ConfigDict(strict=True, extra="forbid")
    display_name: DisplayNameText | None  # required; null clears the name


class DisplayNameOut(BaseModel):
    # Deliberately not MemberOut: MemberOut carries the email, and a worker calls this route.
    display_name: str | None


MEMBERS = """
SELECT m.id AS member_id, m.user_id, u.email, m.role, m.display_name
FROM memberships m JOIN users u ON u.id = m.user_id
WHERE m.role <> 'removed'
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
    # FOR UPDATE, not NO KEY UPDATE: a sign-in's FOR SHARE read (auth.create) waits instead of
    # slipping in between deleting sessions and changing the role. The filter is inside the locking
    # statement: after a wait, READ COMMITTED re-checks it on the new row version.
    # OF m: the app role has no UPDATE on users (ZB-10), so locking a users row would fail.
    rows = current.db.execute(
        text(
            MEMBERS
            + """
        AND (m.id = :id OR m.user_id = :me)
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

    Shared with the working hours (ZIF-46), time off (ZIF-47) and display name (ZIF-97)
    endpoints, which reuse this lookup instead of writing their own.
    """
    # NO KEY UPDATE: target()'s FOR UPDATE on the same row waits for us, or we for it. A sign-in
    # (FOR SHARE) for this person waits for us too; it is one short save.
    locking = " FOR NO KEY UPDATE" if lock else ""
    user_id: UUID | None = current.db.scalar(
        text("SELECT user_id FROM memberships WHERE id = :id AND role <> 'removed'" + locking),
        {"id": member_id},
    )
    if user_id is None:
        raise ApiError(404, "not_found")
    return user_id


def is_owner(current: SignedIn) -> bool:
    """The owner half of may_manage on its own, for a list route with no single user_id to compare
    against (the pending queue, ZIF-52). One source of truth, so a list and the per-row decision it
    offers can never disagree about who sees what."""
    return current.role == "owner"


def may_manage(current: SignedIn, user_id: UUID) -> bool:
    """An owner manages everyone's data; anyone else only their own. Shared with time off
    (ZIF-47) and the display name (ZIF-97)."""
    return is_owner(current) or user_id == current.user_id


def guarded(current: SignedIn, statement: str, member_id: UUID, **values: object) -> None:
    """Run a change the keep_an_owner trigger may refuse; its refusal is a 409."""
    try:
        current.db.execute(text(statement), {"id": member_id, **values})
    except IntegrityError as error:
        # Only that refusal; any other integrity error stays a 500.
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


# A live appointment: confirmed, or a hold (pending, or a card check in flight) that hasn't lapsed,
# and not over yet. time_off.IN_THE_WAY's carve-out plus awaiting_payment, with no window.
UPCOMING = text("""
SELECT EXISTS (
  SELECT FROM bookings
  WHERE worker_id = :id AND ends_at > now()
    AND (status = 'confirmed'
         OR (status IN ('pending', 'awaiting_payment') AND expires_at > now())))
""")


@router.delete(
    "/members/{member_id}",
    name="delete",
    status_code=204,
    responses={s: {"model": Error} for s in (401, 403, 404, 409, 415, 422)},
)
def remove(member_id: UUID, current: CurrentOwner, request: Request) -> None:
    """End a member's membership in this business. The row stays, so their bookings and history
    still resolve; their sessions, service assignments, working hours and time off here go.
    Refused while they hold an upcoming appointment."""
    from app import time_off  # here, not at the top: app.time_off imports this module

    # First statement (CONTRIBUTING.md "Bookings"): with the row kept, the bookings foreign key no
    # longer stops a booking racing the removal; the lock does. Order: lock 51, memberships, (the
    # trigger) tenants, as time off writes take them.
    time_off.lock_bookings(current)
    member = target(current, member_id)
    if current.db.scalar(UPCOMING, {"id": member_id}):
        raise ApiError(409, "has_upcoming_bookings")
    # What deleting the row cascaded (0006, 0017, 0019, 0021, 0034). Sessions first: the role change
    # below fails on the sessions foreign key while one survives. sessions has no row-level
    # security, so tenant_id is mandatory.
    current.db.execute(
        text("DELETE FROM sessions WHERE tenant_id = :tenant_id AND user_id = :user_id"),
        {"tenant_id": current.tenant_id, "user_id": member.user_id},
    )
    for table in ("service_workers", "working_hours", "time_off"):
        current.db.execute(text(f"DELETE FROM {table} WHERE member_id = :id"), {"id": member_id})
    # 0034's comment: "removing a member drops their holds" (worker_id, not member_id).
    current.db.execute(text("DELETE FROM booking_holds WHERE worker_id = :id"), {"id": member_id})
    guarded(
        current,
        "UPDATE memberships SET role = 'removed', removed_at = now() WHERE id = :id",
        member_id,
    )
    auth.record(
        current.db,
        request,
        "member_removed",
        actor_user_id=current.user_id,
        target=f"user:{member.user_id}",
    )


@router.put(
    "/members/{member_id}/display-name",
    name="set_display_name",
    responses={s: {"model": Error} for s in (401, 403, 404, 415, 422)},
)
def set_display_name(
    member_id: UUID,
    change: DisplayNameChange,
    current: CurrentSession,
    request: Request,
    response: Response,
) -> DisplayNameOut:
    """Set or clear the name clients see when booking this member. An owner changes
    anyone's, a member their own. Never MemberOut: that carries the email, and a worker
    calls this route."""
    # The member first (404 outside this business), then the authorization check: CONTRIBUTING.md
    # :84-86. NO KEY UPDATE, and never a lock on tenants.
    user_id = member_user(current, member_id, lock=True)
    if not may_manage(current, user_id):
        raise ApiError(403, "owner_only")
    response.headers["Cache-Control"] = "no-store"
    # One statement, and never naming role in the SET list: keep_an_owner is AFTER UPDATE OF role
    # and would take a lock CONTRIBUTING.md forbids. Not wrapped in guarded(): the trigger cannot
    # fire. No row back means the name was already exactly this: no event. RETURNING, not rowcount:
    # Session.execute gives a Result, which has no rowcount (mypy).
    changed = current.db.execute(
        text(
            "UPDATE memberships SET display_name = :name "
            "WHERE id = :id AND display_name IS DISTINCT FROM :name "
            "RETURNING id"
        ),
        {"id": member_id, "name": change.display_name},
    ).first()
    if changed is not None:
        auth.record(
            current.db,
            request,
            "member_display_name_changed",
            actor_user_id=current.user_id,
            target=f"user:{user_id}",
            # The name itself never reaches the event, the logs or any 4xx body.
            details={"display_name": "set" if change.display_name else "cleared"},
        )
    return DisplayNameOut(display_name=change.display_name)
