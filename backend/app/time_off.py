"""When a member can't be booked. A worker manages their own blocks, an owner everyone's; the
reason is shown only to the block's member and to owners."""

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import text

from app import auth, members
from app.accounts import printable
from app.auth import CurrentSession, SignedIn
from app.errors import ApiError, Error

LONGEST = timedelta(days=366)  # migration 0018, ck_time_off_at_most_366_days


def iso_text(value: object) -> object:
    # Lax datetime parsing also takes epoch seconds (1789000000, or "1789000000" in a query).
    if not isinstance(value, str) or not re.match(r"[0-9]{4}-", value):
        raise ValueError("not an ISO 8601 date and time")
    return value


def utc(value: datetime) -> datetime:
    # The year check comes first: astimezone raises OverflowError (a 500) at year 1, and Postgres
    # would store a BC date that psycopg can't read back.
    if not 2000 <= value.year <= 2999:
        raise ValueError("out of range")
    return value.astimezone(UTC)


# With an offset, always: naive is a 422. strict=False because strict mode refuses every string.
Instant = Annotated[
    AwareDatetime, Field(strict=False), BeforeValidator(iso_text), AfterValidator(utc)
]
Reason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    AfterValidator(printable),
]
STRICT = ConfigDict(strict=True, extra="forbid")


class TimeOffIn(BaseModel):
    model_config = STRICT
    starts_at: Instant
    ends_at: Instant  # exclusive
    reason: Reason | None = None


class TimeOffChange(BaseModel):
    """Only the fields sent change. reason may be null (clears it); the times may not."""

    model_config = STRICT
    starts_at: Instant | SkipJsonSchema[None] = None
    ends_at: Instant | SkipJsonSchema[None] = None
    reason: Reason | None = None

    @field_validator("starts_at", "ends_at", mode="before")
    @classmethod
    def not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("null")
        return value


class TimeOffOut(BaseModel):
    id: UUID
    member_id: UUID
    starts_at: datetime
    ends_at: datetime
    reason: str | None  # null when there is none, or when the caller may not see it
    source: Literal["manual", "google"]


def fields(sees_reason: bool) -> str:
    return "id, member_id, starts_at, ends_at, source, " + (
        "reason" if sees_reason else "NULL::text AS reason"
    )


def may_manage(current: SignedIn, user_id: UUID) -> bool:
    return current.role == "owner" or user_id == current.user_id


def checked(starts_at: datetime, ends_at: datetime) -> None:
    if ends_at <= starts_at:
        raise ApiError(422, "end_not_after_start")
    if ends_at - starts_at > LONGEST:
        raise ApiError(422, "time_off_too_long")


def manual_block(current: SignedIn, time_off_id: UUID) -> tuple[UUID, UUID]:
    """A manual block of this business that the caller may change, with its member locked.

    404 for another business's block, a Google block or an unknown id; 403 for another member's
    block when the caller isn't an owner. Returns (member_id, the member's user_id).
    """
    member_id: UUID | None = current.db.scalar(
        text("SELECT member_id FROM time_off WHERE id = :id AND source = 'manual'"),
        {"id": time_off_id},
    )
    if member_id is None:
        raise ApiError(404, "not_found")
    # The member first, then their blocks: the same order as removing a member (membership row,
    # then the cascade to time_off), so the two never deadlock. Never lock tenants: keep_an_owner
    # locks memberships and then tenants (migration 0014).
    user_id = members.member_user(current, member_id, lock=True)
    if not may_manage(current, user_id):
        raise ApiError(403, "owner_only")
    return member_id, user_id


router = APIRouter(prefix="/api", tags=["time-off"])
MEMBER_PATH = "/members/{member_id}/time-off"
BLOCK_PATH = "/time-off/{time_off_id}"


