"""Rate limits counted in Postgres: a fixed window per key, no Redis."""

import hashlib
import hmac
import ipaddress
from datetime import timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import RateLimitSettings, is_dev_value_on_public_url
from app.db import SessionLocal

MIN_KEY = 32  # characters; a shorter key counts as unset
# SHA-256 of docker-compose.yaml's development ZIF_RATE_LIMIT_KEY.
DEV_KEY_SHA256 = "1a77674c6e51c52ee4710b01c38678b88ab6c007d4e874a962b0caf258430ca5"

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


# Locks the keys in sorted order before REFUND writes them, as HIT does: an UPDATE alone locks in
# scan order, and a refund racing a hit() on the same keys could then deadlock.
LOCK_KEYS = text("""
SELECT key FROM rate_limits WHERE key = ANY(CAST(:keys AS text[])) ORDER BY key FOR UPDATE
""")

REFUND = text("""
UPDATE rate_limits SET hits = hits - 1
WHERE key = ANY(CAST(:keys AS text[])) AND hits > 0
  AND window_start > now() - CAST(:window AS interval)
""")


def refund(keys: list[str], window: timedelta) -> None:
    """Give back one hit on each key, for an attempt hit() counted that is not to count after all.
    Only in the live window, never below zero, in its own transaction like hit()."""
    with SessionLocal.begin() as session:
        session.execute(LOCK_KEYS, {"keys": sorted(keys)})
        session.execute(REFUND, {"keys": sorted(keys), "window": window})


COUNT = text("""
SELECT hits FROM rate_limits WHERE key = :key AND window_start > now() - CAST(:window AS interval)
""")


def count(key: str, window: timedelta) -> int:
    """The attempts counted against key in its live window: the window hit() would add to, so a
    window hit() would start again counts 0. Reads only, so it charges nothing."""
    with SessionLocal.begin() as session:
        hits: int | None = session.scalar(COUNT, {"key": key, "window": window})
    return hits or 0


COUNT_ALL = text("""
SELECT coalesce(sum(hits), 0) FROM rate_limits
WHERE key = ANY(CAST(:keys AS text[])) AND window_start > now() - CAST(:window AS interval)
""")


def count_all(keys: list[str], window: timedelta) -> int:
    """count() of each key, added up, in one statement."""
    with SessionLocal.begin() as session:
        hits: int = session.scalar(COUNT_ALL, {"keys": keys, "window": window})
    return hits


def _pepper() -> bytes | None:
    """ZIF_RATE_LIMIT_KEY, read at call time; None while it is unset, shorter than MIN_KEY, or the
    development key on a public ZIF_APP_URL."""
    key = RateLimitSettings().rate_limit_key.get_secret_value()
    if len(key) < MIN_KEY or is_dev_value_on_public_url(key, DEV_KEY_SHA256):
        return None
    return key.encode()


def key_state() -> Literal["set", "unset", "weak", "dev"]:
    """Never the key itself. "dev": the development key on a public ZIF_APP_URL, ignored as if
    unset."""
    key = RateLimitSettings().rate_limit_key.get_secret_value()
    if not key:
        return "unset"
    if is_dev_value_on_public_url(key, DEV_KEY_SHA256):
        return "dev"
    return "set" if len(key) >= MIN_KEY else "weak"


def _key(action: str, kind: str, value: str) -> str:
    """Every key that embeds an address goes through here. With the pepper set the value is its
    HMAC-SHA256, so the table holds neither addresses nor hashes anyone can check a guess against;
    unset, the value stays as the key's callers built it (the current form)."""
    pepper = _pepper()
    if pepper is None:
        return f"{action}:{kind}:{value}"
    # surrogatepass: a lone surrogate from a JSON body must not turn into a 500 here.
    digest = hmac.new(pepper, value.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()
    return f"{action}:{kind}:{digest}"


def email_key(action: str, email: str) -> str:
    """The caller normalises the email first. Hashed, so the table holds no addresses."""
    pepper = _pepper()
    if pepper is None:
        # surrogatepass: a lone surrogate from a JSON body must not turn into a 500 here.
        return (
            f"{action}:email:{hashlib.sha256(email.encode('utf-8', 'surrogatepass')).hexdigest()}"
        )
    return _key(action, "email", email)


GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def address_for_limits(email: str) -> str:
    """The address as a limit key sees it: lowercase, without a `+tag`, and for Gmail without dots
    and under one domain, so aliases of one mailbox share a key. Used only to build limit keys;
    storage and delivery keep the address as entered."""
    local, _, domain = email.lower().rpartition("@")
    local = local.split("+", 1)[0]
    if domain in GMAIL_DOMAINS:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


# Authentication routes key IPv6 by /48: a /48 holder can otherwise rotate 65,536 /64s.
AUTH_V6_PREFIX = 48


def ip_key(action: str, host: str | None, *, v6_prefix: int = 64) -> str:
    """IPv4 per address, IPv6 per /64 by default (one household or server).

    Every address that doesn't parse shares one key, so it is still limited.
    """
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return _key(action, "ip", "unknown")
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is None:
            return _key(
                action, "ip", str(ipaddress.IPv6Network((int(address), v6_prefix), strict=False))
            )
        address = address.ipv4_mapped
    return _key(action, "ip", str(address))
