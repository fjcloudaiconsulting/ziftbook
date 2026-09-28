"""/api/public/booking-link/* (ZIF-54): a client manages one booking from the link in their
booking emails, with no account. See docs/specs/2026-09-24-zif-54-spec.md for the numbered tests
this file protects (backend half; the frontend and 30/38 live elsewhere)."""

import base64
import contextlib
import email as email_module
import hashlib
import logging
import secrets
import threading
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from opentelemetry.sdk.trace import ReadableSpan
from psycopg.errors import CheckViolation, InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from app import booking_links, mail
from app.booking_links import COOKIE
from app.bookings import RESCHEDULE
from app.db import tenant_context
from app.jobs import Job
from tests.conftest import (
    People,
    fresh_address,
    fresh_email,
    member_id,
    new_client,
    put_settings,
    save_setting,
    signed_in,
    wait_until_blocked,
)
from tests.test_availability_api import assign, new_service, weekdays
from tests.test_booking_email import (
    MAILPIT,
    _client_email,
    _search,
    clean_outbox,  # noqa: F401 -- autouse: these tests send mail, so they leave outbox rows
    jobs_for_booking,
    template_of,
    texts_to,
)
from tests.test_bookings_api import DAY, TODAY, ZONE, at, post_booking
from tests.test_bookings_approval_api import ago, expire, make_pending, patch, shift
from tests.test_working_hours import seed


@pytest.fixture
def app(people: People) -> FastAPI:
    from app.main import create_app

    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture
def ready(people: People, owner: TestClient) -> str:
    """A 30-minute service performed by the owner, who works 09:00-17:00 every day."""
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    return service_id


@pytest.fixture
def worker_ready(people: People, owner: TestClient) -> str:
    """A 30-minute service performed only by only_a, a worker who is not an owner: its bookings
    are the ones that tell "every owner" apart from "every owner plus the assigned worker" (R1)."""
    service_id = new_service(owner)
    seed(people.a, people.only_a, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.only_a))
    return service_id


def merchant_recipients(
    app_engine: Engine, tenant_id: uuid.UUID, booking_id: str, template: str
) -> list[str]:
    """The user_id of every merchant email of `template` enqueued for this booking, sorted."""
    return sorted(
        j["payload"]["user_id"]
        for j in jobs_for_booking(app_engine, tenant_id, booking_id)
        if template_of(j) == template
    )


def confirmed_booking(app: FastAPI, tenant_id: object, service_id: str, **overrides: Any) -> str:
    booking_id = make_pending(app, tenant_id, service_id, **overrides)
    return booking_id


def mint(tenant_id: uuid.UUID, booking_id: str) -> str:
    """A raw token whose hash is inserted straight into booking_links, bypassing mail.py's mint
    path -- these tests are the router's, not the mailer's (test_booking_email.py owns that)."""
    token = secrets.token_urlsafe(32)
    with tenant_context(tenant_id) as session:
        session.execute(
            text("""
            INSERT INTO booking_links (token_hash, tenant_id, booking_id)
            VALUES (:h, current_setting('app.tenant_id')::uuid, :b)
            """),
            {"h": hashlib.sha256(token.encode()).digest(), "b": booking_id},
        )
    return token


def with_cookie(app: FastAPI, tenant_id: uuid.UUID, token: str) -> TestClient:
    client = new_client(app)
    client.cookies.set(COOKIE, f"{tenant_id}.{token}")
    return client


def linked(app: FastAPI, tenant_id: uuid.UUID, booking_id: str) -> TestClient:
    """A client already holding the manage cookie for booking_id."""
    return with_cookie(app, tenant_id, mint(tenant_id, booking_id))


URL = "/api/public/booking-link"


def get(client: TestClient, path: str = "", **params: Any) -> Response:
    return client.get(f"{URL}{path}", params=params)


def post(client: TestClient, path: str, body: dict[str, Any]) -> Response:
    return client.post(f"{URL}{path}", json=body)


# ---------------------------------------------------------------------------
# D3/D4: the exchange and the token/hash oracle.
# ---------------------------------------------------------------------------


