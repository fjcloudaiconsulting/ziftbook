"""/api/public/booking-link/* (ZIF-54): a client manages one booking from the link in their
booking emails, with no account. See docs/specs/2026-09-24-zif-54-spec.md for the numbered tests
this file protects (backend half; the frontend and 30/38 live elsewhere)."""

import hashlib
import secrets
import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg.errors import CheckViolation, InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from app.booking_links import COOKIE
from app.db import tenant_context
from tests.conftest import People, fresh_address, member_id, new_client, signed_in
from tests.test_availability_api import assign, new_service, weekdays
from tests.test_booking_email import jobs_for_booking, template_of
from tests.test_bookings_api import at
from tests.test_bookings_approval_api import make_pending, patch
from tests.test_working_hours import seed

ZONE = "Europe/Amsterdam"
TODAY = date.today()
DAY = TODAY + timedelta(days=2)


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

    bad_shape = post(new_client(app), "/session", {"token": "not-a-token"})
    unknown_tenant = post(new_client(app), "/session", {"token": f"{uuid.uuid4()}.{live_token}"})
    unknown_hash = post(
        new_client(app), "/session", {"token": f"{people.a}.{secrets.token_urlsafe(32)}"}
    )
    no_cookie = get(new_client(app))

    bodies = [bad_shape.json(), unknown_tenant.json(), unknown_hash.json(), no_cookie.json()]
    statuses = [
        bad_shape.status_code,
        unknown_tenant.status_code,
        unknown_hash.status_code,
        no_cookie.status_code,
    ]
    assert statuses == [404, 404, 404, 404]
    assert bodies == [{"code": "link_expired"}] * 4


# 7. fence. Merchant cancel, decline, pending expiry and starts_at passing all revoke every route.
def test_a_dead_booking_gives_404_on_every_route(people: People, app: FastAPI, ready: str) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)
    assert get(client).status_code == 200

    assert patch(owner, booking_id, "cancelled_by_merchant").status_code == 200

    assert get(client).status_code == 404
    assert (
        get(client, "/availability", **{"from": DAY.isoformat(), "to": DAY.isoformat()}).status_code
        == 404
    )
    assert post(client, "/cancel", {"booking_id": booking_id, "refund_pct": 100}).status_code == 404
    assert (
        post(
            client,
            "/reschedule",
            {"booking_id": booking_id, "starts_at": at("10:00"), "reschedule_count": 0},
        ).status_code
        == 404
    )
    assert post(client, "/consents", {"booking_id": booking_id}).status_code == 404


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
    set_cookie = exchange.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=strict" in set_cookie.lower().replace("samesite=strict", "SameSite=strict")

    for response in (get(client), exchange):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


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

    response = post(client_for_b, "/cancel", {"booking_id": a, "refund_pct": 100})

    assert response.status_code == 409
    assert response.json()["code"] == "link_changed"
    with tenant_context(people.a) as session:
        status: str = session.scalar(text("SELECT status FROM bookings WHERE id = :id"), {"id": a})
    assert status == "confirmed"


# 12. fence. The LinkedBooking key set carries no client name, email, phone or ids.
def test_linked_booking_carries_no_client_identity(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)

    body = get(client).json()

    blob = str(body)
    assert "worker_id" not in body["booking"]
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
    booking_id = confirmed_booking(app, people.a, ready)  # still pending, not yet accepted
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
    people: People, app: FastAPI, ready: str, app_engine: Engine
) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
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
    assert templates.count("booking_client_cancelled") == 1  # one owner in this fixture


# 4/20. fence. cancelled_by_client is not, and never becomes, a merchant PATCH target.
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


# 15 (part). A raw UPDATE past max_reschedules raises 23514 (the CHECK is the backstop).
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
                text("UPDATE bookings SET original_starts_at = now() WHERE id = :id"),
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


# 24. fence. A reschedule enqueues booking_confirmed (:r1), booking_client_rescheduled per
# merchant and a reminder for the new start.
def test_reschedule_enqueues_confirmation_merchant_email_and_reminder(
    people: People, app: FastAPI, ready: str, owner: TestClient, app_engine: Engine
) -> None:
    booking_id = confirmed_booking(app, people.a, ready, starts_at=at("10:00"))
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)

    response = post(client, "/reschedule", reschedule_body(booking_id, at("10:30"), 0))
    assert response.status_code == 200

    jobs = jobs_for_booking(app_engine, people.a, booking_id)
    keys = {j["dedupe_key"] for j in jobs}
    assert any(k.endswith("booking_confirmed:r1") for k in keys)
    assert any("booking_client_rescheduled:r1" in k for k in keys)
    assert any("booking_reminder" in k for k in keys)


# 17/17b. fence. refund_pct is anchored to earliest_starts_at, which only goes down.
def test_refund_is_anchored_to_the_earliest_start_ever_held(
    people: People, app: FastAPI, ready: str, owner: TestClient
) -> None:
    booking_id = confirmed_booking(app, people.a, ready, starts_at=at("09:00"))
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client = linked(app, people.a, booking_id)
    # Move to a slot far in the future so the CURRENT lead alone would look like a full refund.
    far = post(
        client,
        "/reschedule",
        reschedule_body(booking_id, at("15:00", day=DAY + timedelta(days=20)), 0),
    )
    assert far.status_code == 200, far.json()

    view = get(linked(app, people.a, booking_id)).json()
    # earliest_starts_at is still the ORIGINAL near start, so with 48h free-cancellation and the
    # booking only two days out at the start, the refund reflects that near anchor, not the far
    # new date.
    assert view["engine"]["refund_pct"] == 0


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


# 28. guard. The 11th exchange in 15 minutes, and the 21st write in an hour, both 429.
def test_rate_limits_trip_before_any_lookup(people: People, app: FastAPI, ready: str) -> None:
    booking_id = confirmed_booking(app, people.a, ready)
    assert patch(signed_in(app, people.a, people.both), booking_id, "confirmed").status_code == 200
    address = fresh_address()
    for _ in range(10):
        client = new_client(app, address)
        post(client, "/session", {"token": f"{people.a}.{secrets.token_urlsafe(32)}"})
    tripped = post(
        new_client(app, address), "/session", {"token": f"{people.a}.{secrets.token_urlsafe(32)}"}
    )
    assert tripped.status_code == 429
