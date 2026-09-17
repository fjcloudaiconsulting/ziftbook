"""When a client can book a service: each worker's free slots, from working hours, time off and
bookings, with an automatic buffer after every appointment (ZIF-48)."""

import re
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, BeforeValidator, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import business_settings, limits, schedule
from app.db import tenant_context
from app.errors import ApiError, Error

Interval = tuple[datetime, datetime]  # UTC, [start, end)
# A booking: start, end, its service's buffer override.
Booked = tuple[datetime, datetime, int | None]


def buffer_for(duration_minutes: int, override: int | None, pct: int) -> int:
    """The gap after an appointment: the service's own buffer, else pct of its length, rounded
    up."""
    return override if override is not None else -(-duration_minutes * pct // 100)


def merged(intervals: Iterable[Interval]) -> list[Interval]:
    """Sorted, with overlapping or touching intervals joined."""
    out: list[Interval] = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def overlaps(blocks: list[Interval], start: datetime, end: datetime) -> bool:
    """Whether [start, end) meets any of merged() blocks. Touching isn't overlapping."""
    i = bisect_right(blocks, start, key=lambda b: b[1])  # the first block ending after start
    return i < len(blocks) and blocks[i][0] < end


def anchor(start: time, step: int) -> time | None:
    """A shift's first slot: its start rounded up to the step (10:07 -> 10:15). None past
    midnight."""
    minutes = -(-(start.hour * 60 + start.minute) // step) * step
    return time(*divmod(minutes, 60)) if minutes < 24 * 60 else None


def day_shifts(rows: list[schedule.Row], weekday: int) -> list[tuple[time, time]]:
    """One weekday's shifts in local time, touching ones joined (09:00-10:30 + 10:30-12:00)."""
    joined: list[tuple[time, time]] = []
    for _, start, end in sorted(r for r in rows if r[0] == weekday):
        if joined and start == joined[-1][1]:
            joined[-1] = (joined[-1][0], end)
        else:
            joined.append((start, end))
    return joined


def window(
    now: datetime, zone: str, first: date, last: date, notice: int, horizon: int
) -> tuple[date, date, datetime]:
    """The requested days clamped to [today, today + horizon] in the business's timezone, and the
    earliest bookable instant. Today is local: at 01:00 in Amsterdam the UTC date is yesterday."""
    today = now.astimezone(ZoneInfo(zone)).date()
    return (
        max(first, today),
        min(last, today + timedelta(days=horizon)),
        now + timedelta(minutes=notice),
    )


def member_slots(
    rows: list[schedule.Row],
    time_off: list[Interval],
    booked: list[Booked],
    *,
    zone: str,
    first: date,
    last: date,
    duration: int,
    buffer: int,
    pct: int,
    step: int,
    earliest: datetime,
) -> list[datetime]:
    """One worker's bookable starts on local days first..last, in UTC, ascending."""
    bookings = merged((s, e) for s, e, _ in booked)
    blocked = merged(
        [
            *time_off,
            *(
                (s, e + timedelta(minutes=buffer_for((e - s) // timedelta(minutes=1), o, pct)))
                for s, e, o in booked
            ),
        ]
    )
    length = timedelta(minutes=duration)
    tail = timedelta(minutes=duration + buffer)
    stride = timedelta(minutes=step)
    out: list[datetime] = []
    day = first
    while day <= last:
        for local_start, local_end in day_shifts(rows, day.isoweekday()):
            opens = schedule.to_utc(day, local_start, zone)
            closes = schedule.to_utc(day, local_end, zone)
            at = anchor(local_start, step)
            if at is None or closes <= opens:  # past midnight, or inside a skipped hour (ZIF-46)
                continue
            t = schedule.to_utc(day, at, zone)
            while t < opens:  # an anchor the clock skipped can land before the shift opens
                t += stride
            # ponytail: ~180k iterations at worst (20 workers, 31 days, 5-minute step); add a
            # per-day cutoff if profiling says so.
            while t + length <= closes:
                if (
                    t >= earliest
                    and not overlaps(blocked, t, t + length)
                    and not overlaps(bookings, t, t + tail)
                ):
                    out.append(t)
                t += stride
        day += timedelta(days=1)
    return out


def booked(
    db: Session, members: list[UUID], start: datetime, end: datetime
) -> list[tuple[UUID, datetime, datetime, int | None]]:
    """Bookings that occupy these workers between start and end: (member_id, starts_at, ends_at,
    their service's buffer_minutes). No bookings table yet: ZIF-51 replaces this body."""
    return []


def now() -> datetime:
    return datetime.now(UTC)


router = APIRouter(prefix="/api/public", tags=["availability"])
MAX_DAYS = 31
LIMIT, LIMIT_WINDOW = 60, timedelta(minutes=1)


def ymd(value: object) -> object:
    # Lax date parsing also takes epoch numbers (1789000000) and other shapes.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("not a YYYY-MM-DD date")
    return value


# A local calendar date, exactly YYYY-MM-DD. strict=False: strict mode refuses every string.
Day = Annotated[date, Field(strict=False), BeforeValidator(ymd)]


class Worker(BaseModel):
    id: UUID  # memberships.id; no email, no user id (display name: ruling 1)


class Availability(BaseModel):
    timezone: str  # the business's, for showing the slots in local time
    duration_minutes: int
    workers: list[Worker]  # assigned to the service and with working hours
    slots: list[datetime]  # UTC starts, ascending, unique


@router.get(
    "/businesses/{tenant_id}/services/{service_id}/availability",
    name="read",
    responses={s: {"model": Error} for s in (404, 422, 429)},
)
def read_availability(
    tenant_id: UUID,
    service_id: UUID,
    from_: Annotated[Day, Query(alias="from")],
    to: Day,
    request: Request,
    response: Response,
    member_id: UUID | None = None,
) -> Availability:
    """Bookable starts for a service on local days from..to (at most 31), for one worker or anyone.
    Public: no session."""
    ip = request.client.host if request.client else None
    # Per IP only: a per-business key would let anyone block a business's page.
    if limits.hit({limits.ip_key("availability", ip): LIMIT}, LIMIT_WINDOW):
        raise ApiError(429, "rate_limited")
    if to < from_ or (to - from_).days >= MAX_DAYS:
        raise ApiError(422, "invalid_range")
    request.state.tenant_id = tenant_id
    response.headers["Cache-Control"] = "no-store"
    # An unknown tenant sees no rows (row-level security): a 404 like any other service.
    with tenant_context(tenant_id) as db:
        service = db.execute(
            text("""
            SELECT duration_minutes, buffer_minutes FROM services
            WHERE id = :id AND archived_at IS NULL
            """),
            {"id": service_id},
        ).first()
        if service is None:
            raise ApiError(404, "not_found")
        settings = business_settings.read(db)
        hours: dict[UUID, list[schedule.Row]] = defaultdict(list)
        for m, weekday, starts_at, ends_at in db.execute(
            text("""
            SELECT w.member_id, w.weekday, w.starts_at, w.ends_at
            FROM working_hours w JOIN service_workers s ON s.member_id = w.member_id
            WHERE s.service_id = :service_id
            """),
            {"service_id": service_id},
        ).tuples():
            hours[m].append((weekday, starts_at, ends_at))
        workers = sorted(hours)
        # An unassigned or unknown member_id: no slots, never a 404 (no membership probing).
        chosen = [m for m in workers if member_id in (None, m)]
        zone = settings.timezone
        first, last, earliest = window(
            now(),
            zone,
            from_,
            to,
            settings.min_notice_minutes,
            settings.booking_horizon_days,
        )
        found: set[datetime] = set()
        # Dates only after clamping: to=9999-12-31 would overflow below.
        if chosen and first <= last:
            start = schedule.to_utc(first - timedelta(days=1), time(), zone)
            # Two days past the last: a booking's buffer and a midnight clock change.
            end = schedule.to_utc(last + timedelta(days=2), time(), zone)
            time_off: dict[UUID, list[Interval]] = defaultdict(list)
            # Never the reason: it is private to the member and owners.
            for m, starts_at, ends_at in db.execute(
                text("""
                SELECT member_id, starts_at, ends_at FROM time_off
                WHERE member_id = ANY(CAST(:members AS uuid[]))
                  AND starts_at < :end AND ends_at > :start
                  AND starts_at > CAST(:start AS timestamptz) - interval '366 days'
                """),
                {"members": chosen, "start": start, "end": end},
            ).tuples():
                time_off[m].append((starts_at, ends_at))
            bookings: dict[UUID, list[Booked]] = defaultdict(list)
            for m, starts_at, ends_at, override in booked(db, chosen, start, end):
                bookings[m].append((starts_at, ends_at, override))
            buffer = buffer_for(
                service.duration_minutes, service.buffer_minutes, settings.buffer_pct
            )
            for m in chosen:
                found.update(
                    member_slots(
                        hours[m],
                        time_off[m],
                        bookings[m],
                        zone=zone,
                        first=first,
                        last=last,
                        duration=service.duration_minutes,
                        buffer=buffer,
                        pct=settings.buffer_pct,
                        step=settings.slot_step_minutes,
                        earliest=earliest,
                    )
                )
    return Availability(
        timezone=zone,
        duration_minutes=service.duration_minutes,
        workers=[Worker(id=m) for m in workers],
        slots=sorted(found),
    )
