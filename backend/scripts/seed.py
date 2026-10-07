"""Fill the running local app with synthetic businesses, people and bookings (make seed).

Stdlib only. Every call goes through the web app's /api proxy, as a browser's would, and the
people it signs up are real accounts: the worker must be running (it sends the emails) and Mailpit
catches them. Refuses anything but this machine: see check_local().
"""

import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
GRID = 15 * 60  # seconds: every start lands on the 15-minute grid the businesses offer


def check_local(*urls: str) -> None:
    """Exit 2 unless every URL points at this machine. The seed writes accounts and one shared
    password, so a mistyped --base must not reach anyone else's server."""
    for url in urls:
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
        if host not in LOCAL_HOSTS:
            print(f"refusing {url}: make seed only talks to this machine", file=sys.stderr)
            sys.exit(2)


def opener() -> urllib.request.OpenerDirector:
    """No proxies, ever: urllib would send these writes to http_proxy even for localhost."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Api:
    """One person's connection: its own cookies, so its own session."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        # The __Host- cookies are Secure, so over http nothing resends them for us.
        self.cookies: dict[str, str] = {}
        self.date: datetime | None = None  # the server's clock, from its last Date header

    def call(self, method: str, path: str, body: object = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if self.cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        try:
            with opener().open(req, timeout=30) as resp:
                status, raw, headers = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as err:
            status, raw, headers = err.code, err.read(), err.headers
        for value in headers.get_all("Set-Cookie") or []:
            name, _, rest = value.partition("=")
            if name.startswith("__Host-"):
                self.cookies[name] = rest.split(";", 1)[0]
        if stamp := headers.get("Date"):
            self.date = parsedate_to_datetime(stamp).astimezone(UTC)
        try:
            return status, (json.loads(raw) if raw else None)
        except ValueError:
            return status, raw.decode(errors="replace")

    def ok(self, method: str, path: str, body: object = None) -> Any:
        status, data = self.call(method, path, body)
        if status >= 300:
            sys.exit(f"{method} {path}: {status} {data}")
        return data


def bucket(start: datetime, now: datetime, zone: str) -> str:
    """Where a start sits relative to now, checked in this order. All instants are UTC."""
    if start <= now:
        return "past"
    tz = ZoneInfo(zone)
    if start.astimezone(tz).date() == now.astimezone(tz).date():
        return "today"
    if start < now + timedelta(hours=24):
        return "within_24h"
    return "further"


def on_grid(instant: datetime, up: bool = False) -> datetime:
    seconds = instant.timestamp()
    return datetime.fromtimestamp((-(-seconds // GRID) if up else seconds // GRID) * GRID, UTC)


@dataclass(frozen=True)
class Plan:
    """The start of each merchant-made booking, by role, and the day the far public ones go to."""

    completed: datetime  # past, ends before now
    no_show: datetime  # the same moment, in the other business
    today: datetime  # starts on the business's own date today
    near: datetime  # in the future but under 24 hours away (no reminder is queued for it)
    far_date: date  # the local date three days out: at least 48 hours from now at any hour


def plan(now: datetime, zone: str, duration: int = 60) -> Plan:
    """UTC arithmetic throughout: an aware local datetime plus a timedelta is wall-clock time and
    drifts over a clock change. The zone only decides which date "today" is."""
    tz = ZoneInfo(zone)
    today = now.astimezone(tz).date()
    midnight = datetime.combine(today, time(), tz).astimezone(UTC)
    past = on_grid(now - timedelta(minutes=15 + duration))
    # ponytail: in the first 75 minutes of the local day there is no room for a past booking
    # today, so it goes back a day and an ahead one carries "today"; if "today" ever needs the
    # past one at any hour (a calendar test at 00:20), this needs a shorter service.
    ahead = on_grid(now + timedelta(minutes=30), up=True)
    if past < midnight:
        past, today_start = on_grid(now - timedelta(hours=26)), ahead
    else:
        today_start = past
    near = today_start if today_start > now else on_grid(now + timedelta(hours=3), up=True)
    return Plan(past, past, today_start, near, today + timedelta(days=3))
