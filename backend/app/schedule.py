"""A member's working week, in local time, and turning local times into instants (ZIF-46).

Also opening_hours (ZIF-105): the business's own weekly envelope every member's working hours and
every bookable slot must fall inside.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from itertools import pairwise
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Request, Response
from psycopg.errors import ExclusionViolation
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Row as SqlRow  # aliased: this module's own Row is the (weekday, from, to)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth, business_settings, members
from app.auth import CurrentOwner, CurrentSession, SignedIn
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


def day_shifts(rows: list[Row], weekday: int) -> list[tuple[time, time]]:
    """One weekday's shifts in local time, touching ones joined (09:00-10:30 + 10:30-12:00)."""
    joined: list[tuple[time, time]] = []
    for _, start, end in sorted(r for r in rows if r[0] == weekday):
        if joined and start == joined[-1][1]:
            joined[-1] = (joined[-1][0], end)
        else:
            joined.append((start, end))
    return joined


def by_weekday(rows: list[Row]) -> dict[int, list[tuple[time, time]]]:
    """Every weekday's joined shifts at once, keyed by weekday; a weekday with no rows is absent.

    day_shifts scans the whole list, so callers that ask about many weekdays (availability: up to
    14 days x 20 workers) build this once instead.
    """
    return {weekday: day_shifts(rows, weekday) for weekday in {r[0] for r in rows}}


def envelope(db: Session) -> dict[int, list[tuple[time, time]]]:
    """The business's opening week, keyed by weekday; `{}` when it never configured any.

    One statement, never one per weekday or per worker. No WHERE tenant_id: row-level security
    scopes it. No ORDER BY: day_shifts sorts. `{}` is falsy, exactly like the row list it came
    from, so "never configured" stays a plain truth test on the result.
    """
    rows: list[Row] = list(
        db.execute(text("SELECT weekday, starts_at, ends_at FROM opening_hours")).tuples()
    )
    return by_weekday(rows)


def within(shift: tuple[time, time], envelope: list[tuple[time, time]]) -> bool:
    """Whether a shift fits entirely inside one of a weekday's joined opening shifts."""
    start, end = shift
    return any(opens <= start and end <= closes for opens, closes in envelope)


def week(current: SignedIn, member_id: UUID) -> list[Shift]:
    rows = current.db.execute(
        text("""
        SELECT weekday, starts_at, ends_at FROM working_hours
        WHERE member_id = :id ORDER BY weekday, starts_at
        """),
        {"id": member_id},
    ).all()
    return to_shifts(rows)


def to_shifts(rows: Sequence[SqlRow[Any]]) -> list[Shift]:
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
    settings = business_settings.read(current.db)
    if current.role != "owner" and not (
        user_id == current.user_id and settings.workers_edit_own_hours
    ):
        raise ApiError(403, "owner_only")
    rows = sorted(
        (s.weekday, time.fromisoformat(s.starts_at), time.fromisoformat(s.ends_at)) for s in shifts
    )
    if any(end <= start for _, start, end in rows):
        raise ApiError(422, "end_not_after_start")
    if overlapping(rows):
        raise ApiError(422, "overlapping_hours")
    # After the 403, never before: authorise first, then do the endpoint's work. (Not a secrecy
    # rule - any signed-in member reads the identical envelope from GET /api/opening-hours.)
    # One statement; no rows means the business never configured opening hours, and behaves exactly
    # as it did before ZIF-105. Checked against the JOINED shifts, so 09:00-15:00 fits opening rows
    # 09:00-12:00 + 12:00-15:00. Not a trigger: a trigger fires per row, cannot name the weekday,
    # and would put a per-tenant set_config duty on every future data migration touching
    # working_hours (CONTRIBUTING.md:70-71).
    open_days = envelope(current.db)
    if open_days:
        for weekday, start, end in rows:
            if not within((start, end), open_days.get(weekday, [])):
                raise ApiError(422, "outside_opening_hours", weekday=weekday)
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


opening_router = APIRouter(prefix="/api", tags=["opening-hours"])
OPENING_PATH = "/opening-hours"
OPENING_OVERLAP = "ex_opening_hours_overlap"  # migration 0025
# The first key of the two-key advisory lock below: a namespace of our own, so the second key is
# free to be any hash of the tenant id without colliding with another feature's lock.
OPENING_LOCK = 105  # ZIF-105


def opening_week(current: SignedIn) -> list[Shift]:
    return to_shifts(
        current.db.execute(
            text("""
            SELECT weekday, starts_at, ends_at FROM opening_hours ORDER BY weekday, starts_at
            """)
        ).all()
    )


