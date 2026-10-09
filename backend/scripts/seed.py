"""Fill the running local app with synthetic businesses, people and bookings (make seed).

Stdlib only. Every call goes through the web app's /api proxy, as a browser's would, and the
people it signs up are real accounts: the worker must be running (it sends the emails) and Mailpit
catches them. Refuses anything but this machine: see check_local().
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
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
            parts = urlsplit(url)
            host, netloc = parts.hostname, parts.netloc
        except ValueError:
            host, netloc = None, ""
        # urllib and urlsplit disagree on where a host with userinfo ends ("evil\@127.0.0.1"):
        # no seed URL needs userinfo, so any "@" in the authority is refused outright.
        if host not in LOCAL_HOSTS or "@" in netloc:
            print(f"refusing {url}: make seed only talks to this machine", file=sys.stderr)
            sys.exit(2)


class LocalRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to this machine: urllib resends the cookies to wherever it points."""

    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> Any:
        try:
            check_local(newurl)
        except SystemExit:
            fp.close()  # the 302's own response, which nothing else will now read
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener() -> urllib.request.OpenerDirector:
    """No proxies, ever: urllib would send these writes to http_proxy even for localhost."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), LocalRedirects)


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

    completed: datetime  # past, ends before now (the no-show in the other business too)
    today: datetime  # starts on the business's own date today
    near: datetime  # in the future but under 24 hours away (no reminder is queued for it)
    far_date: date  # the local date three days out: at least 48 hours from now at any hour


def plan(now: datetime, zone: str, duration: int = 60) -> Plan:
    """UTC arithmetic throughout: an aware local datetime plus a timedelta is wall-clock time and
    drifts over a clock change. The zone only decides which date "today" is."""
    tz = ZoneInfo(zone)
    today = now.astimezone(tz).date()
    midnight = datetime.combine(today, dtime(), tz).astimezone(UTC)
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
    return Plan(past, today_start, near, today + timedelta(days=3))


PASSWORD = "Ziftbook-seed-2026"  # one shared password, printed; only ever on this machine
WEEK = [{"weekday": d, "starts_at": "07:00", "ends_at": "22:00"} for d in range(1, 8)]
SIGN_UP_LINK = r"/sign-up/complete#([A-Za-z0-9_-]+)"
INVITE_LINK = r"/invite#([0-9a-f-]{36}\.[A-Za-z0-9_-]{43})"
BOOKING_LINK = r"/booking#([0-9a-f-]{36}\.[A-Za-z0-9_-]{43})"
# Far public bookings start on a local date three days out or more: 48 hours at the least.
FURTHER = frozenset({"further"})
NEAR = frozenset({"today", "within_24h"})
PAST = frozenset({"past"})
TODAY = frozenset({"today"})
# slot_unavailable retries per run: each attempt counts against the booking IP limit.
MAX_RETRIES = 5


def email_of(name: str) -> str:
    return f"{name}.seed@example.com"


class Mailbox:
    """Mailpit, read through its API."""

    def __init__(self, url: str) -> None:
        self.api = Api(url)

    def messages(self, to: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"query": f'to:"{to}"', "limit": 50})
        found = self.api.ok("GET", f"/api/v1/search?{query}")["messages"]
        # Mailpit matches loosely ("worker.seed@" finds "ownerworker.seed@"): keep the exact one.
        return [m for m in found if to in [r["Address"].lower() for r in m["To"]]]

    def ids(self, to: str) -> set[str]:
        return {m["ID"] for m in self.messages(to)}

    def link(self, to: str, pattern: str, known: set[str]) -> str:
        """The first capture of `pattern` in a message to `to` that was not there before `known`."""
        seen = set(known)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            for message in self.messages(to):
                if message["ID"] in seen:
                    continue
                seen.add(message["ID"])
                body = self.api.ok("GET", f"/api/v1/message/{message['ID']}")["Text"]
                if found := re.search(pattern, body):
                    return found.group(1)
            time.sleep(1)
        sys.exit(
            f"no email to {to} in 60s: is the worker running, or does the account already exist "
            "(make reset)?"
        )


@dataclass
class Business:
    name: str
    country: str
    zone: str
    language: str
    rules: dict[str, Any]  # settings: how it confirms, cancels and moves bookings
    owner: Api
    tenant_id: str = ""
    slug: str = ""
    policy_version: str = ""
    services: list[str] | None = None  # service ids: 60 minutes, then 30


@dataclass
class Booked:
    label: str
    business: Business
    id: str
    status: str  # what the owner should read back
    start: datetime
    expect: frozenset[str]  # the buckets the start may sit in
    client: str
    reschedules: int = 0


class Seed:
    def __init__(self, base: str, mailpit: str) -> None:
        self.base = base.rstrip("/")
        self.mail = Mailbox(mailpit)
        self.retries = 0
        self.now = datetime.now(UTC)
        self.booked: list[Booked] = []
        self.link = ""  # the guest manage URL to print

    def server_now(self) -> datetime:
        probe = Api(self.base)
        probe.ok("GET", "/api/healthz")
        # ponytail: the host clock if a proxy strips the Date header; off by the host's drift only.
        return probe.date or datetime.now(UTC)

    def sign_in(self, who: str) -> tuple[int, Any]:
        return Api(self.base).call(
            "POST", "/api/session", {"email": email_of(who), "password": PASSWORD}
        )

    def refuse(self, who: str, status: int, data: Any) -> None:
        code = data.get("code") if isinstance(data, dict) else data
        hint = " (wait 15 minutes)" if status == 429 else ""
        sys.exit(f"signing in as {who} to see what is seeded: {status} {code}{hint}")

    def wait_for_app(self) -> None:
        deadline = time.monotonic() + 120
        while True:
            try:
                if Api(self.base).call("GET", "/api/healthz")[0] == 200:
                    return
            except OSError:
                pass
            if time.monotonic() > deadline:
                sys.exit(f"{self.base} did not answer in 120s: make up first")
            time.sleep(2)

    # People ----------------------------------------------------------------------------------

    def sign_up(self, who: str, locale: str, business: str, country: str) -> tuple[Api, Any]:
        email, api = email_of(who), Api(self.base)
        known = self.mail.ids(email)
        api.ok("POST", "/api/sign-up", {"email": email, "locale": locale})
        token = self.mail.link(email, SIGN_UP_LINK, known)
        status, data = api.call(
            "POST",
            "/api/sign-up/complete",
            {
                "token": token,
                "password": PASSWORD,
                "name": who.title(),
                "business_name": business,
                "country": country,
            },
        )
        if status != 201:
            sys.exit(f"sign-up/complete for {email}: {status} {data} (make reset?)")
        return api, data

    def invite(self, owner: Api, who: str) -> Any:
        """Invite and accept, from a cookie-less connection: accepting replaces its session."""
        email = email_of(who)
        known = self.mail.ids(email)
        owner.ok("POST", "/api/invites", {"email": email})
        token = self.mail.link(email, INVITE_LINK, known)
        return Api(self.base).ok(
            "POST",
            "/api/invites/accept",
            {"token": token, "password": PASSWORD, "name": who.title()},
        )

    # Businesses ------------------------------------------------------------------------------

    def book_public(
        self,
        biz: Business,
        service: str,
        first: date,
        last: date,
        who: str,
        locale: str,
        guest: Api | None = None,
    ) -> tuple[Api, dict[str, Any], str]:
        """A guest books the first slot offered in first..last, the next if it was just taken."""
        guest = guest or Api(self.base)
        email = email_of(who)
        for attempt in range(MAX_RETRIES + 1):
            query = urllib.parse.urlencode({"from": first.isoformat(), "to": last.isoformat()})
            path = f"/api/public/businesses/{biz.tenant_id}/services/{service}"
            slots = guest.ok("GET", f"{path}/availability?{query}")["slots"]
            if not slots:
                sys.exit(f"no slot for {who} in {biz.name} from {first} to {last}")
            starts = datetime.fromisoformat(slots[min(attempt, len(slots) - 1)]).astimezone(UTC)
            status, data = guest.call(
                "POST",
                f"{path}/bookings",
                {
                    "starts_at": starts.isoformat(),
                    "name": who.title(),
                    "email": email,
                    "locale": locale,
                    "policy_version": biz.policy_version,
                },
            )
            if status == 201:
                return guest, data, email
            if status == 403 and "turnstile_failed" in str(data):
                sys.exit(
                    "booking refused by Turnstile: unset ZIF_TURNSTILE_SECRET, set "
                    "ZIF_TURNSTILE_DISABLED=true and restart"
                )
            if status != 409:
                sys.exit(f"booking {who} at {biz.name}: {status} {data}")
            self.retries += 1
            if self.retries > MAX_RETRIES:
                sys.exit(f"too many taken slots ({self.retries}): run make reset, make seed")
        sys.exit(f"booking {who} at {biz.name}: out of slots")

    def keep(
        self,
        label: str,
        biz: Business,
        booking: dict[str, Any],
        status: str,
        client: str,
        expect: frozenset[str],
    ) -> Booked:
        start = datetime.fromisoformat(booking["starts_at"]).astimezone(UTC)
        entry = Booked(label, biz, booking["id"], status, start, expect, client)
        self.booked.append(entry)
        return entry

    def override(
        self,
        label: str,
        biz: Business,
        service: str,
        member: str,
        start: datetime,
        who: str,
        expect: frozenset[str],
    ) -> Booked:
        """The owner books a client in at any start (a walk-in, or one already past)."""
        booking = biz.owner.ok(
            "POST",
            "/api/bookings",
            {
                "new_client": {"name": who.title(), "email": email_of(who), "locale": biz.language},
                "service_id": service,
                "member_id": member,
                "starts_at": start.isoformat(),
                "override": True,
            },
        )
        return self.keep(label, biz, booking, "confirmed", email_of(who), expect)

    def transition(self, entry: Booked, status: str, message: str | None = None) -> None:
        body: dict[str, str] = {"status": status}
        if message:
            body["message"] = message
        entry.business.owner.ok("PATCH", f"/api/bookings/{entry.id}", body)
        entry.status = status

    # The run ---------------------------------------------------------------------------------

    def run(self) -> None:
        self.wait_for_app()
        status, data = self.sign_in("both")
        if status == 200:
            status, data = self.sign_in("owner")
            # owner.seed is invited before it is made an owner: only the last step finishes it.
            if status == 401 or (status == 200 and data["role"] != "owner"):
                sys.exit("a previous run stopped part-way: make reset, then make seed")
            if status != 200:
                self.refuse("owner.seed", status, data)
            print("already seeded (if an earlier run failed part-way: make reset, then make seed)")
            self.print_logins()
            return
        if status != 401:  # 401: nothing seeded yet
            self.refuse("both.seed", status, data)
        both, both_session = self.sign_up("both", "nl", "Seedwijk Kapsalon", "NL")
        owner_b, owner_b_session = self.sign_up("ownerworker", "pt", "Estudio Seedsol", "BR")
        # pending_ttl_hours 168, the most there is: a day would lapse every seeded pending
        # tomorrow (a week from now they lapse anyway).
        a = Business(
            "Seedwijk Kapsalon",
            "NL",
            "Europe/Amsterdam",
            "nl",
            {
                "auto_confirm": False,
                "pending_ttl_hours": 168,
                "free_cancellation_hours": 24,
                "reschedule_cutoff_hours": 12,
                "max_reschedules": 1,
                "published": True,
                "cancellation_policy_text": "Kosteloos annuleren tot 24 uur voor de afspraak.",
            },
            both,
            tenant_id=both_session["tenant_id"],
        )
        b = Business(
            "Estudio Seedsol",
            "BR",
            "America/Sao_Paulo",
            "pt",
            {
                "auto_confirm": True,
                "free_cancellation_hours": 48,
                "reschedule_cutoff_hours": 24,
                "max_reschedules": 2,
                "published": True,
                "cancellation_policy_text": "Cancelamento gratuito ate 48 horas antes.",
            },
            owner_b,
            tenant_id=owner_b_session["tenant_id"],
        )
        worker = self.invite(both, "worker")
        both_in_b = self.invite(owner_b, "both")
        team_a = [both_session["member_id"], worker["member_id"]]
        team_b = [owner_b_session["member_id"], both_in_b["member_id"]]
        for biz, team, called, services in (
            (
                a,
                team_a,
                ["Bea", "Wim"],
                [
                    ("Knippen", "Haircut", "Corte", 3500, 60),
                    ("Baard trimmen", "Beard trim", "Aparar barba", 1800, 30),
                ],
            ),
            (
                b,
                team_b,
                ["Olivia", "Bea"],
                [
                    ("Ontspanningsmassage", "Relaxing massage", "Massagem relaxante", 6000, 60),
                    ("Wenkbrauwen", "Eyebrow design", "Design de sobrancelhas", 2500, 30),
                ],
            ),
        ):
            self.open_business(biz, team, called, services)
        for biz in (a, b):
            session = biz.owner.ok("GET", "/api/session")
            biz.slug = session["slug"]
            page = Api(self.base).ok("GET", f"/api/public/booking-pages/{biz.slug}")
            biz.policy_version = page["policy_version"]
        self.bookings(a, b, team_a, team_b)
        self.read_back()
        sentinel = self.invite(both, "owner")
        both.ok("PATCH", f"/api/members/{sentinel['member_id']}", {"role": "owner"})
        Api(self.base).ok(
            "POST", "/api/session", {"email": email_of("owner"), "password": PASSWORD}
        )
        self.print_all(a, b)

    def open_business(
        self, biz: Business, team: list[str], called: list[str], services: list[tuple[Any, ...]]
    ) -> None:
        owner, tz = biz.owner, ZoneInfo(biz.zone)
        # Wide opening and working hours: a public slot within a day exists at any hour.
        owner.ok("PUT", "/api/settings", biz.rules)
        owner.ok("PUT", "/api/opening-hours", WEEK)
        for member, display in zip(team, called, strict=True):
            owner.ok("PUT", f"/api/members/{member}/display-name", {"display_name": display})
            owner.ok("PUT", f"/api/members/{member}/working-hours", WEEK)
        today = self.server_now().astimezone(tz).date()
        owner.ok(
            "POST",
            f"/api/members/{team[1]}/time-off",
            {
                "first_day": (today + timedelta(days=30)).isoformat(),
                "last_day": (today + timedelta(days=30)).isoformat(),
                "reason": "Holiday",
            },
        )
        day = today + timedelta(days=20)
        owner.ok(
            "POST",
            f"/api/members/{team[0]}/time-off",
            {
                "starts_at": datetime.combine(day, dtime(12), tz).isoformat(),
                "ends_at": datetime.combine(day, dtime(14), tz).isoformat(),
                "reason": "Dentist",
            },
        )
        biz.services = [
            owner.ok(
                "POST",
                "/api/services",
                {
                    "name": {"en": en, "nl": nl, "pt": pt},
                    "price": {"amount_minor": cents},
                    "duration_minutes": minutes,
                    "worker_ids": team,
                },
            )["id"]
            for nl, en, pt, cents, minutes in services
        ]

    def bookings(self, a: Business, b: Business, team_a: list[str], team_b: list[str]) -> None:
        # Merchant-made ones first, so the public availability already leaves them out. The plan
        # is made from the server's clock right before, not from step 0's.
        self.now = now = self.server_now()
        pa, pb = plan(now, a.zone), plan(now, b.zone)
        long_a, short_a = a.services or ["", ""]
        long_b, short_b = b.services or ["", ""]
        done = self.override("completed", a, long_a, team_a[0], pa.completed, "walkin.one", PAST)
        self.transition(done, "completed")
        gone = self.override("no show", b, long_b, team_b[0], pb.completed, "walkin.two", PAST)
        self.transition(gone, "no_show")
        for biz, p, service, team, tag in (
            (a, pa, long_a, team_a, "a"),
            (b, pb, long_b, team_b, "b"),
        ):
            # Distinct people and workers, so a today and a near booking never share a person.
            if p.today != p.completed:
                self.override(
                    "confirmed today",
                    biz,
                    service,
                    team[1],
                    p.today,
                    f"walkin.today.{tag}",
                    TODAY,
                )
            if p.near != p.today:
                self.override(
                    "confirmed in under 24h",
                    biz,
                    service,
                    team[1],
                    p.near,
                    f"walkin.near.{tag}",
                    NEAR,
                )
        today_a = now.astimezone(ZoneInfo(a.zone)).date()
        today_b = now.astimezone(ZoneInfo(b.zone)).date()

        _, booking, who = self.book_public(
            a, long_a, today_a, today_a + timedelta(days=1), "pending.near", "en"
        )
        self.keep("pending, within 24h", a, booking, "pending", who, NEAR)
        _, booking, who = self.book_public(a, long_a, pa.far_date, pa.far_date, "pending.far", "nl")
        self.keep("pending, further out", a, booking, "pending", who, FURTHER)
        _, booking, who = self.book_public(a, short_a, pa.far_date, pa.far_date, "accepted", "pt")
        self.transition(
            self.keep("confirmed by the owner", a, booking, "pending", who, FURTHER), "confirmed"
        )
        _, booking, who = self.book_public(a, short_a, pa.far_date, pa.far_date, "declined", "en")
        self.transition(
            self.keep("declined", a, booking, "pending", who, FURTHER),
            "declined",
            "Sorry, we are closed.",
        )

        _, booking, who = self.book_public(b, long_b, pb.far_date, pb.far_date, "withdrawn", "pt")
        self.transition(
            self.keep("cancelled by the owner", b, booking, "confirmed", who, FURTHER),
            "cancelled_by_merchant",
        )
        far = (pb.far_date, pb.far_date)
        guest, entry, _ = self.confirmed_with_link(
            b, short_b, far, "walked.away", "nl", "cancelled by the client"
        )
        view = guest.ok("GET", "/api/public/booking-link")
        guest.ok(
            "POST",
            "/api/public/booking-link/cancel",
            {"booking_id": entry.id, "refund_pct": view["engine"]["refund_pct"]},
        )
        entry.status = "cancelled_by_client"
        # A week or more out, so the reschedule is far from the cutoff whatever the hour.
        later = (today_b + timedelta(days=7), today_b + timedelta(days=12))
        guest, entry, _ = self.confirmed_with_link(
            b, long_b, later, "moved.on", "en", "rescheduled"
        )
        slots = guest.ok(
            "GET",
            "/api/public/booking-link/availability?"
            + urllib.parse.urlencode({"from": later[0].isoformat(), "to": later[1].isoformat()}),
        )["slots"]
        target = next(s for s in reversed(slots) if datetime.fromisoformat(s) != entry.start)
        moved = guest.ok(
            "POST",
            "/api/public/booking-link/reschedule",
            {"booking_id": entry.id, "starts_at": target, "reschedule_count": 0},
        )
        entry.start = datetime.fromisoformat(moved["booking"]["starts_at"]).astimezone(UTC)
        entry.reschedules = 1
        # A client with an account: a worker of A booking as a client at B, signed in.
        signed_in = Api(self.base)
        signed_in.ok("POST", "/api/session", {"email": email_of("worker"), "password": PASSWORD})
        _, booking, who = self.book_public(b, short_b, *far, "worker", "en", guest=signed_in)
        self.keep("client with an account", b, booking, "confirmed", who, FURTHER)
        _, _, token = self.confirmed_with_link(b, short_b, far, "guest.link", "pt", "guest link")
        self.link = f"{self.base}/pt/booking#{token}"

    def confirmed_with_link(
        self,
        biz: Business,
        service: str,
        days: tuple[date, date],
        who: str,
        locale: str,
        label: str,
    ) -> tuple[Api, Booked, str]:
        """A guest booking in an auto-confirming business, and a guest connection opened from the
        manage link in the confirmation email. Also the link's token."""
        known = self.mail.ids(email_of(who))
        guest, booking, email = self.book_public(biz, service, *days, who, locale)
        token = self.mail.link(email, BOOKING_LINK, known)
        guest.ok("POST", "/api/public/booking-link/session", {"token": token})
        entry = self.keep(label, biz, booking, "confirmed", email, FURTHER)
        return guest, entry, token

    def read_back(self) -> None:
        """What the owners see for each booking is what was meant to be seeded."""
        wrong: list[str] = []
        for e in self.booked:
            got = e.business.owner.ok("GET", f"/api/bookings/{e.id}")
            start = datetime.fromisoformat(got["starts_at"]).astimezone(UTC)
            seen = bucket(start, self.now, e.business.zone)
            if got["status"] != e.status:
                wrong.append(f"{e.label}: status {got['status']}, expected {e.status}")
            if start != e.start:
                wrong.append(f"{e.label}: starts {start}, expected {e.start}")
            if seen not in e.expect:
                wrong.append(f"{e.label}: {seen}, expected one of {sorted(e.expect)}")
            if got["reschedule_count"] != e.reschedules:
                wrong.append(f"{e.label}: rescheduled {got['reschedule_count']} times")
        if wrong:
            sys.exit("read-back failed:\n  " + "\n  ".join(wrong))
        print(f"read-back: {len(self.booked)} bookings are as seeded")

    # Output ----------------------------------------------------------------------------------

    def print_logins(self) -> None:
        rows = [
            ("both", "Seedwijk Kapsalon owner; Estudio Seedsol worker", "nl"),
            ("owner", "Seedwijk Kapsalon owner (no services)", "none"),
            ("worker", "Seedwijk Kapsalon worker; client at Estudio Seedsol", "none"),
            ("ownerworker", "Estudio Seedsol owner and worker", "pt"),
        ]
        print(f"\nPassword for all: {PASSWORD}")
        for who, role, language in rows:
            print(f"  {email_of(who):<28} {language:<5} {role}")
        print(
            "Accounts made by an invite have no language of their own: open /en, /nl or /pt "
            "in the URL.\nboth.seed's role at Estudio Seedsol is not reachable from the "
            "sign-in until ZIF-81 (it opens the oldest business)."
        )

    def print_all(self, a: Business, b: Business) -> None:
        self.print_logins()
        print("\nBookings (local time in the business):")
        for e in sorted(self.booked, key=lambda e: (e.business.name, e.start)):
            local = e.start.astimezone(ZoneInfo(e.business.zone)).strftime("%a %d-%m %H:%M")
            print(f"  {e.business.name:<22} {local}  {e.status:<22} {e.label:<26} {e.client}")
        print("\nPublic booking pages:")
        for biz in (a, b):
            print(f"  {self.base}/{biz.language}/{biz.slug}")
        print(f"\nGuest manage link (an untouched confirmed booking): {self.link}")
        print("Pending bookings in Seedwijk Kapsalon lapse a week after seeding.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:3000", help="the web app URL")
    parser.add_argument("--mailpit", default="http://127.0.0.1:8025", help="Mailpit URL")
    args = parser.parse_args()
    check_local(args.base, args.mailpit)
    Seed(args.base, args.mailpit).run()


if __name__ == "__main__":
    main()
