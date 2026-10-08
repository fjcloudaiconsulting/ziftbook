"""ZIF-117: a guest's booking exists only after they confirm the emailed link.

POST .../services/{s}/holds holds the time for 15 minutes and emails a link that lives 24 hours;
POST /api/public/booking-hold reads it (writes nothing); POST /api/public/booking-hold/confirm books
it through the same core as POST .../bookings. Like tests/test_bookings_api.py this file never
monkeypatches availability.now: the write path takes its clock from Postgres, so every day here is
two real days out and the hold's clocks are moved by UPDATE, as the migrate role.
"""

import asyncio
import base64
import hashlib
import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from opentelemetry.sdk.trace import ReadableSpan
from sqlalchemy import Engine, event, text

from app import bookings, limits, mail
from app.db import tenant_context
from app.jobs import Job, run_once
from app.main import create_app
from app.worker import KINDS
from tests.conftest import (
    People,
    fresh_email,
    member_id,
    new_client,
    save_setting,
    sent_to,
    signed_in,
    token_in,
)
from tests.test_availability_api import assign, get, new_service, seed_booking, weekdays
from tests.test_bookings_api import DAY, at, post_booking
from tests.test_working_hours import seed

POLICY = "2026-09-01"


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture(autouse=True)
def _published(people: People) -> None:
    save_setting(people.a, "published", True)


@pytest.fixture
def ready(people: People, owner: TestClient) -> str:
    """A 30-minute service performed by the owner, who works 09:00-17:00 every day."""
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    return service_id


def hold_url(tenant_id: object, service_id: object) -> str:
    return f"/api/public/businesses/{tenant_id}/services/{service_id}/holds"


def post_hold(
    client: TestClient, tenant_id: object, service_id: object, **overrides: Any
) -> Response:
    body: dict[str, Any] = {
        "starts_at": at("09:00"),
        "email": fresh_email(),
        "locale": "en",
        **overrides,
    }
    return client.post(hold_url(tenant_id, service_id), json=body)


def read(client: TestClient, token: str) -> Response:
    return client.post("/api/public/booking-hold", json={"token": token})


def confirm(client: TestClient, token: str, **overrides: Any) -> Response:
    body: dict[str, Any] = {"token": token, "name": "Ana", "policy_version": POLICY, "consents": {}}
    return client.post("/api/public/booking-hold/confirm", json={**body, **overrides})


def run_jobs() -> None:
    """The verify emails only: a booking email would leave an outbox row behind."""
    verify = {"email.booking_verify": KINDS["email.booking_verify"]}

    async def run_until_idle() -> None:
        while await run_once(verify):
            pass

    asyncio.run(run_until_idle())


def link(tenant_id: object, email: str) -> str:
    """Runs the queued jobs, then returns `tenant.token` from the newest link mailed to email."""
    run_jobs()
    token = token_in(sent_to(email)[0])
    assert token.startswith(f"{tenant_id}.")
    return token


def held(
    app: FastAPI, tenant_id: object, service_id: object, email: str | None = None, **overrides: Any
) -> tuple[str, str]:
    """A hold placed and its email sent: (email, token)."""
    email = email or fresh_email()
    response = post_hold(new_client(app), tenant_id, service_id, email=email, **overrides)
    assert response.status_code == 202, response.text
    return email, link(tenant_id, email)


def as_operator(migrate_engine: Engine, tenant_id: object, sql: str, **params: Any) -> Any:
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        return conn.execute(text(sql), params).all()


def age(migrate_engine: Engine, tenant_id: object, email: str, column: str, ago: str) -> None:
    """Move one of a hold's clocks into the past (the app role cannot)."""
    as_operator(
        migrate_engine,
        tenant_id,
        f"UPDATE booking_holds SET {column} = now() - CAST(:ago AS interval) "
        "WHERE email = :e RETURNING 1",
        ago=ago,
        e=email,
    )


def holds_of(migrate_engine: Engine, tenant_id: object, email: str) -> list[Any]:
    return as_operator(
        migrate_engine, tenant_id, "SELECT * FROM booking_holds WHERE email = :e", e=email
    )


def slots(app: FastAPI, tenant_id: object, service_id: object) -> list[str]:
    response = get(new_client(app), tenant_id, service_id, DAY.isoformat(), DAY.isoformat())
    assert response.status_code == 200
    found: list[str] = response.json()["slots"]
    return [s.replace("+00:00", "Z") for s in found]