def lock_week(current: SignedIn) -> None:
    """Serialize two owners replacing the opening week.

    Not optional. The replace body is an unqualified DELETE + INSERT, and under READ COMMITTED two
    concurrent saves are not last-writer-wins: the second DELETE wakes to find the first's rows
    already gone and cannot see its inserts, so both weeks end up stored - an envelope wider than
    either owner asked for, which re-exposes exactly the stale working hours this table exists to
    fence off.

    Transaction-scoped and released on commit or rollback. An advisory lock, not a row lock: there
    is no membership row to lock here, and an endpoint must never lock tenants
    (CONTRIBUTING.md:79-83). The INSERT still takes only the KEY SHARE its foreign key takes.
    Its own function, so the concurrency test has the same seam ZIF-46's test takes on
    members.member_user.
    """
    # hashtext is 32-bit, so two unrelated tenants can in principle share a key: that costs one of
    # them a wait, never a wrong answer, since every holder replaces only its own rows. It is also
    # an internal Postgres function whose hash is not promised to be stable across major versions -
    # harmless here, because a key only ever has to agree with other live sessions, never with a
    # value stored anywhere.
    current.db.execute(
        text("SELECT pg_advisory_xact_lock(:key, hashtext(current_setting('app.tenant_id')))"),
        {"key": OPENING_LOCK},
    )


@opening_router.get(OPENING_PATH, name="read", responses={401: {"model": Error}})
def read_opening_hours(current: CurrentSession, response: Response) -> list[Shift]:
    """When the business is open, Monday first. Anyone in the business may read it: a worker needs
    the envelope while editing their own week."""
    response.headers["Cache-Control"] = "no-store"
    return opening_week(current)


# Design notes, deliberately a comment and not a docstring: a docstring here ships verbatim into
# openapi.json as the operation's description, and from there into the generated web client.
#
# An empty week is refused (opening_hours_required) - see this handler's docstring. To stop taking
# bookings: clear working hours, archive the services, or block the period as time off (ZIF-101).
#
# Narrowing the week is accepted and never touches working_hours (ZIF-105, accept-and-narrow):
# availability clips. Two visible costs: the console may show a worker working past closing (a
# ZIF-50 hint), and that worker's next PUT of their unchanged week is a 422 naming the day - which
# is the acceptance criterion, not a bug. Reject loses because a {"code": str} body cannot name the
# blocking members and naming them would be personal data; clamp loses because replace_week
# replaces a week wholesale, so a clamp is silent data loss with no history.
@opening_router.put(
    OPENING_PATH, name="replace", responses={s: {"model": Error} for s in (401, 403, 415, 422)}
)
def replace_opening_hours(
    shifts: Annotated[list[Shift], Body(max_length=MAX_SHIFTS)],
    current: CurrentOwner,
    request: Request,
    response: Response,
) -> list[Shift]:
    """Replace the business's whole opening week. Owners only; an empty week is refused, because no
    rows is how this table says "never configured"."""
    lock_week(current)
    rows = sorted(
        (s.weekday, time.fromisoformat(s.starts_at), time.fromisoformat(s.ends_at)) for s in shifts
    )
    if not rows:
        raise ApiError(422, "opening_hours_required")
    if any(end <= start for _, start, end in rows):
        raise ApiError(422, "end_not_after_start")
    if overlapping(rows):
        raise ApiError(422, "overlapping_hours")
    # The WHERE is redundant - row-level security already scopes it - and deliberate: this exact
    # statement copied into a psql session as a superuser would otherwise wipe every tenant's week.
    old = (
        current.db.execute(
            text(
                "DELETE FROM opening_hours "
                "WHERE tenant_id = current_setting('app.tenant_id')::uuid "
                "RETURNING weekday, starts_at, ends_at"
            )
        )
        .tuples()
        .all()
    )
    try:  # no `if rows:` guard: the empty week was refused above
        current.db.execute(
            text("""
            INSERT INTO opening_hours (tenant_id, weekday, starts_at, ends_at)
            VALUES (current_setting('app.tenant_id')::uuid, :weekday, :starts_at, :ends_at)
            """),
            [{"weekday": d, "starts_at": s, "ends_at": e} for d, s, e in rows],
        )
    except IntegrityError as error:
        # Only our overlap rule, keyed on this table's constraint; anything else stays a 500.
        if (
            isinstance(error.orig, ExclusionViolation)
            and error.orig.diag.constraint_name == OPENING_OVERLAP
        ):
            raise ApiError(422, "overlapping_hours") from None
        raise
    if sorted(old) != rows:
        auth.record(current.db, request, "opening_hours_changed", actor_user_id=current.user_id)
    response.headers["Cache-Control"] = "no-store"
    return opening_week(current)
