"""ZIF-73: platform admins suspend and reactivate a business, and its owners are told."""

import asyncio
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app import admin_auth, admin_businesses, auth
from app.admin_auth import ADMIN_COOKIE
from app.db import tenant_context
from app.jobs import run_once
from app.main import create_app
from app.worker import KINDS
from tests.conftest import (
    People,
    add_membership,
    add_user,
    end_membership,
    failing,
    jobs_as_the_worker,
    member_id,
    migrate_tenant_context,
    save_setting,
    sent_to,
    signed_in,
)
from tests.test_admin_businesses import LIST, admin, an_admin, app, tag  # noqa: F401
from tests.test_admin_routes import admin_routes
from tests.test_admin_second_factor import sign_in, verified_client
from tests.test_bookings_api import while_the_tenant_lock_is_held
from tests.test_bookings_db import insert_booking, seed_service
from tests.test_clients_db import seed_client

SUSPEND_SQL = "SELECT * FROM suspend_business(:h, 'a reason')"
REACTIVATE_SQL = "SELECT * FROM reactivate_business(:h)"


def suspension_of(migrate_engine: Engine, tenant_id: uuid.UUID) -> tuple[Any, Any]:
    with migrate_engine.connect() as conn:
        row = conn.execute(
            text("SELECT suspended_at, suspension_reason FROM tenants WHERE id = :t"),
            {"t": tenant_id},
        ).one()
    return row.suspended_at, row.suspension_reason


def call(engine: Engine, tenant_id: uuid.UUID, sql: str, h: bytes, lock_timeout: str = "5s") -> Any:
    """One definer call as the app role, in a transaction scoped to the business."""
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
        return conn.execute(text(sql), {"h": h}).one()


def cookie_hash(client: Any) -> bytes:
    return auth.hash_token(client.cookies[ADMIN_COOKIE])


# F1. Kills: a column grant on tenants (any app-role path could flip any business, tenants has no
# row-level security).
def test_the_app_role_has_no_grant_on_the_suspension_columns(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        for column in ("suspended_at", "suspension_reason"):
            assert not conn.scalar(
                text("SELECT has_column_privilege('ziftbook_app', 'tenants', :c, 'UPDATE')"),
                {"c": column},
            )


# F2. Kills: trusting the caller (a random hash, a half session), and live_admin_user without the
# freshness test (a factor 16 minutes old).
def test_the_functions_refuse_a_session_without_a_fresh_second_factor(
    app: FastAPI,  # noqa: F811
    people: People,
    app_engine: Engine,
    migrate_engine: Engine,
    an_admin: uuid.UUID,  # noqa: F811
) -> None:
    v = verified_client(app, migrate_engine, an_admin)  # enrolling ends other sessions: first
    half = sign_in(app, an_admin)
    h = cookie_hash(v.client)

    def passed_ago(minutes: int) -> None:
        with migrate_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE admin_sessions SET second_factor_at = now() - make_interval(mins => :m)"
                    " WHERE id_hash = :h"
                ),
                {"m": minutes, "h": h},
            )

    passed_ago(16)
    for hash_ in (auth.hash_token("nobody"), cookie_hash(half), h):
        for sql in (SUSPEND_SQL, REACTIVATE_SQL):
            assert tuple(call(app_engine, people.a, sql, hash_)) == ("stale", None)
    assert suspension_of(migrate_engine, people.a) == (None, None)
    passed_ago(14)
    # a half session of an admin whose other session is fresh is still no session with a factor
    for sql in (SUSPEND_SQL, REACTIVATE_SQL):
        assert tuple(call(app_engine, people.a, sql, cookie_hash(half))) == ("stale", None)
    # positive control: the fresh session itself, within the window
    assert call(app_engine, people.a, SUSPEND_SQL, h).outcome == "suspended"


# F3. Kills: no advisory lock, or the wrong key (it must be bookings.LOCK_KEY on the business).
def test_a_suspension_waits_for_the_business_booking_lock(
    people: People,
    app_engine: Engine,
    migrate_engine: Engine,
    an_admin: uuid.UUID,  # noqa: F811
    app: FastAPI,  # noqa: F811
) -> None:
    fresh = verified_client(app, migrate_engine, an_admin)
    h = cookie_hash(fresh.client)
    with app_engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_xact_lock(51, hashtext(:t))"), {"t": str(people.a)})
        with pytest.raises(DBAPIError) as busy:
            call(app_engine, people.a, SUSPEND_SQL, h, lock_timeout="200ms")
        assert getattr(busy.value.orig, "sqlstate", None) == "55P03"
        assert suspension_of(migrate_engine, people.a) == (None, None)
        # positive control: another business's lock is not held
        assert (
            call(app_engine, people.b, SUSPEND_SQL, h, lock_timeout="200ms").outcome == "suspended"
        )
        holder.rollback()
    assert call(app_engine, people.a, SUSPEND_SQL, h).outcome == "suspended"


