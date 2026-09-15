"""Rate limits counted in Postgres: a fixed window per key, no Redis."""

import hashlib
import ipaddress
from datetime import timedelta

from sqlalchemy import text

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

# ponytail: purges on every attempt, a batch at a time; move to the ZIF-5 sweeper.
PURGE = text("""
DELETE FROM rate_limits WHERE key IN (
  SELECT key FROM rate_limits WHERE window_start < now() - interval '1 day'
  LIMIT 100 FOR UPDATE SKIP LOCKED)
""")


def hit(limits: dict[str, int], window: timedelta) -> bool:
    """Count one attempt against each key; True if any key is now over its limit.

    Commits in its own transaction, before the request does anything else: the attempt counts even
    if the request then fails.
    """
    with SessionLocal.begin() as session:
        rows = session.execute(HIT, {"keys": list(limits), "window": window}).all()
        session.execute(PURGE)
    return any(hits > limits[key] for key, hits in rows)


def email_key(action: str, email: str) -> str:
    # Hashed, so the table holds no addresses; still personal data, kept for at most a day.
    return f"{action}:email:{hashlib.sha256(email.encode()).hexdigest()}"


def ip_key(action: str, host: str | None) -> str | None:
    """IPv4 per address, IPv6 per /64 (one household or server). None if there is no address."""
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is None:
            return f"{action}:ip:{ipaddress.IPv6Network((int(address), 64), strict=False)}"
        address = address.ipv4_mapped
    return f"{action}:ip:{address}"
