"""The availability core (ZIF-48): pure functions, no database. Local times are written local,
expectations in UTC; the business is in Europe/Amsterdam."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import availability
from app.availability import (
    Booked,
    Interval,
    anchor,
    buffer_for,
    clip,
    day_span,
    member_slots,
    merged,
    overlaps,
)
from app.schedule import Row, by_weekday, to_utc

ZONE = "Europe/Amsterdam"
LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)
MONDAY = date(2026, 3, 30)  # summer time: local = UTC + 2


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def local(day: date, at: str) -> datetime:
    """A local wall-clock time on day, as UTC."""
    return to_utc(day, time.fromisoformat(at), ZONE)


def rows(weekday: int, *shifts: tuple[str, str]) -> list[Row]:
    return [(weekday, time.fromisoformat(s), time.fromisoformat(e)) for s, e in shifts]


def spans(*shifts: tuple[str, str]) -> list[tuple[time, time]]:
    """One weekday's local shifts, the shape clip takes and returns."""
    return [(time.fromisoformat(s), time.fromisoformat(e)) for s, e in shifts]


def slots(
    shift_rows: list[Row],
    first: date,
    last: date | None = None,
    *,
    time_off: list[Interval] | None = None,
    booked: list[Booked] | None = None,
    opening: list[Row] | None = None,
    duration: int = 60,
    buffer: int = 0,
    pct: int = 0,
    step: int = 60,
    earliest: datetime = LONG_AGO,
) -> list[datetime]:
    return member_slots(
        shift_rows,
        time_off or [],
        booked or [],
        opening=by_weekday(opening or []),
        zone=ZONE,
        first=first,
        last=last or first,
        duration=duration,
        buffer=buffer,
        pct=pct,
        step=step,
        earliest=earliest,
    )


def at(day: date, *times: str) -> list[datetime]:
    return [local(day, t) for t in times]


# 1. buffer_for


@pytest.mark.parametrize(
    ("duration", "override", "pct", "expected"),
    [
        (30, None, 10, 3),
        (45, None, 10, 5),
        (60, 0, 10, 0),
        (60, 15, 10, 15),
        (5, None, 0, 0),
        (720, None, 100, 720),
    ],
    ids=["pct", "rounded up", "zero override wins", "override", "zero pct", "whole"],
)
def test_the_buffer_is_the_override_else_a_percentage_rounded_up(
    duration: int, override: int | None, pct: int, expected: int
) -> None:
    assert buffer_for(duration, override, pct) == expected


# 2. anchor


@pytest.mark.parametrize(
    ("start", "step", "expected"),
    [
        ("10:00", 15, "10:00"),
        ("10:07", 15, "10:15"),
        ("10:07", 5, "10:10"),
        ("13:10", 15, "13:15"),
        ("00:00", 60, "00:00"),
        ("23:50", 15, None),
    ],
)
def test_a_shift_s_first_slot_is_its_start_rounded_up_to_the_step(
    start: str, step: int, expected: str | None
) -> None:
    result = anchor(time.fromisoformat(start), step)
    assert result == (None if expected is None else time.fromisoformat(expected))


# 3. merged


def test_merged_sorts_and_joins_overlapping_and_touching_intervals() -> None:
    a, b, c, d, e, f = (utc(f"2026-03-30T{h:02}:00") for h in (8, 9, 10, 11, 12, 14))
    g = utc("2026-03-30T15:00")
    assert merged([(e, f), (c, d), (a, c), (b, c), (f, g)]) == [(a, d), (e, g)]
    assert merged([(d, e), (a, b)]) == [(a, b), (d, e)]
    assert merged([]) == []


# 4. overlaps