def counts(migrate_engine: Engine, tenant_id: object) -> dict[str, int]:
    tables = ("booking_holds", "bookings", "booking_events", "clients", "consents")
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        out = {t: conn.scalar(text(f"SELECT count(*) FROM {t}")) for t in tables}
        out["jobs"] = conn.scalar(
            text("SELECT count(*) FROM jobs WHERE tenant_id = :t"), {"t": tenant_id}
        )
    return out


# F1: FENCE (R2). Wrong impl: the arm filters on the 24h link (created_at + LINK_TTL), or on
# nothing, or reads no buffer.
def test_a_live_hold_blocks_its_time_and_buffer_and_an_expired_one_blocks_nothing(
    people: People, app: FastAPI, owner: TestClient, migrate_engine: Engine
) -> None:
    save_setting(people.a, "buffer_pct", 0)  # a missing buffer must read as no tail at all
    service_id = new_service(owner, buffer_minutes=15)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    assert {at("09:00"), at("09:30")} <= set(slots(app, people.a, service_id))

    email, _ = held(app, people.a, service_id)

    free = set(slots(app, people.a, service_id))
    assert at("09:00") not in free
    assert at("09:30") not in free  # inside the 15-minute buffer after 09:30
    assert at("09:45") in free

    age(migrate_engine, people.a, email, "expires_at", "1 second")  # the link still lives

    assert {at("09:00"), at("09:30")} <= set(slots(app, people.a, service_id))


# F2: FENCE (R3). Wrong impl: the read consumes, extends or deletes the hold, or enqueues anything.
def test_reading_the_link_writes_nothing(
    people: People, app: FastAPI, ready: str, migrate_engine: Engine
) -> None:
    email, token = held(app, people.a, ready)
    before = (counts(migrate_engine, people.a), holds_of(migrate_engine, people.a, email))

    client = new_client(app)
    for _ in range(2):
        response = read(client, token)
        assert response.status_code == 200

    assert (counts(migrate_engine, people.a), holds_of(migrate_engine, people.a, email)) == before
    body = response.json()
    assert set(body) == {
        "business",
        "slug",
        "timezone",
        "language",
        "service_id",
        "service_name",
        "starts_at",
        "ends_at",
        "price",
        "worker_display_name",
        "anyone",
        "cancellation",
        "auto_confirm",
        "policy_version",
        "held_until",
        "email",
    }
    assert body["email"] == email
    assert body["service_id"] == ready
    assert response.headers["Cache-Control"] == "no-store"


# F3: FENCE (R5). Wrong impl: the hold POST (or the verify mail) calls find_or_create, inserting a
# client for an unknown address or touching a known one (xmin moves on any ON CONFLICT DO UPDATE,
# even one that fills nothing; the stored phone and locale are blank, so FILL would fill them).
def test_nothing_is_written_about_a_person_before_the_click(
    people: People, app: FastAPI, ready: str, migrate_engine: Engine
) -> None:
    email = fresh_email()
    with tenant_context(people.a) as session:
        session.execute(
            text(
                "INSERT INTO clients (tenant_id, name, email) "
                "VALUES (current_setting('app.tenant_id')::uuid, 'Merchant Record', :e)"
            ),
            {"e": email},
        )
    stored = as_operator(
        migrate_engine,
        people.a,
        "SELECT xmin::text AS xmin, * FROM clients WHERE email = :e",
        e=email,
    )
    before = counts(migrate_engine, people.a)

    response = post_hold(new_client(app), people.a, ready, email=email, locale="pt")
    assert response.status_code == 202
    link(people.a, email)
    unknown = fresh_email()
    response = post_hold(new_client(app), people.a, ready, email=unknown, starts_at=at("10:00"))
    assert response.status_code == 202
    link(people.a, unknown)

    after = counts(migrate_engine, people.a)
    assert (
        as_operator(
            migrate_engine,
            people.a,
            "SELECT xmin::text AS xmin, * FROM clients WHERE email = :e",
            e=email,
        )
        == stored
    )
    assert (after["clients"], after["bookings"], after["booking_events"], after["consents"]) == (
        before["clients"],
        before["bookings"],
        before["booking_events"],
        before["consents"],
    )
    assert after["booking_holds"] == before["booking_holds"] + 2


