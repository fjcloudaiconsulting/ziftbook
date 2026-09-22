"""ZIF-55's cancellation rule engine: the boundary table IS the specification of the rule.

See docs/specs/2026-09-22-zif-55-spec.md §4. Pure, no database, no fixtures.
"""

import dataclasses
import subprocess
import sys
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.cancellation import Decision, Policy, decide

# D15, verbatim: the message a reviewer of the diff that changed a row has to read.
SPEC = """
The cancellation boundary table is ZIF-55's specification of the rule, not a sample of it.
Changing a row here restates money for every booking already sold: the thresholds are
snapshotted on the booking, the rule is not. Add a new function beside decide() and point new
bookings at it -- do not edit this one.
"""

P = Policy(free_cancellation_hours=48, reschedule_cutoff_hours=24)
STARTS = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text)


# F1 (T1) - the §4 boundary table, all eleven rows, whole Decision per row.
# Kills: `>` instead of `>=` at the refund threshold (row 2 drops to 0); `>` instead of `>=` at the
# reschedule cutoff (row 4 loses reschedule); `<=` instead of `<` at starts_at (row 7 answers
# no_refund_no_reschedule instead of started); charge_pct = 100 - refund_pct (rows 3-9 answer 100);
# any partial-refund tier (a refund_pct outside {0, 100}); copy_key derived from a three-tier
# ladder, from refund_pct alone, or on a `refund implies reschedule` assumption (row 10).
@pytest.mark.parametrize(
    "policy,starts_at,now,expected",
    [
        (P, STARTS, utc("2026-09-29T11:59:59Z"), (True, True, 100, 0, "full_refund_reschedule")),
        (P, STARTS, utc("2026-09-29T12:00:00Z"), (True, True, 100, 0, "full_refund_reschedule")),
        (P, STARTS, utc("2026-09-29T12:00:01Z"), (True, True, 0, 0, "no_refund_reschedule")),
        (P, STARTS, utc("2026-09-30T12:00:00Z"), (True, True, 0, 0, "no_refund_reschedule")),
        (P, STARTS, utc("2026-09-30T12:00:01Z"), (True, False, 0, 0, "no_refund_no_reschedule")),
        (P, STARTS, utc("2026-10-01T11:59:59Z"), (True, False, 0, 0, "no_refund_no_reschedule")),
        (P, STARTS, utc("2026-10-01T12:00:00Z"), (False, False, 0, 0, "started")),
        (P, STARTS, utc("2026-10-01T12:00:01Z"), (False, False, 0, 0, "started")),
        (P, STARTS, utc("2026-10-02T12:00:00Z"), (False, False, 0, 0, "started")),
        (
            Policy(0, 48),
            STARTS,
            utc("2026-09-30T12:00:00Z"),
            (True, False, 100, 0, "full_refund_no_reschedule"),
        ),
        (
            Policy(0, 0),
            STARTS,
            utc("2026-10-01T11:59:59Z"),
            (True, True, 100, 0, "full_refund_reschedule"),
        ),
    ],
    ids=[
        "48h+1s",
        "exactly 48h",
        "48h-1s",
        "exactly 24h",
        "24h-1s",
        "1s before",
        "exactly starts_at",
        "1s after",
        "a day after",
        "inverted policy at 24h",
        "zero policy 1s before",
    ],
)
def test_the_boundary_table(
    policy: Policy,
    starts_at: datetime,
    now: datetime,
    expected: tuple[bool, bool, int, int, str],
) -> None:
    assert decide(policy, starts_at, now) == Decision(*expected), SPEC