def test_the_exchange_sets_the_cookie_and_the_fragment_is_never_needed_again(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    token = mint(people.a, booking_id)
    client = new_client(app)

    response = post(client, "/session", {"token": f"{people.a}.{token}"})

    assert response.status_code == 204
    cookie = response.cookies.get(COOKIE)
    assert cookie == f"{people.a}.{token}"
    assert response.headers["cache-control"] == "no-store"


# 5. fence. Kills a lookup outside the token's own tenant RLS.
def test_a_token_from_another_tenant_is_link_expired(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    token = mint(people.a, booking_id)

    response = post(new_client(app), "/session", {"token": f"{people.b}.{token}"})

    assert response.status_code == 404
    assert response.json()["code"] == "link_expired"


# 6. fence. Every failure shape gives the identical 404 body: no oracle.
def test_every_failure_shape_gives_the_identical_404(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    live_token = mint(people.a, booking_id)
    dead_id = confirmed_booking(app, people.a, ready, starts_at=at("11:00"))
    dead_token = mint(people.a, dead_id)
    assert patch(signed_in(app, people.a, people.both), dead_id, "declined").status_code == 200

    def exchange(token: str) -> Response:
        return post(new_client(app), "/session", {"token": token})

    answers = [
        exchange("not-a-token"),  # no tenant part at all
        exchange(f"{people.a}.too-short"),  # a real tenant, a secret of the wrong shape
        exchange(f"{uuid.uuid4()}.{live_token}"),  # unknown tenant
        exchange(f"{people.a}.{secrets.token_urlsafe(32)}"),  # unknown hash
        exchange(f"{people.a}.{dead_token}"),  # a real link of a dead booking
        get(new_client(app)),  # no cookie
    ]

    assert [(a.status_code, a.json()) for a in answers] == [(404, {"code": "link_expired"})] * 6


# 7. fence. Merchant cancel, decline, pending expiry and starts_at passing all revoke every route,
# the exchange included. Kills a stored expiry, or a view window after the start (the "started"
# case is one minute past its start, so any grace period at all keeps it alive).
@pytest.mark.parametrize("death", ["cancelled", "declined", "expired", "started"])
def test_a_dead_booking_gives_404_on_every_route(
    people: People, app: FastAPI, ready: str, death: str
) -> None:
    booking_id = confirmed_booking(app, people.a, ready, starts_at=at("10:00"))
    owner = signed_in(app, people.a, people.both)
    if death in ("cancelled", "started"):
        assert patch(owner, booking_id, "confirmed").status_code == 200
    token = mint(people.a, booking_id)
    client = with_cookie(app, people.a, token)
    assert get(client).status_code == 200

    if death == "cancelled":
        assert patch(owner, booking_id, "cancelled_by_merchant").status_code == 200
    elif death == "declined":
        assert patch(owner, booking_id, "declined").status_code == 200
    elif death == "expired":
        expire(people.a, booking_id)
    else:
        shift(people.a, booking_id, ago(at("10:00"), timedelta(minutes=1)))

    day = {"from": DAY.isoformat(), "to": DAY.isoformat()}
    answers = [
        post(new_client(app), "/session", {"token": f"{people.a}.{token}"}),
        get(client),
        get(client, "/availability", **day),
        post(client, "/cancel", {"booking_id": booking_id, "refund_pct": 0}),
        post(client, "/reschedule", reschedule_body(booking_id, at("11:00"), 0)),
        post(client, "/consents", {"booking_id": booking_id}),
    ]
    assert [(a.status_code, a.text) for a in answers] == [(404, '{"code":"link_expired"}')] * 6


# 8. fence. Opening (session/read/availability) writes nothing: bookings, events, consents, jobs
# and booking_links row counts are unchanged.
def counts(migrate_engine: Engine, tenant_id: uuid.UUID) -> tuple[int, int, int, int, int]:
    with tenant_context(tenant_id) as session:
        b = session.scalar(text("SELECT count(*) FROM bookings"))
        e = session.scalar(text("SELECT count(*) FROM booking_events"))
        c = session.scalar(text("SELECT count(*) FROM consents"))
        j = session.scalar(text("SELECT count(*) FROM jobs"))
        links = session.scalar(text("SELECT count(*) FROM booking_links"))
    return (b, e, c, j, links)


def test_opening_the_link_has_no_side_effects(
    people: People, app: FastAPI, ready: str, migrate_engine: Engine
) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    token = mint(people.a, booking_id)
    client = new_client(app)

    before = counts(migrate_engine, people.a)
    assert post(client, "/session", {"token": f"{people.a}.{token}"}).status_code == 204
    assert get(client).status_code == 200
    assert (
        get(client, "/availability", **{"from": DAY.isoformat(), "to": DAY.isoformat()}).status_code
        == 200
    )
    after = counts(migrate_engine, people.a)

    assert before == after


# 9. fence. The exchange records no consent.
def test_the_exchange_records_no_consent(people: People, app: FastAPI, ready: str) -> None:
    booking_id = confirmed_booking(app, people.a, ready, consents={"marketing_email": True})
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    token = mint(people.a, booking_id)

    assert post(new_client(app), "/session", {"token": f"{people.a}.{token}"}).status_code == 204

    with tenant_context(people.a) as session:
        assert session.scalar(text("SELECT count(*) FROM consents")) == 0
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM booking_events WHERE booking_id = :id "
                    "AND event = 'consent_confirmed'"
                ),
                {"id": booking_id},
            )
            == 0
        )


# 10. fence. text/plain -> 415; cookie flags; every response carries no-referrer/no-store.
def test_json_only_cookie_flags_and_headers(people: People, app: FastAPI, ready: str) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)

    plain = client.post(
        f"{URL}/cancel",
        content=b'{"booking_id": "x", "refund_pct": 0}',
        headers={"content-type": "text/plain"},
    )
    assert plain.status_code == 415

    token = mint(people.a, booking_id)
    exchange = post(new_client(app), "/session", {"token": f"{people.a}.{token}"})
    flags = {f.strip().lower() for f in exchange.headers["set-cookie"].split(";")[1:]}
    assert {"httponly", "secure", "samesite=strict", "path=/", "max-age=3600"} <= flags

    # Every route, success and failure alike (spec S4).
    view = get(client)
    responses = [
        plain,
        exchange,
        post(new_client(app), "/session", {"token": "not-a-token"}),
        view,
        get(client, "/availability", **{"from": DAY.isoformat(), "to": DAY.isoformat()}),
        post(client, "/consents", {"booking_id": booking_id}),
        post(client, "/reschedule", reschedule_body(booking_id, at("11:00"), 0)),
        post(client, "/cancel", cancel_body(booking_id, 42)),
        post(client, "/cancel", cancel_body(booking_id, view.json()["engine"]["refund_pct"])),
        get(client),
    ]
    assert [r.status_code for r in responses] == [415, 204, 404, 200, 200, 409, 200, 409, 200, 404]
    for response in responses:
        assert response.headers.get("cache-control") == "no-store", response.request.url
        assert response.headers.get("referrer-policy") == "no-referrer", response.request.url


# 11. fence. The cookie of booking B with body booking_id A -> 409 link_changed; A untouched.
def test_cookie_and_body_booking_mismatch_is_link_changed(
    people: People, app: FastAPI, ready: str
) -> None:
    a = confirmed_booking(app, people.a, ready)
    b = confirmed_booking(app, people.a, ready, starts_at=at("11:00"))
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, a, "confirmed").status_code == 200
    assert patch(owner, b, "confirmed").status_code == 200
    client_for_b = linked(app, people.a, b)
    # B's own terms, so the echo alone would let the cancel through: only the id check refuses it.
    refund_pct = get(client_for_b).json()["engine"]["refund_pct"]

    response = post(client_for_b, "/cancel", {"booking_id": a, "refund_pct": refund_pct})

    assert response.status_code == 409
    assert response.json()["code"] == "link_changed"
    with tenant_context(people.a) as session:
        status: str = session.scalar(text("SELECT status FROM bookings WHERE id = :id"), {"id": a})
    assert status == "confirmed"