def test_overlaps_counts_containment_and_partial_overlap_but_not_touching() -> None:
    nine, ten, eleven, noon = (utc(f"2026-03-30T{h:02}:00") for h in (9, 10, 11, 12))
    half = timedelta(minutes=30)
    blocks = [(ten, eleven)]
    assert not overlaps(blocks, nine, ten)  # touching before
    assert not overlaps(blocks, eleven, noon)  # touching after
    assert overlaps(blocks, ten + timedelta(minutes=10), ten + half)  # contained
    assert overlaps(blocks, nine, noon)  # contains
    assert overlaps(blocks, nine + half, ten + half)  # partial, before
    assert overlaps(blocks, ten + half, eleven + half)  # partial, after
    assert not overlaps([], nine, noon)


# 5. window


NOW = utc("2026-07-15T23:00")  # 01:00 on 2026-07-16 in Amsterdam


def test_the_window_starts_today_in_the_business_s_timezone() -> None:
    result = availability.window(NOW, ZONE, date(2026, 7, 15), date(2026, 7, 20), 60, 1)
    assert result == (date(2026, 7, 16), date(2026, 7, 17), utc("2026-07-16T00:00"))


def test_a_window_ending_before_today_is_empty() -> None:
    first, last, _ = availability.window(NOW, ZONE, date(2026, 7, 10), date(2026, 7, 15), 60, 60)
    assert first > last


# 6. member_slots


def test_plain_slots_on_one_shift() -> None:
    assert slots(rows(1, ("09:00", "12:00")), MONDAY) == [
        utc("2026-03-30T07:00"),
        utc("2026-03-30T08:00"),
        utc("2026-03-30T09:00"),
    ]


def test_each_day_uses_its_own_offset_around_the_change() -> None:
    week = rows(6, ("09:00", "10:00")) + rows(7, ("09:00", "10:00"))
    assert slots(week, date(2026, 3, 28), date(2026, 3, 29)) == [
        utc("2026-03-28T08:00"),
        utc("2026-03-29T07:00"),
    ]


def test_a_shift_across_the_spring_change_has_three_hours() -> None:
    assert slots(rows(7, ("01:00", "05:00")), date(2026, 3, 29)) == [
        utc("2026-03-29T00:00"),
        utc("2026-03-29T01:00"),
        utc("2026-03-29T02:00"),
    ]


def test_a_shift_across_the_autumn_change_has_five_hours() -> None:
    assert slots(rows(7, ("01:00", "05:00")), date(2026, 10, 25)) == [
        utc("2026-10-24T23:00"),
        utc("2026-10-25T00:00"),
        utc("2026-10-25T01:00"),
        utc("2026-10-25T02:00"),
        utc("2026-10-25T03:00"),
    ]


def test_a_shift_opening_in_the_repeated_hour_offers_the_grid_before_its_anchor() -> None:
    # 02:10 is its first occurrence (00:10Z); 01:00Z is the second 02:00, on the grid.
    assert slots(rows(7, ("02:10", "05:00")), date(2026, 10, 25)) == [
        utc("2026-10-25T01:00"),
        utc("2026-10-25T02:00"),
        utc("2026-10-25T03:00"),
    ]


def test_shifts_around_the_skipped_hour_give_each_start_once_in_order() -> None:
    # 02:10-02:40 converts to 01:10Z-01:40Z, inside 03:00-04:00 (01:00Z-02:00Z).
    around = rows(7, ("02:10", "02:40"), ("03:00", "04:00"))
    found = slots(around, date(2026, 3, 29), duration=10, step=10)
    assert found == [utc(f"2026-03-29T01:{m}0") for m in range(6)]


def test_a_shift_inside_the_skipped_hour_is_dropped() -> None:
    assert slots(rows(7, ("02:30", "03:15")), date(2026, 3, 29), duration=15, step=15) == []


def test_an_anchor_the_clock_skipped_never_lands_before_the_shift_opens() -> None:
    found = slots(rows(7, ("02:50", "05:00")), date(2026, 3, 29), duration=15, step=15)
    assert found[0] == utc("2026-03-29T02:00")  # 04:00 summer time
    assert min(found) >= utc("2026-03-29T01:50")  # 02:50 converts to 01:50Z