# F4: FENCE. Wrong impl: a distinct answer per cause, or a lookup without the 24h filter.
def test_every_dead_link_is_the_same_404(
    people: People, app: FastAPI, ready: str, migrate_engine: Engine
) -> None:
    client = new_client(app)
    _, used = held(app, people.a, ready, starts_at=at("09:00"))
    assert confirm(client, used).status_code == 201

    replaced_email = fresh_email()
    first = post_hold(new_client(app), people.a, ready, email=replaced_email, starts_at=at("10:00"))
    replaced = link(people.a, replaced_email)
    again = post_hold(
        new_client(app),
        people.a,
        ready,
        email=replaced_email,
        starts_at=at("10:00"),
        replaces=first.json()["secret"],
    )
    assert again.status_code == 202

    old_email, old = held(app, people.a, ready, starts_at=at("11:00"))
    age(migrate_engine, people.a, old_email, "created_at", "24 hours 1 second")

    _, elsewhere = held(app, people.a, ready, starts_at=at("12:00"))
    foreign = f"{people.b}.{elsewhere.split('.', 1)[1]}"

    # A LIVE hold's page secret (its mail has gone out by now) is never a link token.
    as_secret = f"{people.a}.{again.json()['secret']}"
    for token in (used, replaced, old, foreign, as_secret, "garbage", f"{people.a}.short"):
        for answer in (read(client, token), confirm(client, token)):
            assert (answer.status_code, answer.json()) == (404, {"code": "link_expired"}), token


# F5: FENCE. Wrong impl: confirm requires a LIVE hold, or deletes the hold before re-deriving and
# keeps the delete when the time turns out taken.
def test_a_late_click_books_if_the_time_is_free_and_keeps_the_link_if_not(
    people: People, app: FastAPI, ready: str, migrate_engine: Engine
) -> None:
    free_email, free_token = held(app, people.a, ready, starts_at=at("09:00"))
    age(migrate_engine, people.a, free_email, "expires_at", "1 minute")
    assert confirm(new_client(app), free_token).status_code == 201

    taken_email, taken_token = held(app, people.a, ready, starts_at=at("10:00"))
    age(migrate_engine, people.a, taken_email, "expires_at", "1 minute")
    other = post_booking(new_client(app), people.a, ready, starts_at=at("10:00"))
    assert other.status_code == 201

    answer = confirm(new_client(app), taken_token)

    assert (answer.status_code, answer.json()) == (409, {"code": "slot_unavailable"})
    assert len(holds_of(migrate_engine, people.a, taken_email)) == 1
    assert read(new_client(app), taken_token).status_code == 200


# F6: FENCE. Wrong impl: replace by address (a stranger cancels a victim's hold), or a replace
# whose delete survives a refused new time.
def test_only_the_secret_replaces_a_hold_and_a_refused_replace_keeps_it(
    people: People, app: FastAPI, ready: str, migrate_engine: Engine
) -> None:
    email = fresh_email()
    first = post_hold(new_client(app), people.a, ready, email=email, starts_at=at("09:00"))
    first_token = link(people.a, email)
    secret = first.json()["secret"]

    # A stranger knowing the address, at the same business: a garbage secret or a well-formed
    # unknown one replaces nothing (four posts plus the two below stay inside the 5/h per address).
    for hour, replaces in (("13:00", "garbage"), ("14:00", "A" * 43)):
        stranger = post_hold(
            new_client(app), people.a, ready, email=email, starts_at=at(hour), replaces=replaces
        )
        assert stranger.status_code == 202
        assert set(stranger.json()) == {"starts_at", "expires_at", "secret"}
        assert read(new_client(app), first_token).status_code == 200
    assert at("09:00") not in slots(app, people.a, ready)

    seed_booking(people.a, ready, member_id(people.a, people.both), at("11:00"), at("11:30"))
    refused = post_hold(
        new_client(app), people.a, ready, email=email, starts_at=at("11:00"), replaces=secret
    )
    assert (refused.status_code, refused.json()) == (409, {"code": "slot_unavailable"})
    assert read(new_client(app), first_token).status_code == 200

    moved = post_hold(
        new_client(app), people.a, ready, email=email, starts_at=at("10:00"), replaces=secret
    )
    assert moved.status_code == 202
    second_token = link(people.a, email)

    assert at("09:00") in slots(app, people.a, ready)
    assert read(new_client(app), first_token).status_code == 404
    assert read(new_client(app), second_token).json()["starts_at"].startswith(DAY.isoformat())