@router.get(MEMBER_PATH, name="list", responses={s: {"model": Error} for s in (401, 404, 422)})
def list_time_off(
    member_id: UUID,
    from_: Annotated[Instant, Query(alias="from")],
    to: Instant,
    current: CurrentSession,
    response: Response,
) -> list[TimeOffOut]:
    """A member's blocks that overlap [from, to), by start. Anyone in the business may read them;
    the reason only the member and owners."""
    if to <= from_ or to - from_ > LONGEST:
        raise ApiError(422, "invalid_window")
    user_id = members.member_user(current, member_id, lock=False)
    rows = current.db.execute(
        text(f"""
        SELECT {fields(may_manage(current, user_id))} FROM time_off
        WHERE member_id = :id AND starts_at < :to AND ends_at > :from
        ORDER BY starts_at, id
        """),
        {"id": member_id, "from": from_, "to": to},
    )
    response.headers["Cache-Control"] = "no-store"
    return [TimeOffOut.model_validate(r, from_attributes=True) for r in rows]


@router.post(
    MEMBER_PATH,
    name="create",
    status_code=201,
    responses={s: {"model": Error} for s in (401, 403, 404, 415, 422)},
)
def create_time_off(
    member_id: UUID, new: TimeOffIn, current: CurrentSession, request: Request, response: Response
) -> TimeOffOut:
    # Locked: a member removed meanwhile is a 404 here, not a foreign key 500 at the insert.
    user_id = members.member_user(current, member_id, lock=True)
    if not may_manage(current, user_id):
        raise ApiError(403, "owner_only")
    checked(new.starts_at, new.ends_at)
    row = current.db.execute(
        text(f"""
        INSERT INTO time_off (tenant_id, member_id, starts_at, ends_at, reason)
        VALUES (current_setting('app.tenant_id')::uuid, :member_id, :starts_at, :ends_at, :reason)
        RETURNING {fields(True)}
        """),
        {"member_id": member_id, **new.model_dump()},
    ).one()
    auth.record(
        current.db,
        request,
        "time_off_created",
        actor_user_id=current.user_id,
        target=f"user:{user_id}",
    )
    response.headers["Cache-Control"] = "no-store"
    return TimeOffOut.model_validate(row, from_attributes=True)


@router.patch(
    BLOCK_PATH, name="update", responses={s: {"model": Error} for s in (401, 403, 404, 415, 422)}
)
def update_time_off(
    time_off_id: UUID,
    change: TimeOffChange,
    current: CurrentSession,
    request: Request,
    response: Response,
) -> TimeOffOut:
    member_id, user_id = manual_block(current, time_off_id)
    # A new statement after the lock: READ COMMITTED sees a change committed while we waited.
    params = {"id": time_off_id, "member_id": member_id}
    where = "WHERE id = :id AND member_id = :member_id AND source = 'manual'"
    old = current.db.execute(text(f"SELECT {fields(True)} FROM time_off {where}"), params).first()
    if old is None:  # deleted while we waited
        raise ApiError(404, "not_found")
    block = TimeOffOut.model_validate(old, from_attributes=True)
    before = block.model_dump(include={"starts_at", "ends_at", "reason"})
    after = before | change.model_dump(exclude_unset=True)
    checked(after["starts_at"], after["ends_at"])
    response.headers["Cache-Control"] = "no-store"
    if after == before:
        return block  # {} or the same values: nothing written, nothing recorded
    row = current.db.execute(
        text(f"""
        UPDATE time_off SET starts_at = :starts_at, ends_at = :ends_at, reason = :reason
        {where} RETURNING {fields(True)}
        """),
        params | after,
    ).one()
    auth.record(
        current.db,
        request,
        "time_off_changed",
        actor_user_id=current.user_id,
        target=f"user:{user_id}",
    )
    return TimeOffOut.model_validate(row, from_attributes=True)


@router.delete(
    BLOCK_PATH,
    name="delete",
    status_code=204,
    responses={s: {"model": Error} for s in (401, 403, 404, 415, 422)},
)
def delete_time_off(time_off_id: UUID, current: CurrentSession, request: Request) -> None:
    member_id, user_id = manual_block(current, time_off_id)
    deleted = current.db.scalar(
        text(
            "DELETE FROM time_off WHERE id = :id AND member_id = :m AND source = 'manual' "
            "RETURNING true"
        ),
        {"id": time_off_id, "m": member_id},
    )
    if deleted is None:
        raise ApiError(404, "not_found")
    auth.record(
        current.db,
        request,
        "time_off_deleted",
        actor_user_id=current.user_id,
        target=f"user:{user_id}",
    )