# 12. fence. The LinkedBooking key set carries no client name, email, phone or ids.
def test_linked_booking_carries_no_client_identity(
    people: People, app: FastAPI, ready: str
) -> None:
    email = fresh_email()
    booking_id = confirmed_booking(app, people.a, ready, email=email)
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)

    body = get(client).json()

    blob = str(body)
    assert set(body) == {"booking", "engine", "pending_consents"}
    assert set(body["engine"]) == {
        "can_cancel",
        "can_reschedule",
        "refund_pct",
        "copy_key",
        "reschedule_count",
        "reschedules_left",
        "free_until",
    }
    assert set(body["booking"]) == {
        "id",
        "status",
        "business",
        "timezone",
        "starts_at",
        "ends_at",
        "original_starts_at",
        "service_name",
        "price",
        "worker_display_name",
        "cancellation_policy_text",
    }
    assert "Guest" not in blob  # post_booking's default client name
    assert email not in blob
    with tenant_context(people.a) as session:
        ids = session.execute(
            text("SELECT client_id, worker_id FROM bookings WHERE id = :id"), {"id": booking_id}
        ).one()
    assert str(ids.client_id) not in blob
    assert str(ids.worker_id) not in blob


# ---------------------------------------------------------------------------
# Cancel: engine, echo, emails.
# ---------------------------------------------------------------------------


def cancel_body(booking_id: object, refund_pct: int) -> dict[str, Any]:
    return {"booking_id": booking_id, "refund_pct": refund_pct}