# F2 (T2) - §4 row D-1: both moments wear ONE shared ZoneInfo object, as psycopg3 hands them over.
# Kills: `lead = starts_at - now` without .astimezone(UTC) on both sides - CPython short-circuits
# on `self._tzinfo is other._tzinfo` and skips utcoffset(), so the raw subtraction reads 47h across
# Amsterdam's 2026-10-25 change where the elapsed lead is exactly 48h, and the refund goes to 0.
def test_the_refund_threshold_is_measured_in_elapsed_time_not_wall_clock() -> None:
    ams = ZoneInfo("Europe/Amsterdam")
    starts_at = datetime(2026, 10, 26, 12, 0, tzinfo=ams)
    now = datetime(2026, 10, 24, 13, 0, tzinfo=ams)
    assert starts_at.tzinfo is now.tzinfo  # the trap is set, not assumed
    assert starts_at - now == timedelta(hours=47)  # the wrong answer, spelled out

    assert decide(P, starts_at, now).refund_pct == 100, SPEC


# F3 (T2a) - §4 row D-2: can_cancel across the fold, the direction that loses a live booking.
# Kills: `can_cancel = now < starts_at` on the RAW pair - it reads False for a booking an hour
# away, so a client one hour before their appointment is told it has already started.
def test_a_booking_an_hour_away_across_the_fold_can_still_be_cancelled() -> None:
    ams = ZoneInfo("Europe/Amsterdam")
    starts_at = datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=ams)  # +01
    now = datetime(2026, 10, 25, 2, 30, fold=0, tzinfo=ams)  # +02, sixty minutes EARLIER
    assert starts_at.tzinfo is now.tzinfo
    assert (now < starts_at) is False  # what the raw pair claims

    verdict = decide(P, starts_at, now)

    assert verdict.can_cancel is True, SPEC
    assert verdict.copy_key == "no_refund_no_reschedule", SPEC


# F4 (T2b) - §4 row D-3: the mirror of F3, the direction that loses the merchant's no-show fee.
# Kills: the same raw-pair comparison the other way - it reads True 45 minutes INTO the booking,
# walking a started row out of OCCUPYING and out of the merchant's no_show reach, under a copy_key
# D9 forbids in that state.
def test_a_booking_under_way_across_the_fold_has_started() -> None:
    ams = ZoneInfo("Europe/Amsterdam")
    starts_at = datetime(2026, 10, 25, 2, 45, fold=0, tzinfo=ams)  # +02
    now = datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=ams)  # +01, 45 minutes AFTER the start
    assert starts_at.tzinfo is now.tzinfo
    assert (now < starts_at) is True  # what the raw pair claims

    assert decide(P, starts_at, now) == Decision(False, False, 0, 0, "started"), SPEC


# F5 (T2c) - a naive datetime is refused at the boundary, on either argument.
# Kills: no guard at all, leaning on `.astimezone(UTC)` to surface it. Since Python 3.6 it does
# NOT raise: naive.astimezone(tz) assumes the SYSTEM LOCAL zone. Under TZ=America/New_York a naive
# `now` the caller meant as UTC turns a true 48h lead into 44h and returns refund_pct=0 where 100
# is correct - a wrong refund, silently, on the money path.
@pytest.mark.parametrize(
    "starts_at,now",
    [
        (STARTS, datetime(2026, 9, 29, 12, 0)),
        (datetime(2026, 10, 1, 12, 0), utc("2026-09-29T12:00:00Z")),
    ],
    ids=["naive now", "naive starts_at"],
)
def test_a_naive_datetime_is_refused(starts_at: datetime, now: datetime) -> None:
    with pytest.raises(ValueError, match="aware"):
        decide(P, starts_at, now)


