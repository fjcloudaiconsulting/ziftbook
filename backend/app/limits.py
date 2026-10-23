"""Rate limits counted in Postgres: a fixed window per key, no Redis."""

import hashlib
import ipaddress
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal

# One statement for every key: the row lock serialises concurrent attempts on a key, so the count is
# exact. A window older than :window starts again at 1.
HIT = text("""
INSERT INTO rate_limits AS r (key, window_start, hits)
SELECT k, now(), 1 FROM unnest(CAST(:keys AS text[])) AS k
ON CONFLICT (key) DO UPDATE SET
  hits = CASE WHEN r.window_start > now() - CAST(:window AS interval) THEN r.hits + 1 ELSE 1 END,
  window_start = CASE WHEN r.window_start > now() - CAST(:window AS interval)
                      THEN r.window_start ELSE now() END
RETURNING key, hits
""")

# ponytail: purged opportunistically, 100 rows per attempt, so after a quiet spell old rows (hashed
# emails) outlive the one-day retention; the ZIF-5 sweeper enforces it.
PURGE = text("""
DELETE FROM rate_limits WHERE key IN (
  SELECT key FROM rate_limits WHERE window_start < now() - interval '1 day'
  LIMIT 100 FOR UPDATE SKIP LOCKED)
""")


def _charge(keys: list[str], window: timedelta) -> dict[str, int]:
    """Count one attempt against each key; the hits each key has now, read under its row lock."""
    with SessionLocal.begin() as session:
        rows = session.execute(HIT, {"keys": sorted(keys), "window": window}).all()
        session.execute(PURGE)
    return {key: hits for key, hits in rows}


def hit(limits: dict[str, int], window: timedelta, *, db: Session | None = None) -> bool:
    """Count one attempt against each key; True if any key is now over its limit.

    Commits in its own transaction, before the request does anything else: the attempt counts even
    if the request then fails. Keys are locked in sorted order, so two attempts sharing keys can't
    deadlock whatever order their callers list them in. The purge stays in this transaction: it
    skips locked rows, so it never makes anyone wait, and a second commit would cost every request.

    With db, the attempt is counted in that session's transaction instead, for a caller that already
    holds a connection: a second one checked out meanwhile can deadlock the pool (see
    bookings.account). The rows stay locked until the caller's commit, so only keys nobody else
    contends for belong here (a tenant's own, under its booking lock), and a rollback uncounts the
    attempt. No purge there: the deleted rows would stay locked as long, and every other hit purges.
    """
    if db is not None:
        rows = db.execute(HIT, {"keys": sorted(limits), "window": window}).all()
        counted = {key: hits for key, hits in rows}
    else:
        counted = _charge(list(limits), window)
    return any(hits > limits[key] for key, hits in counted.items())


def charge(key: str, window: timedelta) -> int:
    """Count one attempt against key and return its hits, this one included: exact, so exactly one
    caller sees each number."""
    return _charge([key], window)[key]


COUNT = text("""
SELECT hits FROM rate_limits WHERE key = :key AND window_start > now() - CAST(:window AS interval)
""")


def count(key: str, window: timedelta) -> int:
    """The attempts counted against key in its live window: the window hit() would add to, so a
    window hit() would start again counts 0. Reads only, so it charges nothing."""
    with SessionLocal.begin() as session:
        hits: int | None = session.scalar(COUNT, {"key": key, "window": window})
    return hits or 0


def email_key(action: str, email: str) -> str:
    """The caller normalises the email first. Hashed, so the table holds no addresses."""
    # surrogatepass: a lone surrogate from a JSON body must not turn into a 500 here.
    digest = hashlib.sha256(email.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{action}:email:{digest}"


# Authentication routes key IPv6 by /48: a /48 holder can otherwise rotate 65,536 /64s.
AUTH_V6_PREFIX = 48


def ip_key(action: str, host: str | None, *, v6_prefix: int = 64) -> str:
    """IPv4 per address, IPv6 per /64 by default (one household or server).

    Every address that doesn't parse shares one key, so it is still limited.
    """
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return f"{action}:ip:unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is None:
            return f"{action}:ip:{ipaddress.IPv6Network((int(address), v6_prefix), strict=False)}"
        address = address.ipv4_mapped
    return f"{action}:ip:{address}"