# 18. fence. A stale echoed refund_pct -> 409 terms_changed, nothing written.
def test_a_stale_refund_echo_is_terms_changed(people: People, app: FastAPI, ready: str) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)

    response = post(client, "/cancel", cancel_body(booking_id, 42))

    assert response.status_code == 409
    assert response.json()["code"] == "terms_changed"
    with tenant_context(people.a) as session:
        status: str = session.scalar(
            text("SELECT status FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    assert status == "confirmed"


# 27. fence. A pending booking: reschedule -> 409 not_allowed, cancel -> 200; copy_key pending.
def test_a_pending_booking_can_be_cancelled_but_not_rescheduled(
    people: People, app: FastAPI, ready: str
) -> None:
    # Still pending, not yet accepted, and 20 days out: the engine alone would refund 100, so the
    # refund 0 below is the pending rule's at any hour. Kills a pending keeping the engine's pct.
    far = at("10:00", day=TODAY + timedelta(days=20))
    booking_id = confirmed_booking(app, people.a, ready, starts_at=far)
    client = linked(app, people.a, booking_id)

    view = get(client).json()
    assert view["engine"]["copy_key"] == "pending"
    assert view["engine"]["can_reschedule"] is False
    assert view["engine"]["refund_pct"] == 0

    reschedule_response = post(
        client,
        "/reschedule",
        {"booking_id": booking_id, "starts_at": at("10:00"), "reschedule_count": 0},
    )
    assert (reschedule_response.status_code, reschedule_response.json()["code"]) == (
        409,
        "not_allowed",
    )

    cancel_response = post(client, "/cancel", cancel_body(booking_id, 0))
    assert cancel_response.status_code == 200
    assert cancel_response.json()["engine"]["refund_pct"] == 0


# 23. fence. A client cancel enqueues booking_cancelled_by_client once, booking_client_cancelled
# once per owner/worker, even on a double submit.
def test_client_cancel_enqueues_the_right_emails_once_each(
    people: People, app: FastAPI, worker_ready: str, app_engine: Engine
) -> None:
    booking_id = confirmed_booking(app, people.a, worker_ready)
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)
    refund_pct = get(client).json()["engine"]["refund_pct"]

    first = post(client, "/cancel", cancel_body(booking_id, refund_pct))
    assert first.status_code == 200
    second = post(client, "/cancel", cancel_body(booking_id, refund_pct))
    assert second.status_code == 404  # no longer live: D2

    jobs = jobs_for_booking(app_engine, people.a, booking_id)
    templates = [template_of(j) for j in jobs]
    assert templates.count("booking_cancelled_by_client") == 1
    with tenant_context(people.a) as session:
        recorded = session.scalars(
            text("SELECT event FROM booking_events WHERE booking_id = :id"), {"id": booking_id}
        ).all()
    assert recorded.count("cancelled_by_client") == 1  # kills the cancel logging another event
    # R1: the one owner and the assigned (non-owner) worker, once each. Kills worker_id=None.
    assert merchant_recipients(
        app_engine, people.a, booking_id, "booking_client_cancelled"
    ) == sorted([str(people.both), str(people.only_a)])


# 20. fence. cancelled_by_client is not, and never becomes, a merchant PATCH target.
def test_cancelled_by_client_is_not_a_merchant_transition_target(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200

    response = owner.patch(f"/api/bookings/{booking_id}", json={"status": "cancelled_by_client"})

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Reschedule: the offered() pipeline, the echo, the emails.
# ---------------------------------------------------------------------------


def reschedule_body(booking_id: object, starts_at: str, count: int) -> dict[str, Any]:
    return {"booking_id": booking_id, "starts_at": starts_at, "reschedule_count": count}


# 13. fence. A reschedule outside the offered pipeline -> 409 (time off, hours, buffer, horizon,
# unassigned worker). Here: after the worker is unassigned from the service.
def test_reschedule_after_the_worker_is_unassigned_is_slot_unavailable(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)

    assert owner.put(f"/api/services/{ready}/workers", json=[]).status_code == 200

    response = post(client, "/reschedule", reschedule_body(booking_id, at("11:00"), 0))

    assert response.status_code == 409
    assert response.json()["code"] == "slot_unavailable"


# 13 (part). Outside working hours entirely.
def test_reschedule_outside_working_hours_is_slot_unavailable(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)

    response = post(client, "/reschedule", reschedule_body(booking_id, at("23:00"), 0))

    assert response.status_code == 409
    assert response.json()["code"] == "slot_unavailable"


# 14. fence. 10:00-11:00 moved to 10:30 succeeds, and another booking of the same worker still
# blocks (exclude= in booked()).
def test_reschedule_into_a_free_slot_succeeds_and_others_still_block(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    booking_id = confirmed_booking(app, people.a, ready, starts_at=at("10:00"))
    assert patch(owner, booking_id, "confirmed").status_code == 200
    other = confirmed_booking(app, people.a, ready, starts_at=at("14:00"))
    assert patch(owner, other, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)

    ok = post(client, "/reschedule", reschedule_body(booking_id, at("10:30"), 0))
    assert ok.status_code == 200
    assert ok.json()["booking"]["starts_at"] == at("10:30")

    blocked = post(
        linked(app, people.a, other), "/reschedule", reschedule_body(other, at("10:45"), 0)
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "slot_unavailable"


# 16. fence. A raw UPDATE past max_reschedules raises 23514. Kills a Python-only cap.
def test_a_raw_update_past_max_reschedules_raises_the_check(
    people: People, app: FastAPI, ready: str, owner: TestClient, migrate_engine: Engine
) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    with migrate_engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text("UPDATE bookings SET reschedule_count = max_reschedules WHERE id = :id"),
            {"id": booking_id},
        )
        conn.commit()
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        with pytest.raises(IntegrityError) as raised:
            conn.execute(
                text("UPDATE bookings SET reschedule_count = reschedule_count + 1 WHERE id = :id"),
                {"id": booking_id},
            )
        conn.rollback()
    assert isinstance(raised.value.orig, CheckViolation)


# 21. fence. max_reschedules is read from the snapshot at insert, never the live setting.
def test_max_reschedules_is_the_snapshot_not_the_live_setting(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    assert put_settings_max(owner, 3) == 200
    booking_id = confirmed_booking(app, people.a, ready)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    assert put_settings_max(owner, 0) == 200

    view = get(linked(app, people.a, booking_id)).json()

    assert view["engine"]["reschedules_left"] == 3
    assert view["engine"]["can_reschedule"] is True
    moved = post(
        linked(app, people.a, booking_id),
        "/reschedule",
        reschedule_body(booking_id, at("11:00"), 0),
    )
    assert moved.status_code == 200, moved.json()
    assert moved.json()["engine"]["reschedules_left"] == 2


def put_settings_max(owner: TestClient, value: int) -> int:
    response: int = owner.put("/api/settings", json={"max_reschedules": value}).status_code
    return response


# 22. fence/DB. The app role's UPDATE ... SET original_starts_at raises 42501, and after two
# reschedules the column still holds the first start.
def test_original_starts_at_is_write_once_by_privilege(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    booking_id = confirmed_booking(app, people.a, ready, starts_at=at("10:00"))
    assert patch(owner, booking_id, "confirmed").status_code == 200
    with tenant_context(people.a) as session:
        with pytest.raises(ProgrammingError) as raised:
            session.execute(
                # Its own value: only the missing privilege can refuse this, never a CHECK.
                text("UPDATE bookings SET original_starts_at = original_starts_at WHERE id = :id"),
                {"id": booking_id},
            )
    assert isinstance(raised.value.orig, InsufficientPrivilege)

    client = linked(app, people.a, booking_id)
    assert (
        post(client, "/reschedule", reschedule_body(booking_id, at("10:30"), 0)).status_code == 200
    )
    assert (
        post(
            linked(app, people.a, booking_id),
            "/reschedule",
            reschedule_body(booking_id, at("11:00"), 1),
        ).status_code
        == 200
    )
    with tenant_context(people.a) as session:
        original = session.scalar(
            text("SELECT original_starts_at FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    assert original.strftime("%Y-%m-%dT%H:%M:%SZ") == at("10:00")


def booking_jobs(app_engine: Engine, tenant_id: uuid.UUID, booking_id: str) -> list[Job]:
    with app_engine.begin() as conn:
        rows = conn.execute(
            text("""
            SELECT id, kind, payload FROM jobs
            WHERE tenant_id = :t AND payload->>'booking_id' = :b
            ORDER BY id  -- uuidv7: insertion order
            """),
            {"t": tenant_id, "b": str(booking_id)},
        ).tuples()
        return [Job(id_, kind, tenant_id, payload) for id_, kind, payload in rows]


def same_instant(payload_value: str, api_value: str) -> bool:
    return datetime.fromisoformat(payload_value) == datetime.fromisoformat(api_value)


# 24. fence. A reschedule enqueues booking_confirmed (:r1), booking_client_rescheduled per
# merchant and a reminder for the NEW start; the confirmation's .ics carries SEQUENCE:1 and the old
# reminder sends nothing. Kills a key without the count, a forgotten remind (the confirm already
# enqueued a reminder for the OLD start, so "some reminder exists" proves nothing) and SEQUENCE:0.
def test_reschedule_enqueues_confirmation_merchant_email_and_reminder(
    people: People, app: FastAPI, worker_ready: str, owner: TestClient, app_engine: Engine
) -> None:
    booking_id = confirmed_booking(app, people.a, worker_ready, starts_at=at("10:00"))
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)

    response = post(client, "/reschedule", reschedule_body(booking_id, at("10:30"), 0))
    assert response.status_code == 200

    jobs = jobs_for_booking(app_engine, people.a, booking_id)
    keys = {j["dedupe_key"] for j in jobs}
    assert any(k.endswith("booking_confirmed:r1") for k in keys)
    assert any(f"booking_client_rescheduled:r1:{people.both}" in k for k in keys)
    # R1: the one owner and the assigned (non-owner) worker, once each. Kills worker_id=None.
    assert merchant_recipients(
        app_engine, people.a, booking_id, "booking_client_rescheduled"
    ) == sorted([str(people.both), str(people.only_a)])
    reminders = [
        j
        for j in booking_jobs(app_engine, people.a, booking_id)
        if j.payload["template"] == "booking_reminder"
    ]
    assert [same_instant(j.payload["starts_at"], at("10:30")) for j in reminders] == [False, True]

    client_email = _client_email(people.a, booking_id)
    mail.send_booking(reminders[0])  # the old start's reminder
    assert _search(client_email) == []
    (confirmation,) = [
        j
        for j in booking_jobs(app_engine, people.a, booking_id)
        if j.payload["template"] == "booking_confirmed"
        and "starts_at" in j.payload
        and same_instant(j.payload["starts_at"], at("10:30"))
    ]
    mail.send_booking(confirmation)
    (sent,) = _search(client_email)
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/message/{sent['ID']}/raw", timeout=5) as raw:
        parsed = email_module.message_from_bytes(raw.read())
    (calendar,) = [p for p in parsed.walk() if p.get_content_type() == "text/calendar"]
    ics = calendar.get_payload(decode=True)
    assert isinstance(ics, bytes)
    assert f"UID:{booking_id}".encode() in ics.replace(b"\r\n ", b"")
    assert b"SEQUENCE:1\r\n" in ics


# 17. fence. 30-odd hours out with free cancellation at 120h (so no refund today), rescheduled
# three weeks out, then cancelled -> refund 0. Kills measuring the refund from the new start alone.
def test_refund_is_anchored_to_the_earliest_start_ever_held(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    # 120h, not the default 48h: DAY 09:00 is 33-57h away depending on the clock, and the fence
    # must not depend on the time of day it runs.
    assert put_settings(owner, {"free_cancellation_hours": 120}).status_code == 200
    booking_id = confirmed_booking(app, people.a, ready, starts_at=at("09:00"))
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)
    engine = get(client).json()["engine"]
    # Still cancellable, but no refund left: no deadline to show. Kills free_until without the
    # refund_pct guard (an anchor minus 120h, already in the past).
    assert (engine["can_cancel"], engine["refund_pct"], engine["free_until"]) == (True, 0, None)
    far = post(
        client,
        "/reschedule",
        reschedule_body(booking_id, at("15:00", day=DAY + timedelta(days=20)), 0),
    )
    assert far.status_code == 200, far.json()
    assert far.json()["engine"]["refund_pct"] == 0

    view = get(client).json()
    assert view["engine"]["refund_pct"] == 0
    cancelled = post(client, "/cancel", cancel_body(booking_id, 0))
    assert (cancelled.status_code, cancelled.json()["engine"]["refund_pct"]) == (200, 0)


def first_slot_after(lead: timedelta) -> str:
    """The first half-hour start at least `lead` from now inside the fixture's 09:00-17:00 hours
    (a 30-minute service, so 16:30 is the last start). From lead=30h this is always under 48h."""
    zone = ZoneInfo(ZONE)
    local = (datetime.now(UTC) + lead).astimezone(zone)
    start = local.replace(minute=0, second=0, microsecond=0)
    while start < local or not time(9) <= start.time() <= time(16, 30):
        start += timedelta(minutes=30)
    return start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# 17b. fence (R4). free=48h, cutoff=24h, max=2: book A 20 days out, move earlier to B (30-48h
# out, so B is already inside its no-refund window but still movable), move to C 30 days out,
# cancel -> refund 0, and earliest_starts_at = B. Kills min(original, current) (= A, a full
# refund) and a RESCHEDULE without LEAST (earliest stays A, or jumps to C).
def test_moving_near_then_far_again_keeps_the_near_refund_anchor(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    a = at("10:00", day=TODAY + timedelta(days=20))
    b = first_slot_after(timedelta(hours=30))
    c = at("10:00", day=TODAY + timedelta(days=30))
    lead_b = datetime.fromisoformat(b) - datetime.now(UTC)
    assert timedelta(hours=24) < lead_b < timedelta(hours=48)
    booking_id = confirmed_booking(app, people.a, ready, starts_at=a)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)
    assert get(client).json()["engine"]["refund_pct"] == 100

    assert post(client, "/reschedule", reschedule_body(booking_id, b, 0)).status_code == 200
    moved = post(client, "/reschedule", reschedule_body(booking_id, c, 1))
    assert moved.status_code == 200, moved.json()

    with tenant_context(people.a) as session:
        earliest = session.scalar(
            text("SELECT earliest_starts_at FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    assert earliest == datetime.fromisoformat(b)
    assert get(client).json()["engine"]["refund_pct"] == 0
    cancelled = post(client, "/cancel", cancel_body(booking_id, 0))
    assert (cancelled.status_code, cancelled.json()["engine"]["refund_pct"]) == (200, 0)


# fence (owner ruling). free_until is the refund anchor (earliest_starts_at) minus
# free_cancellation_hours: book 20 days out, move 10 days LATER, and the deadline stays with the
# first start. Kills computing it from the current starts_at.
def test_free_until_is_the_earliest_start_minus_the_free_window(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    assert put_settings(owner, {"free_cancellation_hours": 48}).status_code == 200
    first = at("10:00", day=TODAY + timedelta(days=20))
    booking_id = confirmed_booking(app, people.a, ready, starts_at=first)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)
    later = at("10:00", day=TODAY + timedelta(days=30))
    moved = post(client, "/reschedule", reschedule_body(booking_id, later, 0))
    assert moved.status_code == 200, moved.json()

    deadline = datetime.fromisoformat(first) - timedelta(hours=48)
    for view in (moved.json(), get(client).json()):
        assert view["engine"]["refund_pct"] == 100
        assert datetime.fromisoformat(view["engine"]["free_until"]) == deadline


# fence (demo blocker, ZIF-136). The read is served at the prefix itself, with no trailing slash:
# a slash-only route answers the frontend's slash-less call with a 307 whose Location carries the
# internal API origin. Kills the route back at "/".
def test_the_read_is_served_without_a_trailing_slash(
    people: People, app: FastAPI, ready: str
) -> None:
    client = linked(app, people.a, confirmed_booking(app, people.a, ready))
    response = client.get(URL, follow_redirects=False)
    assert response.status_code == 200, (response.status_code, response.headers.get("location"))


# fence (ZIF-136, redirect half). With a trailing slash every route is a plain 404, never a 3xx:
# Starlette's redirect_slashes answers 307 with a Location on the INTERNAL API origin. Kills
# create_app() without redirect_slashes=False. /api/services/ stands for the older routes.
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"{URL}/"),
        ("GET", f"{URL}/availability/"),
        ("POST", f"{URL}/session/"),
        ("POST", f"{URL}/cancel/"),
        ("POST", f"{URL}/reschedule/"),
        ("POST", f"{URL}/consents/"),
        ("GET", "/api/services/"),
    ],
)
def test_a_trailing_slash_is_404_and_never_a_redirect(
    people: People, app: FastAPI, ready: str, method: str, path: str
) -> None:
    client = linked(app, people.a, confirmed_booking(app, people.a, ready))
    body: dict[str, Any] | None = {} if method == "POST" else None
    response = client.request(method, path, json=body, follow_redirects=False)
    assert (response.status_code, response.headers.get("location")) == (404, None)


# fence. The picker offers nothing the reschedule would refuse: no reschedules left, or the
# service archived since booking, gives an empty slot list (the page shows "no times"). Kills a
# GET /availability that skips decide() or reads buffer_minutes from an archived service.
def test_availability_offers_nothing_the_reschedule_would_refuse(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    day = {"from": DAY.isoformat(), "to": DAY.isoformat()}
    assert put_settings_max(owner, 0) == 200
    spent = confirmed_booking(app, people.a, ready, starts_at=at("10:00"))
    assert patch(owner, spent, "confirmed").status_code == 200
    assert put_settings_max(owner, 3) == 200
    # Its own service, so archiving it leaves `spent`'s service live: each leg kills its own branch.
    doomed = new_service(owner)
    assign(people.a, doomed, member_id(people.a, people.both))
    archived = confirmed_booking(app, people.a, doomed, starts_at=at("11:00"))
    assert patch(owner, archived, "confirmed").status_code == 200
    archived_client = linked(app, people.a, archived)
    assert get(archived_client, "/availability", **day).json()["slots"]  # offered while live

    with tenant_context(people.a) as session:
        session.execute(
            text("UPDATE services SET archived_at = now() WHERE id = :id"), {"id": doomed}
        )

    for client in (linked(app, people.a, spent), archived_client):
        response = get(client, "/availability", **day)
        assert (response.status_code, response.json()["slots"]) == (200, [])


# fence (D9). The picker leaves the booking's own slot out of "booked": a 10:00-10:30 booking is
# offered 10:00 again, a slot that overlaps only itself. Kills GET /availability without exclude=.
def test_availability_offers_slots_overlapping_the_booking_itself(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    booking_id = confirmed_booking(app, people.a, ready, starts_at=at("10:00"))
    assert patch(owner, booking_id, "confirmed").status_code == 200

    response = get(
        linked(app, people.a, booking_id),
        "/availability",
        **{"from": DAY.isoformat(), "to": DAY.isoformat()},
    )

    slots = {datetime.fromisoformat(s) for s in response.json()["slots"]}
    assert datetime.fromisoformat(at("10:00")) in slots


# fence (D10). The create/accept confirmation still queued when the client reschedules sends
# nothing: its payload carries the start it was about, so the starts_at gate skips it. Kills a
# confirmation payload without starts_at (a second, stale "confirmed" email with the old time).
@pytest.mark.parametrize("path", ["create", "accept"])
def test_a_queued_confirmation_run_after_a_reschedule_sends_nothing(
    people: People, app: FastAPI, ready: str, owner: TestClient, app_engine: Engine, path: str
) -> None:
    if path == "create":
        save_setting(people.a, "auto_confirm", True)
        response = post_booking(new_client(app), people.a, ready, starts_at=at("10:00"))
        assert response.json()["status"] == "confirmed", response.json()
        booking_id = response.json()["id"]
    else:
        booking_id = confirmed_booking(app, people.a, ready, starts_at=at("10:00"))
        assert patch(owner, booking_id, "confirmed").status_code == 200
    (original,) = [
        j
        for j in booking_jobs(app_engine, people.a, booking_id)
        if j.payload["template"] == "booking_confirmed"
    ]
    moved = post(
        linked(app, people.a, booking_id),
        "/reschedule",
        reschedule_body(booking_id, at("11:00"), 0),
    )
    assert moved.status_code == 200

    mail.send_booking(original)

    assert _search(_client_email(people.a, booking_id)) == []


# 15. fence. A double submit with the same echoed count: one 200, one 409 changed. And RESCHEDULE
# run directly with a stale :seen updates nothing: the route's own echo check (and the lock) would
# otherwise hide a qualifier without `reschedule_count = :seen`.
def test_a_stale_reschedule_count_changes_nothing(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    booking_id = confirmed_booking(app, people.a, ready, starts_at=at("10:00"))
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)

    first = post(client, "/reschedule", reschedule_body(booking_id, at("11:00"), 0))
    second = post(client, "/reschedule", reschedule_body(booking_id, at("12:00"), 0))

    assert first.status_code == 200
    assert (second.status_code, second.json()) == (409, {"code": "changed"})
    with tenant_context(people.a) as session:
        stale = session.execute(
            RESCHEDULE, {"id": booking_id, "new": datetime.fromisoformat(at("13:00")), "seen": 0}
        ).first()
        row = session.execute(
            text("SELECT starts_at, reschedule_count FROM bookings WHERE id = :id"),
            {"id": booking_id},
        ).one()
    assert stale is None
    assert (row.starts_at, row.reschedule_count) == (datetime.fromisoformat(at("11:00")), 1)


# 13 (rest). Time off, a buffer tail and beyond the horizon: every one of them only offered()
# knows about, none of them is something the EXCLUDE constraint would catch.
def test_reschedule_into_time_off_a_buffer_tail_or_past_the_horizon_is_slot_unavailable(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    save_setting(people.a, "buffer_pct", 20)
    booking_id = confirmed_booking(app, people.a, ready, starts_at=at("10:00"))
    assert patch(owner, booking_id, "confirmed").status_code == 200
    other = confirmed_booking(app, people.a, ready, starts_at=at("14:00"))
    assert patch(owner, other, "confirmed").status_code == 200
    next_day = DAY + timedelta(days=1)
    with tenant_context(people.a) as session:
        session.execute(
            text("""
            INSERT INTO time_off (tenant_id, member_id, first_day, last_day, reason)
            VALUES (current_setting('app.tenant_id')::uuid, :m, :d, :d, 'Holiday')
            """),
            {"m": member_id(people.a, people.both), "d": next_day},
        )
    client = linked(app, people.a, booking_id)

    time_off = post(client, "/reschedule", reschedule_body(booking_id, at("10:00", next_day), 0))
    buffer_tail = post(client, "/reschedule", reschedule_body(booking_id, at("14:30"), 0))
    assert put_settings(owner, {"booking_horizon_days": 1}).status_code == 200
    horizon = post(client, "/reschedule", reschedule_body(booking_id, at("11:00"), 0))

    for answer in (time_off, buffer_tail, horizon):
        assert (answer.status_code, answer.json()) == (409, {"code": "slot_unavailable"})


# 31. guard. A -> B -> A: the move back re-enqueues A's reminder as a dedupe no-op, B's skips
# itself on the starts_at gate, and the client gets exactly one reminder.
def test_there_and_back_again_sends_one_reminder(
    people: People, app: FastAPI, ready: str, owner: TestClient, app_engine: Engine
) -> None:
    booking_id = confirmed_booking(app, people.a, ready, starts_at=at("10:00"))
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)
    assert (
        post(client, "/reschedule", reschedule_body(booking_id, at("11:00"), 0)).status_code == 200
    )
    assert (
        post(client, "/reschedule", reschedule_body(booking_id, at("10:00"), 1)).status_code == 200
    )

    reminders = [
        j
        for j in booking_jobs(app_engine, people.a, booking_id)
        if j.payload["template"] == "booking_reminder"
    ]
    assert len(reminders) == 2  # A once, B once
    for job in reminders:
        mail.send_booking(job)

    assert len(_search(_client_email(people.a, booking_id))) == 1


# 32. guard. Two reschedules in a row: the :r1 client confirmation and the :r1 merchant email,
# run after :r2, send nothing (the starts_at gate). The :r2 merchant email shows old and new time.
def test_an_older_reschedule_email_run_late_sends_nothing(
    people: People, app: FastAPI, ready: str, owner: TestClient, app_engine: Engine
) -> None:
    booking_id = confirmed_booking(app, people.a, ready, starts_at=at("10:00"))
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)
    assert (
        post(client, "/reschedule", reschedule_body(booking_id, at("10:30"), 0)).status_code == 200
    )
    assert (
        post(client, "/reschedule", reschedule_body(booking_id, at("15:00"), 1)).status_code == 200
    )
    moves = [
        j
        for j in booking_jobs(app_engine, people.a, booking_id)
        if j.payload["template"] in ("booking_confirmed", "booking_client_rescheduled")
        and "starts_at" in j.payload
    ]
    first = [j for j in moves if same_instant(j.payload["starts_at"], at("10:30"))]
    second = [j for j in moves if same_instant(j.payload["starts_at"], at("15:00"))]
    assert len(first) == len(second) == 2  # the client's and the one merchant's, each time

    for job in first:
        mail.send_booking(job)
    client_email = _client_email(people.a, booking_id)
    owner_email = email_of_user(people.a, people.both)
    assert _search(client_email) == []
    assert _search(owner_email) == []

    for job in second:
        mail.send_booking(job)
    assert len(_search(client_email)) == 1
    (merchant_text,) = texts_to(owner_email)
    assert "10:30" in merchant_text
    assert "15:00" in merchant_text


def email_of_user(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    with tenant_context(tenant_id) as session:
        found: str = session.scalar(text("SELECT email FROM users WHERE id = :id"), {"id": user_id})
    return found


# ---------------------------------------------------------------------------
# Consent confirmation (D11).
# ---------------------------------------------------------------------------


# 25. fence. A grant at booking, then a later withdrawal, then confirm -> no new grant row.
def test_confirm_skips_a_purpose_withdrawn_after_booking(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    booking_id = confirmed_booking(
        app, people.a, ready, consents={"marketing_email": True, "sms": True}
    )
    assert patch(owner, booking_id, "confirmed").status_code == 200
    with tenant_context(people.a) as session:
        client_id = session.scalar(
            text("SELECT client_id FROM bookings WHERE id = :id"), {"id": booking_id}
        )
        session.execute(
            text("""
            INSERT INTO consents (tenant_id, client_id, purpose, granted, text_shown,
                                  policy_version, source)
            VALUES (current_setting('app.tenant_id')::uuid, :c, 'sms', false, 'withdrawn',
                    '2026-09-01', 'merchant')
            """),
            {"c": client_id},
        )
    client = linked(app, people.a, booking_id)
    before = get(client).json()
    assert set(before["pending_consents"]) == {"marketing_email"}

    response = post(client, "/consents", {"booking_id": booking_id})

    assert response.status_code == 200
    with tenant_context(people.a) as session:
        recorded = (
            session.execute(
                text(
                    "SELECT purpose FROM consents WHERE client_id = :c AND source = 'booking_page'"
                ),
                {"c": client_id},
            )
            .scalars()
            .all()
        )
    assert list(recorded) == ["marketing_email"]


# fence (D11). A withdrawal made BEFORE the booking's grant does not override it: the grant is
# the newer word. Kills WITHDRAWN_SINCE without `created_at > :since`.
def test_a_withdrawal_older_than_the_grant_does_not_suppress_it(
    people: People, app: FastAPI, ready: str, owner: TestClient, migrate_engine: Engine
) -> None:
    booking_id = confirmed_booking(app, people.a, ready, consents={"sms": True})
    assert patch(owner, booking_id, "confirmed").status_code == 200
    # As the migrate role: the app role may not back-date created_at, and no route can.
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text("""
            INSERT INTO consents (tenant_id, client_id, purpose, granted, text_shown,
                                  policy_version, source, created_at)
            SELECT tenant_id, client_id, 'sms', false, 'withdrawn', '2026-09-01', 'merchant',
                   now() - interval '1 day'
            FROM bookings WHERE id = :id
            """),
            {"id": booking_id},
        )

    assert get(linked(app, people.a, booking_id)).json()["pending_consents"] == ["sms"]


# 26. fence. Confirming twice: one set of rows, one event.
def test_confirming_twice_is_idempotent(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    booking_id = confirmed_booking(app, people.a, ready, consents={"marketing_email": True})
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)

    first = post(client, "/consents", {"booking_id": booking_id})
    assert first.status_code == 200
    second = post(client, "/consents", {"booking_id": booking_id})
    assert (second.status_code, second.json()["code"]) == (409, "nothing_to_confirm")

    with tenant_context(people.a) as session:
        consent_rows = session.scalar(
            text("SELECT count(*) FROM consents WHERE source = 'booking_page'")
        )
        confirmed_events = session.scalar(
            text(
                "SELECT count(*) FROM booking_events WHERE booking_id = :id "
                "AND event = 'consent_confirmed'"
            ),
            {"id": booking_id},
        )
    assert (consent_rows, confirmed_events) == (1, 1)


# fence. Two confirms at once: the second waits on the tenant lock behind the first, then finds
# nothing pending -- one set of grant rows, one consent_confirmed event. Kills /consents without
# bookings.LOCK (both read "pending" before either commits, and both record).
def test_two_concurrent_confirms_record_once(
    people: People,
    app: FastAPI,
    ready: str,
    owner: TestClient,
    app_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booking_id = confirmed_booking(app, people.a, ready, consents={"marketing_email": True})
    assert patch(owner, booking_id, "confirmed").status_code == 200
    real_pending = booking_links.pending_consents
    holding, release, paused_once = threading.Event(), threading.Event(), threading.Event()

    def paused_pending(*args: Any) -> list[Any]:
        result = real_pending(*args)
        if not paused_once.is_set():
            paused_once.set()
            holding.set()
            release.wait(timeout=10)
        return result

    monkeypatch.setattr(booking_links, "pending_consents", paused_pending)
    token = mint(people.a, booking_id)
    results: list[int] = []

    def confirm() -> None:
        response = post(with_cookie(app, people.a, token), "/consents", {"booking_id": booking_id})
        results.append(response.status_code)

    first = threading.Thread(target=confirm)
    first.start()
    assert holding.wait(timeout=10)
    second = threading.Thread(target=confirm)
    second.start()
    # Without the lock the second never waits: let the timeout pass and the row counts below fail.
    with contextlib.suppress(AssertionError):
        wait_until_blocked(app_engine, 1)
    release.set()
    for thread in (first, second):
        thread.join(timeout=10)
    assert not first.is_alive() and not second.is_alive()

    with tenant_context(people.a) as session:
        consent_rows = session.scalar(
            text("SELECT count(*) FROM consents WHERE source = 'booking_page'")
        )
        confirmed_events = session.scalar(
            text(
                "SELECT count(*) FROM booking_events WHERE booking_id = :id "
                "AND event = 'consent_confirmed'"
            ),
            {"id": booking_id},
        )
    assert (sorted(results), consent_rows, confirmed_events) == ([200, 409], 1, 1)


# 28. guard. The 11th exchange in 15 minutes, and the 21st write in an hour, both 429.
def test_rate_limits_trip_before_any_lookup(people: People, app: FastAPI, ready: str) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    address = fresh_address()
    for _ in range(10):
        client = new_client(app, address)
        post(client, "/session", {"token": f"{people.a}.{secrets.token_urlsafe(32)}"})
    live = f"{people.a}.{mint(people.a, booking_id)}"
    tripped = post(new_client(app, address), "/session", {"token": live})
    assert tripped.status_code == 429  # a live token: the limit answers before the lookup

    writer = fresh_address()
    for _ in range(20):
        post(new_client(app, writer), "/consents", {"booking_id": booking_id})
    holder = new_client(app, writer)
    holder.cookies.set(COOKIE, live)
    refund_pct = get(linked(app, people.a, booking_id)).json()["engine"]["refund_pct"]
    assert post(holder, "/cancel", cancel_body(booking_id, refund_pct)).status_code == 429
    with tenant_context(people.a) as session:
        status = session.scalar(
            text("SELECT status FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    assert status == "confirmed"


# 29. guard. The logs and spans of a whole flow (exchange, read, availability, consents,
# reschedule, cancel) hold neither the token nor its hash, in any spelling.
def test_no_log_line_or_span_carries_the_token_or_its_hash(
    people: People,
    app: FastAPI,
    ready: str,
    caplog: pytest.LogCaptureFixture,
    spans: Callable[[], list[ReadableSpan]],
) -> None:
    booking_id = confirmed_booking(
        app, people.a, ready, starts_at=at("10:00"), consents={"marketing_email": True}
    )
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    token = mint(people.a, booking_id)
    digest = hashlib.sha256(token.encode()).digest()
    client = new_client(app)

    # DEBUG is the dev default (observability rules): nothing may leak at the chattiest level.
    with caplog.at_level(logging.DEBUG), caplog.at_level(logging.DEBUG, logger="app"):
        assert post(client, "/session", {"token": f"{people.a}.{token}"}).status_code == 204
        view = get(client)
        assert view.status_code == 200
        day = {"from": DAY.isoformat(), "to": DAY.isoformat()}
        assert get(client, "/availability", **day).status_code == 200
        assert post(client, "/consents", {"booking_id": booking_id}).status_code == 200
        moved = post(client, "/reschedule", reschedule_body(booking_id, at("11:00"), 0))
        assert moved.status_code == 200
        refund_pct = moved.json()["engine"]["refund_pct"]
        assert post(client, "/cancel", cancel_body(booking_id, refund_pct)).status_code == 200

    logged = "\n".join(repr(vars(record)) for record in caplog.records)
    traced = "\n".join(
        repr((s.name, s.attributes, [(e.name, e.attributes) for e in s.events])) for s in spans()
    )
    assert "booking cancelled by client" in logged
    assert "booking rescheduled" in logged
    assert traced
    for spelling in (token, digest.hex(), repr(digest), base64.b64encode(digest).decode()):
        assert spelling not in logged
        assert spelling not in traced