def test_a_split_shift_has_no_slot_in_the_gap() -> None:
    split = rows(1, ("09:00", "12:00"), ("13:00", "15:00"))
    assert slots(split, MONDAY) == at(MONDAY, "09:00", "10:00", "11:00", "13:00", "14:00")


def test_an_odd_second_start_has_its_own_anchor() -> None:
    split = rows(1, ("09:00", "10:00"), ("13:10", "15:00"))
    found = slots(split, MONDAY, duration=30, step=15)
    assert [t for t in found if t > local(MONDAY, "12:00")][0] == local(MONDAY, "13:15")


def test_touching_shifts_are_joined() -> None:
    joined = rows(1, ("09:00", "10:30"), ("10:30", "12:00"))
    assert slots(joined, MONDAY, step=30) == at(MONDAY, "09:00", "09:30", "10:00", "10:30", "11:00")


def test_slots_stay_on_the_grid_from_the_rounded_start() -> None:
    found = slots(rows(1, ("10:07", "11:00")), MONDAY, duration=15, step=15)
    assert found == at(MONDAY, "10:15", "10:30", "10:45")


def test_a_slot_ending_exactly_at_the_shift_end_is_offered() -> None:
    assert slots(rows(1, ("09:00", "10:00")), MONDAY) == at(MONDAY, "09:00")


DAY_SHIFT = rows(1, ("09:00", "12:00"))


def test_time_off_blocks_what_it_overlaps_and_nothing_it_touches() -> None:
    off = [(local(MONDAY, "10:00"), local(MONDAY, "10:30"))]
    found = slots(DAY_SHIFT, MONDAY, time_off=off, duration=30, step=15)
    assert local(MONDAY, "09:30") in found
    assert local(MONDAY, "10:30") in found
    for blocked in at(MONDAY, "09:45", "10:00", "10:15"):
        assert blocked not in found


def test_a_booking_blocks_its_own_override_buffer() -> None:
    booking = (local(MONDAY, "10:00"), local(MONDAY, "10:30"), 10)
    found = slots(DAY_SHIFT, MONDAY, booked=[booking], duration=30, step=15)
    assert local(MONDAY, "10:30") not in found
    assert local(MONDAY, "10:45") in found
    assert local(MONDAY, "09:30") in found


def test_a_booking_without_an_override_blocks_a_percentage_of_its_length() -> None:
    booking = (local(MONDAY, "10:00"), local(MONDAY, "10:45"), None)
    found = slots(DAY_SHIFT, MONDAY, booked=[booking], duration=15, pct=10, step=5)
    assert local(MONDAY, "10:45") not in found
    assert local(MONDAY, "10:50") in found


def test_a_new_slot_needs_its_own_buffer_before_the_next_booking() -> None:
    booking = (local(MONDAY, "11:00"), local(MONDAY, "11:30"), 0)
    found = slots(DAY_SHIFT, MONDAY, booked=[booking], duration=30, buffer=10, step=15)
    assert local(MONDAY, "10:15") in found
    assert local(MONDAY, "10:30") not in found


def test_a_new_slot_s_buffer_may_run_past_the_shift_end_or_into_time_off() -> None:
    assert slots(rows(1, ("09:00", "10:00")), MONDAY, buffer=10) == at(MONDAY, "09:00")
    off = [(local(MONDAY, "10:00"), local(MONDAY, "12:00"))]
    assert local(MONDAY, "09:00") in slots(DAY_SHIFT, MONDAY, time_off=off, buffer=10)


def test_minimum_notice_moves_the_first_slot() -> None:
    later = slots(DAY_SHIFT, MONDAY, duration=15, step=15, earliest=local(MONDAY, "10:10"))
    assert later[0] == local(MONDAY, "10:15")
    exact = slots(DAY_SHIFT, MONDAY, duration=15, step=15, earliest=local(MONDAY, "10:15"))
    assert exact[0] == local(MONDAY, "10:15")


# 7. end to end, at 01:00 in Amsterdam