URL = "/api/admin/businesses/{}/{}"


def suspension(tenant_id: uuid.UUID) -> str:
    return URL.format(tenant_id, "suspension")


def reactivation(tenant_id: uuid.UUID) -> str:
    return URL.format(tenant_id, "reactivation")


@pytest.fixture(autouse=True)
def clean_outbox(people: People, migrate_engine: Engine) -> Iterator[None]:
    yield
    # As the migrate role: the app role has no DELETE on email_outbox (ZB-10).
    for tenant_id in (people.a, people.b):
        with migrate_tenant_context(migrate_engine, tenant_id) as conn:
            conn.execute(text("DELETE FROM email_outbox"))


@dataclass(frozen=True)
class Team:
    owners: dict[str, uuid.UUID]  # by locale: the business's own language when None
    gone: uuid.UUID  # a removed owner


@pytest.fixture
def team(people: People, migrate_engine: Engine) -> Team:
    """Business a: owners both (nl), en and none; a worker (only_a); a removed owner."""
    owners = {"nl": people.both}
    for locale in ("en", None):
        user = add_user(migrate_engine, locale=locale)
        add_membership(people.a, user, "owner")
        owners[str(locale)] = user
    gone = add_user(migrate_engine)
    add_membership(people.a, gone, "owner")
    end_membership(people.a, gone)
    return Team(owners, gone)


def jobs_of(migrate_engine: Engine, tenant_id: uuid.UUID, template: str) -> list[dict[str, Any]]:
    with migrate_engine.connect() as conn:
        return [
            row.payload
            for row in conn.execute(
                text(
                    "SELECT payload FROM jobs WHERE kind = 'email.send' AND tenant_id = :t"
                    " AND payload->>'template' = :template ORDER BY id"
                ),
                {"t": tenant_id, "template": template},
            )
        ]


def audit_of(migrate_engine: Engine, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    with migrate_engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text(
                    "SELECT action, kind, reason, target, actor_user_id FROM platform_audit_events"
                    " WHERE tenant_id = :t AND kind = 'write' ORDER BY id"
                ),
                {"t": tenant_id},
            ).mappings()
        ]


def run_jobs() -> None:
    async def until_idle() -> None:
        while await run_once(KINDS):
            pass

    with jobs_as_the_worker():
        asyncio.run(until_idle())


# Kills: a function that reports success for a business that is not there (a wrong app.tenant_id).
def test_the_functions_say_not_found_for_an_unknown_business(
    app: FastAPI,  # noqa: F811
    app_engine: Engine,
    migrate_engine: Engine,
    an_admin: uuid.UUID,  # noqa: F811
) -> None:
    h = cookie_hash(verified_client(app, migrate_engine, an_admin).client)
    for sql in (SUSPEND_SQL, REACTIVATE_SQL):
        assert tuple(call(app_engine, uuid.uuid4(), sql, h)) == ("not_found", None)


# Kills: the route waiting on the booking lock without the 503 (or writing before it).
def test_a_suspension_while_the_business_lock_is_held_is_busy(
    admin: TestClient,  # noqa: F811
    people: People,
    team: Team,
    migrate_engine: Engine,
) -> None:
    busy = while_the_tenant_lock_is_held(
        admin, people.a, 10, lambda c: c.post(suspension(people.a), json={"reason": "x"})
    )

    assert (busy.status_code, busy.json()) == (503, {"code": "busy"})
    assert suspension_of(migrate_engine, people.a) == (None, None)
    assert audit_of(migrate_engine, people.a) == []


# F4. Kills: an UPDATE with no tenant filter (b suspended too), mailing every member (worker,
# removed owner), an audit row missing or doubled.
def test_suspending_a_business_audits_it_and_tells_its_live_owners(
    admin: TestClient,  # noqa: F811
    people: People,
    team: Team,
    migrate_engine: Engine,
    an_admin: uuid.UUID,  # noqa: F811
) -> None:
    done = admin.post(suspension(people.a), json={"reason": "  chargebacks  "})

    assert done.status_code == 204, done.content
    at, reason = suspension_of(migrate_engine, people.a)
    assert at is not None and reason == "chargebacks"
    assert suspension_of(migrate_engine, people.b) == (None, None)
    assert audit_of(migrate_engine, people.a) == [
        {
            "action": "business_suspended",
            "kind": "write",
            "reason": "chargebacks",
            "target": "business",
            "actor_user_id": an_admin,
        }
    ]
    payloads = jobs_of(migrate_engine, people.a, "business_suspended")
    assert sorted(p["recipient_id"] for p in payloads) == sorted(map(str, team.owners.values()))


