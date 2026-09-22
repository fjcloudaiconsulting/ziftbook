"""ZIF-55's cancellation rule engine.

Pure by construction: this module imports nothing from `app`, opens no transaction, and reads no
clock of its own -- both moments arrive as arguments. Keep the import list stdlib-only: that is the
property tests/test_cancellation.py asserts, and it is why this is its own file rather than another
hundred lines of app/bookings.py.

ZIF-55 ships no caller. The client cancel and reschedule routes that consume this are ZIF-54's.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True, slots=True)
class Policy:
    """The SNAPSHOT on the booking row, never the live settings object."""

    free_cancellation_hours: int
    reschedule_cutoff_hours: int


@dataclass(frozen=True, slots=True)
class Decision:
    can_cancel: bool
    can_reschedule: bool
    refund_pct: int
    charge_pct: int
    copy_key: str


def decide(policy: Policy, starts_at: datetime, now: datetime) -> Decision:
    """What a client may still do with a booking that starts at `starts_at`, as of `now`.

    Both moments must be AWARE. A naive one is refused rather than normalised: since Python 3.6
    `naive.astimezone(tz)` does not raise, it assumes the system's local zone, so a naive `now` the
    caller meant as UTC silently shifts the lead by the host's offset and restates the refund (under
    TZ=America/New_York a true 48h lead reads 44h and the refund goes from 100 to 0). This is a
    trust boundary on a money path; the columns' CHECK covers the policy, nothing covers the clocks.

    `charge_pct` is always 0 here (D8). ZIF-7 must NOT read it as the no-show charge: a post-start
    booking answers 0 while the ticket says "fully charged", because a no-show is a MERCHANT act
    keyed off bookings.status = 'no_show', a constant this engine never computes.
    """
    if starts_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("decide() needs aware datetimes: starts_at and now (ZIF-55 D5)")
    # The first statement, and the only pair anything below compares. CPython short-circuits on
    # `self._tzinfo is other._tzinfo` in both __sub__ and the rich comparisons and never calls
    # utcoffset(), and psycopg3 hands every timestamptz in a row the SAME ZoneInfo object - so a
    # raw comparison is wrong by one hour across a DST change in the launch market.
    lead = starts_at.astimezone(UTC) - now.astimezone(UTC)
    if lead <= timedelta(0):
        # At and after starts_at the booking is the merchant's: a client click must not pull the
        # row out of OCCUPYING and out of the merchant's no_show reach (D7).
        return Decision(False, False, 0, 0, "started")
    refund_pct = 100 if lead >= timedelta(hours=policy.free_cancellation_hours) else 0
    can_reschedule = lead >= timedelta(hours=policy.reschedule_cutoff_hours)
    key = ("full_refund" if refund_pct else "no_refund") + (
        "_reschedule" if can_reschedule else "_no_reschedule"
    )
    return Decision(True, can_reschedule, refund_pct, 0, key)