def test_today_is_the_business_s_local_date_end_to_end() -> None:
    first, last, earliest = availability.window(
        NOW, ZONE, date(2026, 7, 15), date(2026, 7, 20), 60, 1
    )
    every_day = [r for weekday in range(1, 8) for r in rows(weekday, ("09:00", "10:00"))]
    found = member_slots(
        every_day,
        [],
        [],
        opening={},
        zone=ZONE,
        first=first,
        last=last,
        duration=60,
        buffer=0,
        pct=0,
        step=60,
        earliest=earliest,
    )
    assert found == [utc("2026-07-16T07:00"), utc("2026-07-17T07:00")]


# 8. the opening-hours envelope (ZIF-105)


# F22: clip's whole geometry, in one place. Each case kills a different wrong implementation:
# never cutting the end, keeping only the first envelope piece a shift meets, dropping the second
# worker shift, and relaxing the strict `<` that makes a touching pair produce nothing.
@pytest.mark.parametrize(
    ("shifts", "envelope", "expected"),
    [
        ([("09:00", "17:00")], [("09:00", "12:00")], [("09:00", "12:00")]),
        ([("08:00", "12:00")], [("09:00", "17:00")], [("09:00", "12:00")]),
        ([("08:00", "18:00")], [("10:00", "12:00")], [("10:00", "12:00")]),
        ([("09:00", "12:00")], [("12:00", "17:00")], []),
        ([("14:00", "16:00")], [("09:00", "12:00")], []),
        (
            [("09:00", "17:00")],
            [("09:00", "12:00"), ("13:00", "17:00")],
            [("09:00", "12:00"), ("13:00", "17:00")],
        ),
        (
            [("09:00", "11:00"), ("14:00", "18:00")],
            [("10:00", "16:00")],
            [("10:00", "11:00"), ("14:00", "16:00")],
        ),
        ([("09:00", "17:00")], [], []),
    ],
    ids=[
        "end clipped",
        "start clipped",
        "both ends clipped",
        "touching, so nothing",
        "disjoint",
        "one shift split by a lunch closure",
        "two shifts, both clipped",
        "shut that weekday",
    ],
)
def test_clip_cuts_a_day_s_shifts_to_that_day_s_envelope(
    shifts: list[tuple[str, str]],
    envelope: list[tuple[str, str]],
    expected: list[tuple[str, str]],
) -> None:
    assert clip(spans(*shifts), spans(*envelope)) == spans(*expected)


def test_a_shift_running_past_closing_sells_nothing_after_closing() -> None:
    # F23: the ticket's headline scenario, through member_slots. Shop open 09:00-12:00, worker
    # rostered 09:00-17:00. Without clip's min(end, closes) this is 09:00-16:00, eight slots.
    found = slots(
        rows(1, ("09:00", "17:00")),
        MONDAY,
        opening=rows(1, ("09:00", "12:00")),
        duration=60,
        step=60,
    )
    assert found == at(MONDAY, "09:00", "10:00", "11:00")


def test_a_lunch_closure_splits_the_day_and_sells_nothing_in_the_gap() -> None:
    # F24: a genuinely split envelope - 09:00-12:00 + 13:00-17:00, which day_shifts cannot join -
    # against one worker shift spanning both. The afternoon must survive (an implementation taking
    # only the first piece a shift meets would lose it) and the closure must sell nothing.
    found = slots(
        rows(1, ("09:00", "17:00")),
        MONDAY,
        opening=rows(1, ("09:00", "12:00"), ("13:00", "17:00")),
        duration=60,
        step=60,
    )
    assert found == at(MONDAY, "09:00", "10:00", "11:00", "13:00", "14:00", "15:00", "16:00")


