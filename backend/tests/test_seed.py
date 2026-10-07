"""The fences around scripts/seed.py: what it must never talk to, and its time arithmetic.

Pure: no database, no network beyond local stub servers. Run alone with
`uv run pytest --noconftest tests/test_seed.py` (the conftest otherwise builds a test database).

The script itself is exercised live (make seed against a real stack); these are the pieces whose
mistakes would not show there: a request that leaves the machine, and a plan that is wrong only at
certain hours of the day.
"""

import http.server
import importlib
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from scripts import seed

AMSTERDAM = "Europe/Amsterdam"
SAO_PAULO = "America/Sao_Paulo"
HOUR = timedelta(hours=1)


def local(zone: str, year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A wall-clock time in `zone` as a UTC instant (the first one, if it happens twice)."""
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(zone)).astimezone(UTC)


def local_date(instant: datetime, zone: str) -> date:
    return instant.astimezone(ZoneInfo(zone)).date()


def test_guard_accepts_only_local_urls() -> None:  # F1a
    seed.check_local("http://127.0.0.1:3000", "http://localhost:8025")
    seed.check_local("http://[::1]:3000", "http://127.0.0.1:8025")
    for base, mailpit in [
        ("http://example.com", "http://127.0.0.1:8025"),
        ("http://10.0.0.5:3000", "http://127.0.0.1:8025"),
        ("http://localhost.evil.com", "http://127.0.0.1:8025"),
        ("http://localhost@evil.com", "http://127.0.0.1:8025"),
        ("http://evil.example\\@127.0.0.1:3000", "http://127.0.0.1:8025"),
        ("http://127.0.0.1:3000", "http://mail.example.com:8025"),
    ]:
        with pytest.raises(SystemExit) as refused:
            seed.check_local(base, mailpit)
        assert refused.value.code == 2, (base, mailpit)


class Counter(http.server.BaseHTTPRequestHandler):
    hits = 0
    location: str | None = None  # answer 302 to here instead of 200

    def do_GET(self) -> None:
        type(self).hits += 1
        self.send_response(302 if self.location else 200)
        if self.location:
            self.send_header("Location", self.location)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: Any) -> None:
        pass


def stub() -> tuple[http.server.HTTPServer, type[Counter]]:
    handler = type("Handler", (Counter,), {"hits": 0})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler


@pytest.fixture
def stubs() -> Iterator[
    tuple[http.server.HTTPServer, type[Counter], http.server.HTTPServer, type[Counter]]
]:
    proxy, proxy_hits = stub()
    target, target_hits = stub()
    yield proxy, proxy_hits, target, target_hits
    for server in (proxy, target):
        server.shutdown()
        server.server_close()


def test_requests_ignore_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
    stubs: tuple[http.server.HTTPServer, type[Counter], http.server.HTTPServer, type[Counter]],
) -> None:  # F1b
    proxy, proxy_hits, target, target_hits = stubs
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.server_port}")
    reloaded = importlib.reload(seed)  # as if the variables had been set before the script started
    status, body = reloaded.Api(f"http://127.0.0.1:{target.server_port}").call("GET", "/x")
    assert (status, body) == (200, {})
    assert (proxy_hits.hits, target_hits.hits) == (0, 1)
    assert "urlopen(" not in Path(seed.__file__ or "").read_text()


def test_requests_refuse_redirects_off_the_machine(
    stubs: tuple[http.server.HTTPServer, type[Counter], http.server.HTTPServer, type[Counter]],
) -> None:  # F1c
    _, _, target, target_hits = stubs
    target_hits.location = "http://example.invalid/x"
    with pytest.raises(SystemExit) as refused:
        seed.Api(f"http://127.0.0.1:{target.server_port}").call("GET", "/x")
    assert refused.value.code == 2
    assert target_hits.hits == 1


def nows() -> list[tuple[str, datetime]]:
    """Moments where the zone's date differs from UTC's, and the edges of its day."""
    return [
        (AMSTERDAM, local(AMSTERDAM, 2026, 6, 15, 0, 30)),
        (SAO_PAULO, local(SAO_PAULO, 2026, 6, 15, 22, 0)),
        (AMSTERDAM, local(AMSTERDAM, 2026, 6, 15, 1, 30)),
        (SAO_PAULO, local(SAO_PAULO, 2026, 6, 15, 1, 30)),
        (AMSTERDAM, local(AMSTERDAM, 2026, 6, 15, 23, 50)),
        (SAO_PAULO, local(SAO_PAULO, 2026, 6, 15, 23, 50)),
    ]


def past_ends(plan: seed.Plan) -> list[datetime]:
    return [plan.completed + HOUR, plan.no_show + HOUR]


def test_today_role_is_the_zones_today() -> None:  # F2
    for zone, now in [nows()[0], nows()[1]]:
        plan = seed.plan(now, zone)
        assert local_date(plan.today, zone) == local_date(now, zone), (zone, now)
        assert all(end <= now for end in past_ends(plan)), (zone, now)


def edges() -> list[tuple[str, datetime]]:
    """The first minutes of the local day (no room for a past booking today) and its last."""
    return [
        (zone, local(zone, 2026, 6, 15, hour, minute))
        for zone in (AMSTERDAM, SAO_PAULO)
        for hour, minute in [(0, 30), (1, 0), (1, 30), (23, 50)]
    ]


def test_today_role_at_the_edges_of_the_day() -> None:  # F3
    for zone, now in edges():
        plan = seed.plan(now, zone)
        assert local_date(plan.today, zone) == local_date(now, zone), (zone, now)
        assert all(end <= now for end in past_ends(plan)), (zone, now)
        if local_date(plan.completed, zone) != local_date(now, zone):
            # A past booking on another day never carries "today": an ahead one does.
            assert plan.today - now >= timedelta(minutes=30), (zone, now)
        else:
            assert plan.today == plan.completed, (zone, now)


def test_near_role_is_ahead_but_under_a_day() -> None:  # F4
    for zone, now in nows():
        plan = seed.plan(now, zone)
        assert timedelta(minutes=30) <= plan.near - now < timedelta(hours=24), (zone, now)
        assert plan.far_date == local_date(now, zone) + timedelta(days=3)


def test_roles_hold_across_clock_changes() -> None:  # F5
    for now in [
        local(AMSTERDAM, 2026, 10, 25, 1, 50),
        local(AMSTERDAM, 2026, 3, 29, 1, 50),
        local(AMSTERDAM, 2026, 10, 25, 0, 30),
        local(AMSTERDAM, 2026, 3, 29, 0, 30),
        local(AMSTERDAM, 2026, 10, 24, 23, 50),
    ]:
        plan = seed.plan(now, AMSTERDAM)
        assert timedelta(minutes=30) <= plan.near - now < timedelta(hours=24), now
        assert local_date(plan.today, AMSTERDAM) == local_date(now, AMSTERDAM), now
        assert all(end <= now for end in past_ends(plan)), now


def test_bucket_checks_in_order() -> None:  # F6
    zone = AMSTERDAM
    morning = local(zone, 2026, 6, 15, 10, 0)
    assert seed.bucket(morning, morning, zone) == "past"
    assert seed.bucket(morning - timedelta(minutes=1), morning, zone) == "past"
    assert seed.bucket(local(zone, 2026, 6, 15, 15, 0), morning, zone) == "today"
    night = local(zone, 2026, 6, 15, 23, 0)
    assert seed.bucket(local(zone, 2026, 6, 16, 1, 0), night, zone) == "within_24h"
    assert seed.bucket(night + timedelta(hours=30), night, zone) == "further"
