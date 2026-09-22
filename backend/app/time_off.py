"""When a member can't be booked. A worker manages their own blocks, an owner everyone's; the
reason is shown only to the block's member and to owners."""

import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

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
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import text

from app import auth, availability, business_settings, members
from app.accounts import printable
from app.auth import CurrentSession, SignedIn
from app.errors import ApiError, Error

LONGEST = timedelta(days=366)  # migration 0019, ck_time_off_at_most_366_days
LONGEST_DAYS = 366  # migration 0028, ck_time_off_days: last_day - first_day < 366


def iso_text(value: object) -> object:
    # Lax datetime parsing also takes epoch seconds (1789000000, or "1789000000" in a query).
    if not isinstance(value, str) or not re.match(r"[0-9]{4}-", value):
        raise ValueError("not an ISO 8601 date and time")
    return value


def year_bounds(year: int) -> None:
    # astimezone / schedule.to_utc / date + timedelta all overflow (a 500) outside this range
    # (ruling 4), and Postgres would store a BC date psycopg can't read back.
    if not 2000 <= year <= 2999:
        raise ValueError("out of range")


def utc(value: datetime) -> datetime:
    year_bounds(value.year)  # first: astimezone() itself would overflow before this could run
    return value.astimezone(UTC)


def checked_year(value: date) -> date:
    year_bounds(value.year)
    return value


# With an offset, always: naive is a 422. strict=False because strict mode refuses every string.
Instant = Annotated[
    AwareDatetime, Field(strict=False), BeforeValidator(iso_text), AfterValidator(utc)
]
Reason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    AfterValidator(printable),
]
# A local calendar date, exactly YYYY-MM-DD (availability.Day), years 2000..2999.
Day = Annotated[availability.Day, AfterValidator(checked_year)]
STRICT = ConfigDict(strict=True, extra="forbid")
PAIR_FIELDS = ("starts_at", "ends_at", "first_day", "last_day")


class TimeOffChange(BaseModel):
    """Only the fields sent change. reason may be null (clears it); the times and the days may
    not. A body carries fields of one kind: within the block's own kind any subset merges; to
    switch kinds send the complete new pair (checked at the route, which knows the block's kind)."""

    model_config = STRICT
    starts_at: Instant | SkipJsonSchema[None] = None
    ends_at: Instant | SkipJsonSchema[None] = None
    first_day: Day | SkipJsonSchema[None] = None
    last_day: Day | SkipJsonSchema[None] = None
    reason: Reason | None = None

    @field_validator("starts_at", "ends_at", "first_day", "last_day", mode="before")
    @classmethod
    def not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("null")
        return value


class TimeOffIn(TimeOffChange):
    """Exactly one complete pair: {starts_at, ends_at} (a partial block) or {first_day, last_day}
    (a whole-day block, both days included). An explicit null for any of the four is refused."""

    @model_validator(mode="after")
    def one_pair(self) -> TimeOffIn:
        sent = {f for f in PAIR_FIELDS if getattr(self, f) is not None}
        if sent not in ({"starts_at", "ends_at"}, {"first_day", "last_day"}):
            raise ValueError("send exactly one complete pair")
        return self


class TimeOffOut(BaseModel):
    id: UUID
    member_id: UUID
    starts_at: datetime | None
    ends_at: datetime | None
    first_day: date | None
    last_day: date | None
    reason: str | None  # null when there is none, or when the caller may not see it
    source: Literal["manual", "google"]


def fields(sees_reason: bool) -> str:
    return "id, member_id, starts_at, ends_at, first_day, last_day, source, " + (
        "reason" if sees_reason else "NULL::text AS reason"
    )


def checked(starts_at: datetime, ends_at: datetime) -> None:
    if ends_at <= starts_at:
        raise ApiError(422, "end_not_after_start")
    if ends_at - starts_at > LONGEST:
        raise ApiError(422, "time_off_too_long")