def test_the_envelope_is_clipped_in_local_time_not_in_utc() -> None:
    # F3: the spring-forward DST fence. Europe/Amsterdam, 2026-03-29, 02:00 CET -> 03:00 CEST.
    # Worker 02:30-05:00, opening 03:00-05:00. Clipped in local wall clock: max(02:30, 03:00)
    # = 03:00 -> piece (03:00, 05:00) -> opens=01:00Z, closes=03:00Z, anchor(03:00, 60)=03:00 ->
    # t=01:00Z, neither walk loop moves it. Clipping in UTC instead would compare 02:30 local
    # (01:30Z, still on winter time) with 03:00 local (01:00Z) and keep the later 01:30Z as the
    # start, losing this 01:00Z slot.
    found = slots(
        rows(7, ("02:30", "05:00")),
        date(2026, 3, 29),
        opening=rows(7, ("03:00", "05:00")),
        duration=60,
        step=60,
    )
    assert found == [utc("2026-03-29T01:00"), utc("2026-03-29T02:00")]


def test_the_envelope_takes_the_first_of_a_repeated_local_hour() -> None:
    # F4 (GUARD): the fall-back day, 2026-10-25. Worker 01:00-05:00, opening 02:00-05:00. The
    # clipped envelope's ambiguous 02:00 is the *first* 02:00 (to_utc's documented rule), so both
    # passes of the repeated hour are inside opening hours and all four slots sell.
    found = slots(
        rows(7, ("01:00", "05:00")),
        date(2026, 10, 25),
        opening=rows(7, ("02:00", "05:00")),
        duration=60,
        step=60,
    )
    assert found == [
        utc("2026-10-25T00:00"),
        utc("2026-10-25T01:00"),
        utc("2026-10-25T02:00"),
        utc("2026-10-25T03:00"),
    ]


def test_touching_opening_rows_are_joined_before_clipping() -> None:
    # F6: opening rows joined by day_shifts before clipping, so a worker shift spanning the join
    # (09:00-15:00 against 09:00-12:00 + 12:00-15:00) isn't fragmented at noon.
    found = slots(
        rows(1, ("09:00", "15:00")),
        MONDAY,
        opening=rows(1, ("09:00", "12:00"), ("12:00", "15:00")),
        duration=60,
        step=30,
    )
    assert local(MONDAY, "11:30") in found
    assert len(found) == 11


# 9. day_span (ZIF-101)


# 1. fence: 23h and 25h spans, each end through to_utc directly. Kills start + n * 24h.
def test_day_span_is_23_or_25_hours_around_a_clock_change() -> None:
    assert day_span(date(2026, 3, 29), date(2026, 3, 29), ZONE) == (
        utc("2026-03-28T23:00"),
        utc("2026-03-29T22:00"),
    )
    assert day_span(date(2026, 10, 25), date(2026, 10, 25), ZONE) == (
        utc("2026-10-24T22:00"),
        utc("2026-10-25T23:00"),
    )


# 2. fence: tiling, no gap and no overlap, across a whole year in three zones.
@pytest.mark.parametrize("zone", ["Europe/Amsterdam", "America/Santiago", "America/Havana"])
def test_day_span_tiles_with_no_gap_and_no_overlap(zone: str) -> None:
    day = date(2026, 1, 1)
    while day < date(2026, 12, 31):
        tomorrow = day + timedelta(days=1)
        assert day_span(day, day, zone)[1] == day_span(tomorrow, tomorrow, zone)[0]
        day = tomorrow
    for a, b in [
        (date(2026, 1, 10), date(2026, 1, 12)),
        (date(2026, 3, 27), date(2026, 4, 1)),
        (date(2026, 10, 23), date(2026, 10, 28)),
    ]:
        assert day_span(a, b, zone) == (day_span(a, a, zone)[0], day_span(b, b, zone)[1])