# F5. Kills: an unconditional UPDATE (the second reason would overwrite the first), auditing or
# mailing a no-op.
def test_a_repeated_suspension_or_a_reactivation_of_an_active_business_is_a_conflict(
    admin: TestClient,  # noqa: F811
    people: People,
    team: Team,
    migrate_engine: Engine,
) -> None:
    assert admin.post(suspension(people.a), json={"reason": "first"}).status_code == 204
    before = suspension_of(migrate_engine, people.a)

    again = admin.post(suspension(people.a), json={"reason": "second"})
    active = admin.post(reactivation(people.b), json={"reason": "nothing to lift"})

    assert (again.status_code, again.json()) == (409, {"code": "already_suspended"})
    assert (active.status_code, active.json()) == (409, {"code": "not_suspended"})
    assert suspension_of(migrate_engine, people.a) == before
    assert len(audit_of(migrate_engine, people.a)) == 1
    assert audit_of(migrate_engine, people.b) == []
    assert len(jobs_of(migrate_engine, people.a, "business_suspended")) == len(team.owners)
    assert jobs_of(migrate_engine, people.b, "business_reactivated") == []


# F6. Kills: the function in a transaction of its own (a failed enqueue would leave the business
# suspended and no one told, or the audit row without the change).
def test_a_failed_enqueue_rolls_the_suspension_and_its_audit_row_back(
    admin: TestClient,  # noqa: F811
    people: People,
    team: Team,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as broken:
        failing(broken, admin_businesses, "enqueue")
        failed = admin.post(suspension(people.a), json={"reason": "x"})
    assert failed.status_code == 500
    assert suspension_of(migrate_engine, people.a) == (None, None)
    assert audit_of(migrate_engine, people.a) == []
    # positive control: with enqueue back, the same request works
    assert admin.post(suspension(people.a), json={"reason": "x"}).status_code == 204
    assert len(audit_of(migrate_engine, people.a)) == 1


# F7. Kills: either write on admin_router (it would answer 'stale' from the definer all the same,
# so only the router tells).
def test_both_writes_are_on_the_recent_second_factor_router() -> None:
    mine = {
        "/api/admin/businesses/{tenant_id}/suspension",
        "/api/admin/businesses/{tenant_id}/reactivation",
    }
    on_write = {
        route.path for route in admin_auth.admin_write_router.routes if hasattr(route, "path")
    }
    assert mine <= on_write
    on_read = {route.path for route in admin_auth.admin_router.routes if hasattr(route, "path")}
    assert not mine & on_read
    assert {("POST", path) for path in mine} <= admin_routes(create_app())


# F8. Kills: a raw str (a 500 from the CHECK's 500 characters or from a NUL, a blank reason stored).
@pytest.mark.parametrize("reason", ["", "   ", "\t\n", "x" * 501, "a\x00b"])
def test_a_bad_reason_is_a_422_and_writes_nothing(
    admin: TestClient,  # noqa: F811
    people: People,
    team: Team,
    migrate_engine: Engine,
    reason: str,
) -> None:
    for url in (suspension(people.a), reactivation(people.a)):
        refused = admin.post(url, json={"reason": reason})
        assert refused.status_code == 422, refused.content
    extra = admin.post(suspension(people.a), json={"reason": "ok", "tenant_id": str(people.b)})
    assert extra.status_code == 422
    assert suspension_of(migrate_engine, people.a) == (None, None)
    assert audit_of(migrate_engine, people.a) == []


def test_a_reason_of_500_is_kept_stripped(
    admin: TestClient,  # noqa: F811
    people: People,
    team: Team,
    migrate_engine: Engine,
) -> None:
    done = admin.post(suspension(people.a), json={"reason": " " + "x" * 500 + "\n"})
    assert done.status_code == 204, done.content
    assert suspension_of(migrate_engine, people.a)[1] == "x" * 500
    assert admin.post(suspension(uuid.uuid4()), json={"reason": "x"}).status_code == 404


def seed_bookings(
    tenant_id: uuid.UUID, user_id: uuid.UUID, offsets: list[tuple[str, int, int]]
) -> list[Any]:
    """Bookings of one worker as (status, starts, ends) minutes from now, one per offset."""
    service = seed_service(tenant_id)
    client = seed_client(tenant_id, name="Zed Client", email=f"{uuid.uuid4().hex}@example.com")
    worker = member_id(tenant_id, user_id)
    now = datetime.now(UTC)
    with tenant_context(tenant_id) as session:
        return [
            insert_booking(
                session,
                client_id=client,
                worker_id=worker,
                service_id=service,
                status=status,
                starts_at=now + timedelta(minutes=start),
                ends_at=now + timedelta(minutes=end),
                expires_at=now + timedelta(days=1) if status == "pending" else None,
            )
            for status, start, end in offsets
        ]


# F9. Kills: client fields or a booking id in the model, a missing status filter (pending,
# cancelled), another business's bookings, ended bookings, an in-progress one left out, a payment
# join that adds rows or leaks.
def test_a_suspended_business_lists_its_upcoming_confirmed_bookings_only(
    admin: TestClient,  # noqa: F811
    people: People,
    migrate_engine: Engine,
) -> None:
    day = 24 * 60
    ids = seed_bookings(
        people.a,
        people.both,
        [
            ("confirmed", 2 * day, 2 * day + 30),  # future, paid
            ("confirmed", -10, 20),  # in progress
            ("confirmed", -180, -150),  # over
            ("pending", 3 * day, 3 * day + 30),
            ("cancelled_by_merchant", 4 * day, 4 * day + 30),
        ],
    )
    seed_bookings(people.b, people.both, [("confirmed", 2 * day, 2 * day + 30)])
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        conn.execute(
            text(
                "INSERT INTO booking_payments (tenant_id, booking_id, kind, intent_id,"
                " amount_minor, currency, fee_mode, status, settled_at)"
                " VALUES (current_setting('app.tenant_id')::uuid, :b, 'pay', :i, 2500, 'EUR',"
                " 'business_pays', 'paid', now())"
            ),
            {"b": ids[0], "i": "pi_" + uuid.uuid4().hex[:12]},
        )
    assert admin.get(f"{LIST}/{people.a}").json()["upcoming"] is None  # active: nothing listed
    assert admin.get(f"{LIST}/{people.a}").json()["upcoming_total"] == 0
    assert admin.post(suspension(people.a), json={"reason": "why"}).status_code == 204

    body = admin.get(f"{LIST}/{people.a}").json()

    assert body["suspension_reason"] == "why" and body["suspended_at"] is not None
    assert body["upcoming_total"] == 2 and len(body["upcoming"]) == 2
    in_progress, future = body["upcoming"]
    for row in (in_progress, future):
        assert set(row) == {
            "starts_at",
            "ends_at",
            "service_name",
            "worker_display_name",
            "payment",
        }
    assert in_progress["payment"] is None
    assert future["service_name"] == {"en": "Cut"}
    assert future["payment"] == {
        "kind": "pay",
        "status": "paid",
        "amount_minor": 2500,
        "currency": "EUR",
        "refunded_minor": 0,
        "refund_failed": False,
    }
    assert all(str(booking) not in json.dumps(body) for booking in ids)


# F9. Kills: no cap, or a total that is the page's length.
def test_the_upcoming_list_is_capped_with_its_true_total(
    admin: TestClient,  # noqa: F811
    people: People,
) -> None:
    seed_bookings(
        people.a, people.both, [("confirmed", 60 * (n + 1), 60 * (n + 1) + 30) for n in range(105)]
    )
    assert admin.post(suspension(people.a), json={"reason": "why"}).status_code == 204

    body = admin.get(f"{LIST}/{people.a}").json()

    assert len(body["upcoming"]) == admin_businesses.UPCOMING_LIMIT == 100
    assert body["upcoming_total"] == 105


# F10. Kills: the reason in the jobs table (global, every tenant's, kept after the business's data).
def test_a_job_payload_holds_no_reason(
    admin: TestClient,  # noqa: F811
    people: People,
    team: Team,
    migrate_engine: Engine,
) -> None:
    admin.post(suspension(people.a), json={"reason": "sentinel-reason-10"})
    admin.post(reactivation(people.a), json={"reason": "sentinel-lift-10"})

    for template in ("business_suspended", "business_reactivated"):
        payloads = jobs_of(migrate_engine, people.a, template)
        assert len(payloads) == len(team.owners)
        for payload in payloads:
            assert set(payload) - {"traceparent"} == {"recipient_id", "template", "suspended_at"}
            assert "sentinel" not in json.dumps(payload)
            assert datetime.fromisoformat(payload["suspended_at"]).tzinfo is not None


# F11. Kills: an unescaped reason in the HTML part; the wrong locale (the owner's own, else the
# business's language).
def test_the_suspension_email_escapes_the_reason_and_speaks_the_owners_language(
    admin: TestClient,  # noqa: F811
    people: People,
    team: Team,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_setting(people.a, "language", "pt")
    assert admin.post(suspension(people.a), json={"reason": "<b>x</b>"}).status_code == 204

    run_jobs()

    subjects = {
        "nl": "Je bedrijf is opgeschort op ziftbook",
        "en": "Your business is suspended on ziftbook",
        "None": "Seu negócio está suspenso no ziftbook",  # the business's language
    }
    for locale, user in team.owners.items():
        (sent,) = sent_to(f"{user}@example.com")
        assert sent["Subject"] == subjects[locale]
        assert "&lt;b&gt;x&lt;/b&gt;" in sent["Html"] and "<b>x</b>" not in sent["Html"]
        assert "<b>x</b>" in sent["Text"]
    assert sent_to(f"{team.gone}@example.com") == []


# F12. Kills: a check that only asks whether the business is still suspended (the first mail would
# go, with the second reason).
def test_only_the_current_suspensions_email_is_sent(
    admin: TestClient,  # noqa: F811
    people: People,
    team: Team,
) -> None:
    for url, reason in (
        (suspension(people.a), "reason-one"),
        (reactivation(people.a), "lifted"),
        (suspension(people.a), "reason-two"),
    ):
        assert admin.post(url, json={"reason": reason}).status_code == 204

    run_jobs()

    for user in team.owners.values():
        (sent,) = sent_to(f"{user}@example.com")
        assert "reason-two" in sent["Text"] and "reason-one" not in sent["Text"]
        assert "active again" not in sent["Subject"]


# F13. Kills: a reactivation that sends nothing, leaves the reason, or ends the members' sessions.
def test_reactivating_clears_the_suspension_and_tells_the_owners(
    admin: TestClient,  # noqa: F811
    people: People,
    team: Team,
    migrate_engine: Engine,
    an_admin: uuid.UUID,  # noqa: F811
    tag: str,  # noqa: F811
    app: FastAPI,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZIF_APP_URL", "https://app.example.test/")
    owner = signed_in(app, people.a, people.both)
    assert owner.get("/api/session").status_code == 200

    def listed() -> Any:
        rows = admin.get(LIST, params={"q": tag}).json()["businesses"]
        return next(r["suspended_at"] for r in rows if r["id"] == str(people.a))

    assert admin.post(suspension(people.a), json={"reason": "why"}).status_code == 204
    assert owner.get("/api/session").status_code == 401 and listed() is not None

    done = admin.post(reactivation(people.a), json={"reason": "paid up"})

    assert done.status_code == 204, done.content
    assert suspension_of(migrate_engine, people.a) == (None, None)
    assert [(e["action"], e["reason"]) for e in audit_of(migrate_engine, people.a)] == [
        ("business_suspended", "why"),
        ("business_reactivated", "paid up"),
    ]
    assert len(jobs_of(migrate_engine, people.a, "business_reactivated")) == len(team.owners)
    assert owner.get("/api/session").status_code == 200 and listed() is None
    run_jobs()
    for locale, user in team.owners.items():
        (sent,) = sent_to(f"{user}@example.com")  # the suspension's own email was dropped
        language = locale if locale != "None" else "en"
        assert f"https://app.example.test/{language}/settings" in sent["Text"]
        assert "active again" in sent["Subject"] or "actief" in sent["Subject"]
    assert sent_to(f"{team.gone}@example.com") == []


# F14. Kills: logging the reason, in the route or in the mail job (an admin's words about a business
# are not for the log).
def test_the_reason_never_reaches_a_log(
    admin: TestClient,  # noqa: F811
    people: People,
    team: Team,
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    sentinel = f"zif-reason-{uuid.uuid4().hex}"
    assert admin.post(suspension(people.a), json={"reason": sentinel}).status_code == 204
    run_jobs()

    for user in team.owners.values():  # positive control: the mail was composed with the reason
        (sent,) = sent_to(f"{user}@example.com")
        assert sentinel in sent["Text"]
    lines = [json.dumps(line) for line in log_lines()]
    assert any("business suspended" in line for line in lines)
    assert not [line for line in lines if sentinel in line]


# Kills: a multi-line reason collapsed or cut at its first line in either part of the email.
def test_a_two_line_reason_is_quoted_whole(
    admin: TestClient,  # noqa: F811
    people: People,
    team: Team,
) -> None:
    assert (
        admin.post(suspension(people.a), json={"reason": "first line\nsecond line"}).status_code
        == 204
    )
    run_jobs()

    (sent,) = sent_to(f"{people.both}@example.com")
    assert "> first line\n> second line" in sent["Text"]
    assert "first line<br>second line" in sent["Html"]