def checked_days(first_day: date, last_day: date) -> None:
    # Never checked() / LONGEST on a derived span: a day count, not elapsed time (migration 0028's
    # comment on ck_time_off_days). first_day == last_day is a valid single day off.
    if last_day < first_day:
        raise ApiError(422, "last_day_before_first_day")
    if (last_day - first_day).days >= LONGEST_DAYS:
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
    if not members.may_manage(current, user_id):
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
    """A member's blocks that overlap [from, to), by effective start (a day block's local
    midnight). Anyone in the business may read them; the reason only the member and owners."""
    if to <= from_ or to - from_ > LONGEST:
        raise ApiError(422, "invalid_window")
    user_id = members.member_user(current, member_id, lock=False)
    # Read once: day rows match by local date, and midnight is monotonic in the date, so this
    # exact-not-padded window is safe even though `to` is exclusive on the instant side.
    zone = business_settings.read(current.db).timezone
    tz = ZoneInfo(zone)
    first = from_.astimezone(tz).date()
    last = (to - timedelta(microseconds=1)).astimezone(tz).date()
    rows = current.db.execute(
        text(f"""
        SELECT {fields(members.may_manage(current, user_id))} FROM time_off
        WHERE member_id = :id
          AND ((starts_at < :to AND ends_at > :from)
            OR (first_day <= :last AND last_day >= :first))
        """),
        {"id": member_id, "from": from_, "to": to, "first": first, "last": last},
    )
    blocks = [TimeOffOut.model_validate(r, from_attributes=True) for r in rows]

    # SQL cannot compute this: AT TIME ZONE is banned (Postgres lacks 113 tzdata names).
    def effective_start(b: TimeOffOut) -> datetime:
        if b.starts_at is not None:
            return b.starts_at
        assert b.first_day is not None  # ck_time_off_kind: exactly one pair is non-null
        return availability.day_span(b.first_day, b.first_day, zone)[0]

    blocks.sort(key=lambda b: (effective_start(b), b.id))
    response.headers["Cache-Control"] = "no-store"
    return blocks


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
    if not members.may_manage(current, user_id):
        raise ApiError(403, "owner_only")
    if new.starts_at is not None:
        assert new.ends_at is not None  # one_pair already refused a half pair
        checked(new.starts_at, new.ends_at)
    else:
        assert new.first_day is not None and new.last_day is not None
        checked_days(new.first_day, new.last_day)
    row = current.db.execute(
        text(f"""
        INSERT INTO time_off (tenant_id, member_id, starts_at, ends_at, first_day, last_day, reason)
        VALUES (
          current_setting('app.tenant_id')::uuid, :member_id, :starts_at, :ends_at, :first_day,
          :last_day, :reason)
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
    sent = change.model_dump(exclude_unset=True)
    instant_fields = {"starts_at", "ends_at"} & sent.keys()
    day_fields = {"first_day", "last_day"} & sent.keys()
    if instant_fields and day_fields:  # one body, one kind
        raise ApiError(422, "invalid_request")
    is_day_block = block.first_day is not None
    if day_fields:
        first_day: date
        last_day: date
        if is_day_block:  # merges within the block's own kind
            assert block.first_day is not None and block.last_day is not None
            first_day = sent.get("first_day", block.first_day)
            last_day = sent.get("last_day", block.last_day)
        elif day_fields == {"first_day", "last_day"}:  # switching kind: the complete new pair
            first_day, last_day = sent["first_day"], sent["last_day"]
        else:
            raise ApiError(422, "invalid_request")
        checked_days(first_day, last_day)
        after = {
            "starts_at": None,
            "ends_at": None,
            "first_day": first_day,
            "last_day": last_day,
            "reason": sent.get("reason", block.reason),
        }
    elif instant_fields:
        starts_at: datetime
        ends_at: datetime
        if not is_day_block:  # merges within the block's own kind
            assert block.starts_at is not None and block.ends_at is not None
            starts_at = sent.get("starts_at", block.starts_at)
            ends_at = sent.get("ends_at", block.ends_at)
        elif instant_fields == {"starts_at", "ends_at"}:  # switching kind: the complete new pair
            starts_at, ends_at = sent["starts_at"], sent["ends_at"]
        else:
            raise ApiError(422, "invalid_request")
        checked(starts_at, ends_at)
        after = {
            "starts_at": starts_at,
            "ends_at": ends_at,
            "first_day": None,
            "last_day": None,
            "reason": sent.get("reason", block.reason),
        }
    else:  # {} or {reason} alone: the kind, and the other pair, are untouched
        after = block.model_dump(include={"starts_at", "ends_at", "first_day", "last_day"}) | {
            "reason": sent.get("reason", block.reason)
        }
    before = block.model_dump(include={"starts_at", "ends_at", "first_day", "last_day", "reason"})
    response.headers["Cache-Control"] = "no-store"
    if after == before:
        return block  # {} or the same values: nothing written, nothing recorded
    row = current.db.execute(
        text(f"""
        UPDATE time_off SET starts_at = :starts_at, ends_at = :ends_at, first_day = :first_day,
                             last_day = :last_day, reason = :reason
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