# 3. fence: skipped midnight. Kills "midnight + fixed offset" and using the after-change offset.
def test_day_span_at_a_skipped_midnight_starts_at_the_day_s_first_instant() -> None:
    assert day_span(date(2026, 9, 6), date(2026, 9, 6), "America/Santiago")[0] == utc(
        "2026-09-06T04:00"
    )
    assert day_span(date(2026, 9, 5), date(2026, 9, 5), "America/Santiago")[1] == utc(
        "2026-09-06T04:00"
    )
    havana = day_span(date(2026, 3, 8), date(2026, 3, 8), "America/Havana")
    assert havana[0] == utc("2026-03-08T05:00")
    assert havana[1] - havana[0] == timedelta(hours=23)


# 4. fence: repeated midnight starts at the first occurrence (fold=0). Kills fold=1.
def test_day_span_at_a_repeated_midnight_starts_at_the_first_occurrence() -> None:
    assert day_span(date(2026, 11, 1), date(2026, 11, 1), "America/Havana") == (
        utc("2026-11-01T04:00"),
        utc("2026-11-02T05:00"),
    )


# 5. fence (P2): day_span is not a same-tzinfo subtraction.
def test_day_span_is_not_a_bare_subtraction_of_two_aware_datetimes() -> None:
    start = datetime.combine(date(2026, 10, 25), time(), ZoneInfo(ZONE))
    end = datetime.combine(date(2026, 10, 26), time(), ZoneInfo(ZONE))
    assert end - start == timedelta(hours=24)
    span = day_span(date(2026, 10, 25), date(2026, 10, 25), ZONE)
    assert span[1] - span[0] == timedelta(hours=25)


# 6. fence: a run of days is one interval, never n * 24h or n separate intervals.
def test_day_span_of_a_run_spans_all_of_it() -> None:
    assert day_span(date(2026, 10, 24), date(2026, 10, 26), ZONE) == (
        utc("2026-10-23T22:00"),
        utc("2026-10-26T23:00"),
    )


TWICE_DAILY = [
    (d, s, e)
    for weekday in range(1, 8)
    for d, s, e in rows(weekday, ("00:00", "02:00"), ("22:00", "23:59"))
]


def whole_day_off(day: date) -> list[Interval]:
    return [day_span(day, day, ZONE)]


# 7. fence: fall-back day blocked whole; literal UTC instants, never computed through day_span.
def test_a_whole_day_block_empties_only_its_own_local_day_across_the_fall_back() -> None:
    found = slots(
        TWICE_DAILY,
        date(2026, 10, 24),
        date(2026, 10, 26),
        time_off=whole_day_off(date(2026, 10, 25)),
        duration=60,
        step=60,
    )
    assert utc("2026-10-24T20:00") in found  # 10-24 22:00 local
    assert utc("2026-10-25T23:00") in found  # 10-26 00:00 local
    assert not any(utc("2026-10-24T22:00") <= t < utc("2026-10-25T23:00") for t in found)


# 8. fence: spring-forward day blocked whole; the next day's first slot must survive.
def test_a_whole_day_block_does_not_eat_the_next_day_s_first_hour_across_spring_forward() -> None:
    found = slots(
        TWICE_DAILY,
        date(2026, 3, 28),
        date(2026, 3, 30),
        time_off=whole_day_off(date(2026, 3, 29)),
        duration=60,
        step=60,
    )
    assert utc("2026-03-29T22:00") in found  # 03-30 00:00 local
    assert not any(utc("2026-03-28T23:00") <= t < utc("2026-03-29T22:00") for t in found)


# 9. guard: a whole-day block plus an overlapping partial block; the day after is untouched.
def test_a_whole_day_block_and_an_overlapping_partial_block_leave_no_slots_that_day() -> None:
    found = slots(
        TWICE_DAILY,
        date(2026, 10, 24),
        date(2026, 10, 26),
        time_off=[
            *whole_day_off(date(2026, 10, 25)),
            (local(date(2026, 10, 25), "10:00"), local(date(2026, 10, 25), "11:00")),
        ],
        duration=60,
        step=60,
    )
    assert not any(utc("2026-10-24T22:00") <= t < utc("2026-10-25T23:00") for t in found)
    assert utc("2026-10-25T23:00") in found  # 10-26 00:00 local, untouched