# F7: FENCE. Wrong impl: confirm skips max_pending_per_email (and ZIF-115's auto-confirm arm).
def test_the_per_address_cap_runs_at_the_click(
    people: People, app: FastAPI, ready: str, migrate_engine: Engine
) -> None:
    save_setting(people.a, "max_pending_per_email", 1)
    save_setting(people.a, "auto_confirm", True)
    email, token = held(app, people.a, ready, starts_at=at("09:00"))
    assert confirm(new_client(app), token).status_code == 201

    _, second = held(app, people.a, ready, email=email, starts_at=at("10:00"))
    answer = confirm(new_client(app), second)

    assert (answer.status_code, answer.json()) == (429, {"code": "rate_limited"})
    assert len(holds_of(migrate_engine, people.a, email)) == 1


@pytest.fixture
def two(people: People, ready: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A second worker on the same service and hours: (owner's member id, worker's member id)."""
    seed(people.a, people.only_a, weekdays("09:00", "17:00"))
    other = member_id(people.a, people.only_a)
    assign(people.a, ready, other)
    return member_id(people.a, people.both), other


# F8: FENCE. Wrong impl: a named hold falls back to a colleague, or "anyone" never falls back.
def test_anyone_falls_back_to_a_free_colleague_and_a_named_worker_never_does(
    people: People,
    app: FastAPI,
    ready: str,
    two: tuple[uuid.UUID, uuid.UUID],
    migrate_engine: Engine,
) -> None:
    anyone_email, anyone_token = held(app, people.a, ready, starts_at=at("09:00"))
    (hold,) = holds_of(migrate_engine, people.a, anyone_email)
    assert hold.anyone
    seed_booking(people.a, ready, hold.worker_id, at("09:00"), at("09:30"))

    booked = confirm(new_client(app), anyone_token)

    assert booked.status_code == 201
    assert booked.json()["worker_id"] == str(next(m for m in two if m != hold.worker_id))

    named = two[0]
    _, named_token = held(app, people.a, ready, starts_at=at("10:00"), member_id=str(named))
    seed_booking(people.a, ready, named, at("10:00"), at("10:30"))

    refused = confirm(new_client(app), named_token)

    assert (refused.status_code, refused.json()) == (409, {"code": "slot_unavailable"})


# F9: FENCE. Wrong impl: the sweep deletes on the 15-minute expires_at, or deletes nothing.
def test_the_sweep_deletes_a_hold_once_its_link_is_dead(
    people: People, app: FastAPI, ready: str, migrate_engine: Engine
) -> None:
    dead, _ = held(app, people.a, ready, starts_at=at("09:00"))
    lapsed, _ = held(app, people.a, ready, starts_at=at("10:00"))
    age(migrate_engine, people.a, dead, "created_at", "24 hours")
    age(migrate_engine, people.a, lapsed, "expires_at", "1 hour")

    bookings.sweep()

    assert holds_of(migrate_engine, people.a, dead) == []
    assert len(holds_of(migrate_engine, people.a, lapsed)) == 1


# F10: FENCE. Wrong impl: no cross-business key, so one address can be mailed by every business.
def test_one_address_is_limited_across_businesses(people: People, app: FastAPI, ready: str) -> None:
    email = fresh_email()
    for _ in range(10):  # ten holds at other businesses this hour
        assert not limits.hit({limits.email_key("booking_verify", email): 10}, timedelta(hours=1))

    answer = post_hold(new_client(app), people.a, ready, email=email)

    assert (answer.status_code, answer.json()) == (429, {"code": "rate_limited"})
    assert post_hold(new_client(app), people.a, ready).status_code == 202


# F11: FENCE (decision 4, option A). Wrong impl: FILL at confirm (the stored name survives), or a
# blank phone nulling the stored one.
def test_the_click_replaces_the_name_and_keeps_a_phone_left_blank(
    people: People, app: FastAPI, ready: str, migrate_engine: Engine
) -> None:
    email = fresh_email()
    with tenant_context(people.a) as session:
        session.execute(
            text(
                "INSERT INTO clients (tenant_id, name, email, phone) "
                "VALUES (current_setting('app.tenant_id')::uuid, 'Old Name', :e, '+31600000001')"
            ),
            {"e": email},
        )
    _, token = held(app, people.a, ready, email=email)

    assert confirm(new_client(app), token, name="New Name").status_code == 201

    (client,) = as_operator(
        migrate_engine, people.a, "SELECT * FROM clients WHERE email = :e", e=email
    )
    assert (client.name, client.phone, client.user_id) == ("New Name", "+31600000001", None)

    _, typed = held(app, people.a, ready, email=email, starts_at=at("10:00"))
    assert confirm(new_client(app), typed, phone="+31600000002").status_code == 201
    (client,) = as_operator(
        migrate_engine, people.a, "SELECT * FROM clients WHERE email = :e", e=email
    )
    assert client.phone == "+31600000002"


def first_after_tenant(executed: list[tuple[str, Any]]) -> tuple[str, Any]:
    tenant_set = next(
        i for i, (s, _) in enumerate(executed) if s.startswith("SELECT set_config('app.tenant_id'")
    )
    return executed[tenant_set + 1]


def recorded(app_engine: Engine, run: Callable[[], Any]) -> list[tuple[str, Any]]:
    executed: list[tuple[str, Any]] = []

    def record(conn: object, cursor: object, statement: str, parameters: Any, *args: Any) -> None:
        executed.append((statement, parameters))

    event.listen(app_engine, "before_cursor_execute", record)
    try:
        run()
    finally:
        event.remove(app_engine, "before_cursor_execute", record)
    return executed


# F12: FENCE (R1). Wrong impl: any writer of booking_holds whose first statement is not lock 51.
def test_every_hold_writer_takes_the_tenant_lock_first(
    people: People, app: FastAPI, ready: str, app_engine: Engine, migrate_engine: Engine
) -> None:
    email = fresh_email()
    posted: list[Response] = []
    writers = {
        "hold": recorded(
            app_engine,
            lambda: posted.append(post_hold(new_client(app), people.a, ready, email=email)),
        ),
        "mint": recorded(app_engine, run_jobs),
    }
    token = token_in(sent_to(email)[0])
    writers["replace"] = recorded(
        app_engine,
        lambda: posted.append(
            post_hold(
                new_client(app),
                people.a,
                ready,
                starts_at=at("10:00"),
                replaces=posted[0].json()["secret"],
            )
        ),
    )
    _, live = held(app, people.a, ready, starts_at=at("11:00"))
    writers["confirm"] = recorded(app_engine, lambda: posted.append(confirm(new_client(app), live)))
    writers["sweep"] = recorded(app_engine, bookings.sweep)

    assert [p.status_code for p in posted] == [202, 202, 201]
    assert read(new_client(app), token).status_code == 404  # the replace really ran
    for name, executed in writers.items():
        statement, params = first_after_tenant(executed)
        assert statement.startswith("SELECT pg_advisory_xact_lock("), name
        assert params == {"key": 51}, name


# F13: FENCE (R4). Wrong impl: any log line or span on the new paths carrying the address, the
# secret, the token or a hash of either.
def test_no_log_line_or_span_carries_the_address_secret_or_token(
    people: People,
    app: FastAPI,
    ready: str,
    caplog: pytest.LogCaptureFixture,
    spans: Callable[[], list[ReadableSpan]],
) -> None:
    email = fresh_email()
    with caplog.at_level(logging.DEBUG), caplog.at_level(logging.DEBUG, logger="app"):
        first = post_hold(new_client(app), people.a, ready, email=email)
        secret = first.json()["secret"]
        again = post_hold(new_client(app), people.a, ready, email=email, replaces=secret)
        token = link(people.a, email)
        assert read(new_client(app), token).status_code == 200
        assert confirm(new_client(app), token).status_code == 201

    logged = "\n".join(repr(vars(record)) for record in caplog.records)
    traced = "\n".join(
        repr((s.name, s.attributes, [(e.name, e.attributes) for e in s.events])) for s in spans()
    )
    assert "booking hold placed" in logged
    assert "booking confirmed by link" in logged
    assert "email sent" in logged
    secrets_ = (secret, again.json()["secret"], token.split(".", 1)[1])
    spellings = [email]
    for value in secrets_:
        digest = hashlib.sha256(value.encode()).digest()
        spellings += [value, digest.hex(), repr(digest), base64.b64encode(digest).decode()]
    for spelling in spellings:
        assert spelling not in logged
        assert spelling not in traced


# F14: FENCE. Wrong impl: the mailed token is not the one whose hash is stored, the link is not
# the confirm page, or a resend of the job leaves the first link working.
def test_the_verify_email_carries_the_one_working_link(
    people: People, app: FastAPI, ready: str, migrate_engine: Engine
) -> None:
    save_setting(people.a, "language", "nl")
    email, token = held(app, people.a, ready, locale="pt")
    message = sent_to(email)[0]
    (hold,) = holds_of(migrate_engine, people.a, email)
    with tenant_context(people.a) as session:
        business, slug = session.execute(
            text("SELECT name, slug FROM tenants WHERE id = :t"), {"t": people.a}
        ).one()

    assert message["Subject"] == f"Confirme seu agendamento em {business}"
    assert f"/pt/booking/confirm#{token}" in message["Text"]
    assert f"/pt/{slug}\n" in message["Text"]
    assert hold.token_hash == hashlib.sha256(token.split(".", 1)[1].encode()).digest()

    mail.send_booking_verify(
        Job(uuid.uuid4(), "email.booking_verify", people.a, {"hold_id": str(hold.id)})
    )

    assert read(new_client(app), token).status_code == 404
    assert read(new_client(app), token_in(sent_to(email)[0])).status_code == 200


# F15: FENCE. Wrong impl: confirm writes the booking without the shared core's emails, or leaves
# the hold behind.
@pytest.mark.parametrize(
    ("auto_confirm", "templates"),
    [
        (True, {"booking_confirmed", "booking_new"}),
        (False, {"booking_received", "booking_request"}),
    ],
)
def test_the_click_books_and_emails_exactly_as_the_booking_page_did(
    people: People,
    app: FastAPI,
    ready: str,
    migrate_engine: Engine,
    auto_confirm: bool,
    templates: set[str],
) -> None:
    save_setting(people.a, "auto_confirm", auto_confirm)
    email, token = held(app, people.a, ready)

    answer = confirm(new_client(app), token, phone="+31600000003")

    assert answer.status_code == 201
    assert answer.json()["status"] == ("confirmed" if auto_confirm else "pending")
    jobs = as_operator(
        migrate_engine,
        people.a,
        "SELECT payload->>'template' AS template FROM jobs WHERE payload->>'booking_id' = :b",
        b=answer.json()["id"],
    )
    assert {j.template for j in jobs} - {"booking_reminder"} == templates
    assert holds_of(migrate_engine, people.a, email) == []
    (event_row,) = as_operator(
        migrate_engine,
        people.a,
        "SELECT event, policy_version FROM booking_events WHERE booking_id = :b",
        b=answer.json()["id"],
    )
    assert tuple(event_row) == ("created", POLICY)


# F16: FENCE. Wrong impl: the console answers slot_unavailable over a hold, or override is refused.
def test_the_console_names_a_hold_and_override_wins_over_it(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    _, token = held(app, people.a, ready)
    hold_until = read(new_client(app), token).json()["held_until"]
    body = {
        "new_client": {"name": "Walk-in"},
        "service_id": ready,
        "member_id": str(member_id(people.a, people.both)),
        "starts_at": at("09:00"),
    }

    refused = owner.post("/api/bookings", json=body)

    assert (refused.status_code, refused.json()) == (
        409,
        {"code": "slot_held", "held_until": hold_until},
    )
    assert owner.post("/api/bookings", json={**body, "override": True}).status_code == 201
    late = confirm(new_client(app), token)
    assert (late.status_code, late.json()) == (409, {"code": "slot_unavailable"})


# G3: GUARD. The hold takes no personal detail but the address.
@pytest.mark.parametrize(
    "extra",
    [{"name": "Ana"}, {"phone": "+31600000000"}, {"consents": {}}, {"policy_version": POLICY}],
)
def test_a_hold_takes_nothing_but_the_address(
    people: People, app: FastAPI, ready: str, extra: dict[str, Any]
) -> None:
    assert post_hold(new_client(app), people.a, ready, **extra).status_code == 422


# G4: GUARD. Today's page keeps working beside holds: a live hold refuses the same time there.
def test_a_live_hold_refuses_the_time_on_the_old_booking_post(
    people: People, app: FastAPI, ready: str
) -> None:
    held(app, people.a, ready)

    answer = post_booking(new_client(app), people.a, ready, starts_at=at("09:00"))

    assert (answer.status_code, answer.json()) == (409, {"code": "slot_unavailable"})
    assert post_booking(new_client(app), people.a, ready, starts_at=at("10:00")).status_code == 201
