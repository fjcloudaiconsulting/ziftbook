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

# ponytail: purged opportunistically, 100 rows per attempt, so after a quiet spell old rows (hashed
# emails) outlive the one-day retention; the ZIF-5 sweeper enforces it.
PURGE = text("""
DELETE FROM rate_limits WHERE key IN (
  SELECT key FROM rate_limits WHERE window_start < now() - interval '1 day'
  LIMIT 100 FOR UPDATE SKIP LOCKED)
""")


def hit(limits: dict[str, int], window: timedelta) -> bool:
    """Count one attempt against each key; True if any key is now over its limit.

    Commits in its own transaction, before the request does anything else: the attempt counts even
    if the request then fails. Keys are locked in sorted order, so two attempts sharing keys can't
    deadlock whatever order their callers list them in. The purge stays in this transaction: it
    skips locked rows, so it never makes anyone wait, and a second commit would cost every request.
    """
    with SessionLocal.begin() as session:
        rows = session.execute(HIT, {"keys": sorted(limits), "window": window}).all()
        session.execute(PURGE)
    return any(hits > limits[key] for key, hits in rows)


def email_key(action: str, email: str) -> str:
    """The caller normalises the email first. Hashed, so the table holds no addresses."""
    # surrogatepass: a lone surrogate from a JSON body must not turn into a 500 here.
    digest = hashlib.sha256(email.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{action}:email:{digest}"


def ip_key(action: str, host: str | None) -> str:
    """IPv4 per address, IPv6 per /64 (one household or server).

    Every address that doesn't parse shares one key, so it is still limited.
    """
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return f"{action}:ip:unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is None:
            # ponytail: a /48 holder can rotate 65,536 /64s; the per-email limit still caps each
            # account. Widen to /56 if credential stuffing from IPv6 shows up.
            return f"{action}:ip:{ipaddress.IPv6Network((int(address), 64), strict=False)}"
        address = address.ipv4_mapped
    return f"{action}:ip:{address}"
