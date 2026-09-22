"""A business's members: listed, promoted, demoted and removed by its owners, and each member's
display name set by an owner or by that member."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from psycopg.errors import CheckViolation, ForeignKeyViolation
from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import auth
from app.accounts import printable
from app.auth import CurrentOwner, CurrentSession, SignedIn
from app.errors import ApiError, Error

Role = Literal["owner", "worker"]

BOOKINGS_WORKER = "fk_bookings_tenant_id_worker_id_memberships"  # migration 0026

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


# ponytail: printable() blocks C* and Zl/Zp, so ZWJ, RLO and BOM are 422; strip_whitespace is what
# empties an NBSP-only name (and min_length then refuses it). It does not block 60 combining
# marks (Zalgo), blank-rendering glyphs (U+2800 Braille blank, U+3164 Hangul filler) or
# homoglyphs. Accepted: it takes an authenticated member of that business, it damages only that
# business's own page, and any owner can overwrite it. Add a normalisation/blocklist only if a
# real business is hit.
DisplayNameText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=60),
    AfterValidator(printable),
]


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

    Shared with the working hours (ZIF-46), time off (ZIF-47) and display name (ZIF-97)
    endpoints, which reuse this lookup instead of writing their own.
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


def may_manage(current: SignedIn, user_id: UUID) -> bool:
    """An owner manages everyone's data; anyone else only their own. Shared with time off
    (ZIF-47) and the display name (ZIF-97)."""
    return current.role == "owner" or user_id == current.user_id


def guarded(current: SignedIn, statement: str, member_id: UUID, **values: object) -> None:
    """Run a change the keep_an_owner trigger or a booking may refuse; either refusal is a 409."""
    try:
        current.db.execute(text(statement), {"id": member_id, **values})
    except IntegrityError as error:
        # Only these two refusals; any other integrity error stays a 500.
        if (
            isinstance(error.orig, CheckViolation)
            and error.orig.diag.constraint_name == "last_owner"
        ):
            raise ApiError(409, "last_owner") from None
        # bookings (ZIF-51) reference memberships with no ON DELETE, on purpose: a booking is a
        # business record and must never be cascaded away with the person who took it.
        if (
            isinstance(error.orig, ForeignKeyViolation)
            and error.orig.diag.constraint_name == BOOKINGS_WORKER
        ):
            raise ApiError(409, "has_bookings") from None
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
