"""ZIF-55's cancellation rule engine, extended by ZIF-54 for the client's own reschedule.

Pure by construction: this module imports nothing from `app`, opens no transaction, and reads no
clock of its own -- both moments arrive as arguments. Keep it stdlib-only: importing it must not
drag a router in, which is what tests/test_cancellation.py imports it in a subprocess to check, and
it is why this is its own file rather than another hundred lines of app/bookings.py.

ZIF-55 ships no caller. The client cancel and reschedule routes that consume this are ZIF-54's
(app/booking_links.py).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True, slots=True)
class Policy:
    """The SNAPSHOT on the booking row, never the live settings object."""

    free_cancellation_hours: int
    reschedule_cutoff_hours: int
    # ZIF-54. The per-booking snapshot of business_settings.max_reschedules at insert (D8): a
    # settings PUT after the sale must never change how many changes an already-sold booking gets
    # (test 21). Defaulted so ZIF-55's own pure tests, which predate this field, keep constructing
    # a Policy with two arguments.
    max_reschedules: int = 2

    def __post_init__(self) -> None:
        # The columns' CHECK is `BETWEEN 0 AND 720` (hours) and `BETWEEN 0 AND 10`
        # (max_reschedules), but a Policy is built in Python from whatever its caller hands over,
        # and a NEGATIVE value does not fail on its own - it inverts the rule (see decide's own
        # docstring for the hours case). Clocks are a trust boundary (see decide) and so is this.
        if (
            self.free_cancellation_hours < 0
            or self.reschedule_cutoff_hours < 0
            or self.max_reschedules < 0
        ):
            raise ValueError(
                "Policy values cannot be negative (ZIF-55/ZIF-54: the CHECKs start at 0)"
            )


@dataclass(frozen=True, slots=True)
class Decision:
    can_cancel: bool
    can_reschedule: bool
    refund_pct: int
    charge_pct: int
    copy_key: str
    # ZIF-54. How many more times decide() would allow a reschedule after this one, for the page's
    # "N changes left" copy. 0 whenever can_reschedule is False.
    reschedules_left: int = 0


def decide(
    policy: Policy,
    starts_at: datetime,
    now: datetime,
    *,
    reschedule_count: int = 0,
    original_starts_at: datetime | None = None,
    earliest_starts_at: datetime | None = None,
) -> Decision:
    """What a client may still do with a booking that starts at `starts_at`, as of `now`.

    Both moments must be AWARE. A naive one is refused rather than normalised: since Python 3.6
    `naive.astimezone(tz)` does not raise, it assumes the system's local zone, so a naive `now` the
    caller meant as UTC silently shifts the lead by the host's offset and restates the refund (under
    TZ=America/New_York a true 48h lead reads 44h and the refund goes from 100 to 0). This is a
    trust boundary on a money path; the columns' CHECK covers the policy, nothing covers the clocks.
    "Aware" is `utcoffset() is not None`, not `tzinfo is not None`: a tzinfo whose utcoffset()
    returns None makes a datetime that LOOKS aware and behaves naively, and astimezone(UTC) then
    assumes the host zone exactly as it does for tzinfo=None - the same trap, through the gate.

    `charge_pct` is always 0 here (D8). ZIF-7 must NOT read it as the no-show charge: a post-start
    booking answers 0 while the ticket says "fully charged", because a no-show is a MERCHANT act
    keyed off bookings.status = 'no_show', a constant this engine never computes.

    ZIF-54 (D7). `can_cancel`/`can_reschedule`/the "started" check all key off `starts_at`, the
    booking's CURRENT start -- whether it can still be acted on right now. `refund_pct` is
    different: it is anchored to `earliest_starts_at or starts_at`, the earliest start the booking
    EVER had, so a client cannot buy back a refund window by moving a near booking further out and
    then cancelling. A lead computed from that anchor which is <= 0 (the earliest start has already
    passed) means refund 0, not "started" -- "started" is a property of the CURRENT booking alone.

    `can_reschedule` also needs `reschedule_count < policy.max_reschedules` and
    `original_starts_at is not None` (a booking sold before ZIF-54, or one decide() is asked about
    with no snapshot at all, can never be rescheduled).
    """
    if starts_at.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("decide() needs aware datetimes: starts_at and now (ZIF-55 D5)")
    # The first statement, and the only pair anything below compares. CPython short-circuits on
    # `self._tzinfo is other._tzinfo` in both __sub__ and the rich comparisons and never calls
    # utcoffset(), and psycopg3 hands every timestamptz in a row the SAME ZoneInfo object - so a
    # raw comparison is wrong by one hour across a DST change in the launch market.
    lead = starts_at.astimezone(UTC) - now.astimezone(UTC)
    if lead <= timedelta(0):
        # At and after starts_at the booking is the merchant's: a client click must not pull the
        # row out of OCCUPYING and out of the merchant's no_show reach (D7). This is about the
        # CURRENT start only; a moved-out booking whose ORIGINAL start is already in the past is
        # still live and still the client's to act on (test 19).
        return Decision(False, False, 0, 0, "started")
    anchor = (earliest_starts_at or starts_at).astimezone(UTC)
    refund_lead = anchor - now.astimezone(UTC)
    refund_pct = 100 if refund_lead >= timedelta(hours=policy.free_cancellation_hours) else 0
    reschedules_left = max(policy.max_reschedules - reschedule_count, 0)
    can_reschedule = (
        lead >= timedelta(hours=policy.reschedule_cutoff_hours)
        and reschedules_left > 0
        and original_starts_at is not None
    )
    key = ("full_refund" if refund_pct else "no_refund") + (
        "_reschedule" if can_reschedule else "_no_reschedule"
    )
    return Decision(
        True,
        can_reschedule,
        refund_pct,
        0,
        key,
        reschedules_left if can_reschedule else 0,
    )