class Unset(tzinfo):
    """Aware-LOOKING and naive: `tzinfo is not None`, `utcoffset()` is None. The shape psycopg3
    would hand over for a timestamp read through a custom tzinfo that declines to answer."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None


# F5b (T2c') - the naive guard is `utcoffset() is None`, not `tzinfo is None`, on both arguments.
# Kills: `if starts_at.tzinfo is None or now.tzinfo is None` - this datetime walks straight
# through it, and astimezone(UTC) then falls back to the HOST zone (CPython
# datetime.astimezone: `myoffset is None` -> `self.replace(tzinfo=None)._local_timezone()`),
# silently restating the lead and the refund. Measured under TZ=Europe/Amsterdam: 12:00 came back
# as 10:00Z. The mutation does not raise at all, so this is red on a UTC host too.
@pytest.mark.parametrize(
    "starts_at,now",
    [
        (STARTS, datetime(2026, 9, 29, 12, 0, tzinfo=Unset())),
        (datetime(2026, 10, 1, 12, 0, tzinfo=Unset()), utc("2026-09-29T12:00:00Z")),
    ],
    ids=["offsetless now", "offsetless starts_at"],
)
def test_a_tzinfo_that_answers_no_offset_is_refused_as_naive(
    starts_at: datetime, now: datetime
) -> None:
    assert starts_at.tzinfo is not None and now.tzinfo is not None  # the trap is set
    assert starts_at.utcoffset() is None or now.utcoffset() is None

    with pytest.raises(ValueError, match="aware"):
        decide(P, starts_at, now)


# F6 (T2d) - a negative threshold is refused at construction. A Policy comes from the booking row,
# whose CHECK is `BETWEEN 0 AND 720`, but nothing stops a caller building one from anywhere else.
# Kills: no lower-bound guard - Policy(-1, -1) makes `lead >= timedelta(hours=-1)` true for every
# future booking, so decide() answers refund_pct=100 for every lead, at any distance.
@pytest.mark.parametrize("hours", [(-1, 24), (48, -1), (-1, -1)], ids=["free", "cutoff", "both"])
def test_a_negative_threshold_is_refused(hours: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="negative"):
        Policy(*hours)


# G1 (T3) - D9's fourth key spelled out: a merchant may set the reschedule cutoff ABOVE the refund
# threshold, and the engine answers that combination. Guard, not fence: §4 row 10 is this case and
# F1 already parametrises it, and the cross-field validator D2 rejected could ship without ever
# turning this red - it constructs Policy directly and never touches pydantic.
def test_an_inverted_policy_refunds_in_full_without_a_reschedule() -> None:
    verdict = decide(Policy(0, 48), STARTS, utc("2026-09-30T12:00:00Z"))

    assert (verdict.refund_pct, verdict.can_reschedule) == (100, False)
    assert verdict.copy_key == "full_refund_no_reschedule"


# G2 (T4) - zero is the settings' lower bound on both keys and it means "always".
def test_a_zero_policy_allows_everything_until_the_moment_it_starts() -> None:
    assert decide(Policy(0, 0), STARTS, utc("2026-10-01T11:59:59Z")) == Decision(
        True, True, 100, 0, "full_refund_reschedule"
    )
    assert decide(Policy(0, 0), STARTS, STARTS) == Decision(False, False, 0, 0, "started")


# G3 (T5) - a verdict and a snapshot are values, not scratch space.
def test_the_dataclasses_are_frozen() -> None:
    verdict = decide(P, STARTS, utc("2026-09-29T12:00:00Z"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.refund_pct = 0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        P.free_cancellation_hours = 1  # type: ignore[misc]


# F7 (T6) - the engine's purity as the BEHAVIOUR it is for: ZIF-7's money path must be able to
# import it without dragging a router, a database engine and the settings registry in behind it.
# A fresh interpreter, because this one has already imported half of `app`.
# Kills: `from app import business_settings` (or any other app module) in app/cancellation.py -
# the subprocess then reports it and everything it pulls in.
#
# Replaces a version that parsed the import statements with `ast` and asserted the resulting set
# equalled {"dataclasses", "datetime"}: an assertion on the TEXT of a data structure, which a
# harmless `import typing` turned red without touching the property it claimed to hold.
def test_importing_the_engine_drags_in_no_other_app_module() -> None:
    found = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.cancellation, sys;"
            " print(sorted(m for m in sys.modules if m == 'app' or m.startswith('app.')))",
        ],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        check=True,
    )

    assert found.stdout.strip() == "['app', 'app.cancellation']"
