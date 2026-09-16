"""A member's working week, in local time, and turning local times into instants (ZIF-46)."""

from datetime import UTC, date, datetime, time
from itertools import pairwise
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Request, Response
from psycopg.errors import ExclusionViolation
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import auth, business_settings, members
from app.auth import CurrentSession, SignedIn
from app.errors import ApiError, Error

OVERLAP = "ex_working_hours_overlap"  # migration 0017
MAX_SHIFTS = 50
# 00:00-23:59, minutes only. [0-9], not \d: pydantic's \d takes any Unicode digit, which
# time.fromisoformat refuses (a 500).
HH_MM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def to_utc(day: date, at: time, zone: str) -> datetime:
    """The instant a business's clock shows `at` on `day`, in UTC.

    Convert here, never with SQL AT TIME ZONE (CONTRIBUTING). A time the clock skips (02:30 as
    summer time starts) takes the offset from before the change, so it lands at 03:30 summer time.
    A time the clock shows twice is the first of the two. A shift starting inside a skipped hour
    (02:30-03:15 on 2026-03-29 Amsterdam) converts to an interval that ends before it starts, and a
    caller (availability, ZIF-48) must drop or clamp an interval whose end isn't after its start.

    Availability (ZIF-48) is its first caller; until then only tests use it.
    """
    return datetime.combine(day, at, ZoneInfo(zone)).astimezone(UTC)


class Shift(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    weekday: Annotated[int, Field(ge=1, le=7)]  # ISO: 1 is Monday
    starts_at: Annotated[str, Field(pattern=HH_MM)]
    ends_at: Annotated[str, Field(pattern=HH_MM)]


Row = tuple[int, time, time]


def overlapping(rows: list[Row]) -> bool:
    """Whether two shifts on one day overlap. Touching (12:00-12:00) isn't overlapping."""
    ordered = sorted(rows)
    # Sorted, and stopping at the first overlap: each end is the latest so far that day.
    return any(
        day == next_day and next_start < end
        for (day, _, end), (next_day, next_start, _) in pairwise(ordered)
    )


def week(current: SignedIn, member_id: UUID) -> list[Shift]:
    rows = current.db.execute(
        text("""
        SELECT weekday, starts_at, ends_at FROM working_hours
        WHERE member_id = :id ORDER BY weekday, starts_at
        """),
        {"id": member_id},
    )
    return [
        Shift(weekday=r.weekday, starts_at=f"{r.starts_at:%H:%M}", ends_at=f"{r.ends_at:%H:%M}")
        for r in rows
    ]


router = APIRouter(prefix="/api", tags=["working-hours"])
PATH = "/members/{member_id}/working-hours"


@router.get(PATH, name="read", responses={s: {"model": Error} for s in (401, 404, 422)})
def read_week(member_id: UUID, current: CurrentSession, response: Response) -> list[Shift]:
    """A member's working hours, Monday first. Anyone in the business may read them."""
    members.member_user(current, member_id, lock=False)
    response.headers["Cache-Control"] = "no-store"
    return week(current, member_id)


@router.put(
    PATH, name="replace", responses={s: {"model": Error} for s in (401, 403, 404, 415, 422)}
)
def replace_week(
    member_id: UUID,
    shifts: Annotated[list[Shift], Body(max_length=MAX_SHIFTS)],
    current: CurrentSession,
    request: Request,
    response: Response,
) -> list[Shift]:
    """Replace a member's whole week. An owner changes anyone's; a worker only their own, and only
    if the business allows it."""
    # First, before any other statement: two saves of one week run one after the other. Never lock
    # tenants here: keep_an_owner locks memberships and then tenants (migration 0014).
    user_id = members.member_user(current, member_id, lock=True)
    if current.role != "owner" and not (
        user_id == current.user_id and business_settings.read(current.db).workers_edit_own_hours
    ):
        raise ApiError(403, "owner_only")
    rows = sorted(
        (s.weekday, time.fromisoformat(s.starts_at), time.fromisoformat(s.ends_at)) for s in shifts
    )
    if any(end <= start for _, start, end in rows):
        raise ApiError(422, "end_not_after_start")
    if overlapping(rows):
        raise ApiError(422, "overlapping_hours")
    old = (
        current.db.execute(
            text(
                "DELETE FROM working_hours WHERE member_id = :id "
                "RETURNING weekday, starts_at, ends_at"
            ),
            {"id": member_id},
        )
        .tuples()
        .all()
    )
    if rows:  # an empty list would run the statement once with no values
        try:
            current.db.execute(
                text("""
                INSERT INTO working_hours (tenant_id, member_id, weekday, starts_at, ends_at)
                VALUES (:tenant_id, :member_id, :weekday, :starts_at, :ends_at)
                """),
                [
                    {
                        "tenant_id": current.tenant_id,
                        "member_id": member_id,
                        "weekday": d,
                        "starts_at": s,
                        "ends_at": e,
                    }
                    for d, s, e in rows
                ],
            )
        except IntegrityError as error:
            # Only our overlap rule; any other integrity error stays a 500.
            if (
                isinstance(error.orig, ExclusionViolation)
                and error.orig.diag.constraint_name == OVERLAP
            ):
                raise ApiError(422, "overlapping_hours") from None
            raise
    if sorted(old) != rows:
        auth.record(
            current.db,
            request,
            "working_hours_changed",
            actor_user_id=current.user_id,
            target=f"user:{user_id}",
        )
    response.headers["Cache-Control"] = "no-store"
    return week(current, member_id)
