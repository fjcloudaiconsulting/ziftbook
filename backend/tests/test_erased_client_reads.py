"""The console no longer shows an erased client (ZIF-58, spec 2.3): the client list skips it,
PATCH and consents answer 404, it cannot be booked, the booking lists mark it with client_erased,
and the team gets no booking mail naming it. Erased rows are written here as the erase function
will write them, as the migrate role: the app role cannot write erased_at (0063)."""

import json
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text

from app import bookings
from app.db import tenant_context
from app.main import create_app, openapi_document
from tests.conftest import (
    People,
    events,
    fresh_email,
    member_id,
    migrate_tenant_context,
    save_setting,
    signed_in,
    wait_until_blocked,
)
from tests.test_availability_api import assign, new_service, weekdays
from tests.test_booking_email import outbox_for, run_send_booking
from tests.test_bookings_api import at
from tests.test_bookings_approval_api import make_pending
from tests.test_bookings_db import insert_booking
from tests.test_clients_db import seed_client
from tests.test_working_hours import seed

# What the erase function blanks of a client and of its bookings (spec 1.1, 1.3).
ERASE_BOOKINGS = text("""
UPDATE bookings SET client_name = NULL, client_phone = NULL, client_message = NULL,
                    decline_message = NULL, checkout_hash = NULL
WHERE client_id = :id
""")
ERASE_CLIENT = text("""
UPDATE clients SET erased_at = now(), name = '', user_id = NULL, email = NULL, phone = NULL,
                   locale = NULL, client_note = NULL, internal_note = NULL
WHERE id = :id
""")


def erase_in(conn: Connection, client_id: object) -> None:
    conn.execute(ERASE_BOOKINGS, {"id": client_id})
    conn.execute(ERASE_CLIENT, {"id": client_id})


def erase_by_hand(migrate_engine: Engine, tenant_id: uuid.UUID, client_id: object) -> None:
    """Erase a client as the erase function will, as the migrate role, committed."""
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        erase_in(conn, client_id)


def client_of(tenant_id: uuid.UUID, booking_id: str) -> uuid.UUID:
    with tenant_context(tenant_id) as session:
        found: uuid.UUID = session.scalar(
            text("SELECT client_id FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    return found


def consent_rows(tenant_id: uuid.UUID, client_id: object) -> int:
    with tenant_context(tenant_id) as session:
        count: int = session.scalar(
            text("SELECT count(*) FROM consents WHERE client_id = :id"), {"id": client_id}
        )
    return count


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture
def added(owner: TestClient) -> Iterator[tuple[str, str]]:
    """Two clients made in the console: one to erase, one that stays."""
    gone = owner.post("/api/clients", json={"name": "Gone Client", "email": fresh_email()})
    kept = owner.post("/api/clients", json={"name": "Kept Client", "email": fresh_email()})
    assert (gone.status_code, kept.status_code) == (201, 201)
    yield gone.json()["id"], kept.json()["id"]


# ZIF-58 F7 FENCE. Wrong impl: the list with no erased_at filter (the tombstone is listed, with
# its blank name).
def test_the_client_list_skips_an_erased_client(
    people: People, owner: TestClient, migrate_engine: Engine, added: tuple[str, str]
) -> None:
    gone, kept = added
    erase_by_hand(migrate_engine, people.a, gone)

    listed = [c["id"] for c in owner.get("/api/clients").json()]
    assert kept in listed
    assert gone not in listed
    # Paging past the tombstone does not bring it back either.
    paged = [c["id"] for c in owner.get("/api/clients", params={"before": kept}).json()]
    assert gone not in paged
    assert [c["id"] for c in owner.get("/api/clients", params={"q": "Client"}).json()] == [kept]


# ZIF-58 F8 FENCE. Wrong impl: PATCH's FOR NO KEY UPDATE select, or the consents select, without
# the erased_at filter (PATCH then reaches the final trigger, a 500; consents then adds a row to a
# tombstone). Nothing is written and nothing recorded.
def test_an_erased_client_cannot_be_edited_or_given_a_consent(
    people: People, owner: TestClient, migrate_engine: Engine, added: tuple[str, str]
) -> None:
    gone, _ = added
    erase_by_hand(migrate_engine, people.a, gone)
    changed = len(events(migrate_engine, tenant_id=people.a, action="client_changed"))
    recorded = len(events(migrate_engine, tenant_id=people.a, action="consent_recorded"))

    patched = owner.patch(f"/api/clients/{gone}", json={"client_note": "Back again"})
    consented = owner.post(
        f"/api/clients/{gone}/consents",
        json={"policy_version": "2026-09-01", "purposes": {"sms": True}},
    )

    assert (patched.status_code, patched.json()["code"]) == (404, "not_found")
    assert (consented.status_code, consented.json()["code"]) == (404, "not_found")
    assert consent_rows(people.a, gone) == 0
    assert len(events(migrate_engine, tenant_id=people.a, action="client_changed")) == changed
    assert len(events(migrate_engine, tenant_id=people.a, action="consent_recorded")) == recorded


# ZIF-58 F9 FENCE (review finding 9). Wrong impl: the consents select without FOR SHARE. The
# consent's foreign key takes only KEY SHARE on the client, which an erasure's NO KEY UPDATE does
# not block, so without FOR SHARE the consent is written on a row being erased and lands on the
# tombstone. With it, the POST waits for the erasure, re-reads the row and answers 404.
@pytest.mark.parametrize("route", ["consents", "patch"])
def test_a_write_waits_for_an_erasure_in_flight_then_finds_nothing(
    people: People,
    owner: TestClient,
    migrate_engine: Engine,
    app_engine: Engine,
    added: tuple[str, str],
    route: str,
) -> None:
    gone, _ = added
    answered: dict[str, Any] = {}

    def write() -> None:
        if route == "consents":
            answered["r"] = owner.post(
                f"/api/clients/{gone}/consents",
                json={"policy_version": "2026-09-01", "purposes": {"sms": True}},
            )
        else:
            answered["r"] = owner.patch(f"/api/clients/{gone}", json={"client_note": "Late"})

    writer = threading.Thread(target=write)
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        erase_in(conn, gone)  # holds the client's row lock until this block commits
        writer.start()
        wait_until_blocked(app_engine, 1)
    writer.join(timeout=10)
    assert not writer.is_alive()

    assert (answered["r"].status_code, answered["r"].json()["code"]) == (404, "not_found")
    assert consent_rows(people.a, gone) == 0


@pytest.fixture
def clean_outbox(people: People, migrate_engine: Engine) -> Iterator[None]:
    yield
    # As the migrate role: the app role has no DELETE on email_outbox (ZB-10).
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        conn.execute(text("DELETE FROM email_outbox"))


@pytest.fixture
def ready(people: People, owner: TestClient) -> str:
    """A 30-minute service performed by the owner, who works 09:00-17:00 every day, on a published
    page (make_pending books through the public route)."""
    save_setting(people.a, "published", True)
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    return service_id


@pytest.fixture
def booked(app: FastAPI, people: People, owner: TestClient, ready: str) -> dict[str, str]:
    """Bookings two days out: a pending one and a confirmed one of a client then erased, and a
    pending one of a client that stays."""
    gone_email = fresh_email()
    gone = {"name": "Gone Booker", "email": gone_email, "message": "Gone message"}
    pending = make_pending(app, people.a, ready, starts_at=at("09:00"), **gone)
    confirmed = make_pending(app, people.a, ready, starts_at=at("10:00"), **gone)
    assert (
        owner.patch(f"/api/bookings/{confirmed}", json={"status": "confirmed"}).status_code == 200
    )
    live = make_pending(app, people.a, ready, starts_at=at("11:00"), name="Live Booker")
    assert client_of(people.a, pending) == client_of(people.a, confirmed)
    return {"pending": pending, "confirmed": confirmed, "live": live}


# ZIF-58 F10 FENCE. Wrong impl: the merchant booking's existence check without the erased_at filter
# (the tombstone is booked again).
def test_a_merchant_cannot_book_an_erased_client(
    people: People, owner: TestClient, migrate_engine: Engine, ready: str, added: tuple[str, str]
) -> None:
    gone, kept = added
    erase_by_hand(migrate_engine, people.a, gone)
    body = {
        "service_id": ready,
        "member_id": str(member_id(people.a, people.both)),
        "starts_at": at("12:00"),
    }

    refused = owner.post("/api/bookings", json={**body, "client_id": gone})

    assert (refused.status_code, refused.json()["code"]) == (422, "unknown_client")
    with tenant_context(people.a) as session:
        assert (
            session.scalar(
                text("SELECT count(*) FROM bookings WHERE client_id = :id"), {"id": gone}
            )
            == 0
        )
    # The same booking for a live client goes through: the refusal is the tombstone's alone.
    assert owner.post("/api/bookings", json={**body, "client_id": kept}).status_code == 201


def window() -> dict[str, str]:
    return {"from": at("00:00"), "to": at("23:59")}


# ZIF-58 F11 FENCE. Wrong impl: a reader that gives the name without client_erased (the console
# cannot tell an erased client from a blank name), or one that gives client_name null. The queue,
# the agenda and the detail each say client_name "" exactly when client_erased is true, and the
# detail carries nothing about the person.
def test_the_booking_lists_mark_an_erased_client(
    people: People, owner: TestClient, migrate_engine: Engine, booked: dict[str, str]
) -> None:
    erase_by_hand(migrate_engine, people.a, client_of(people.a, booked["pending"]))

    queue = {row["id"]: row for row in owner.get("/api/bookings/pending").json()}
    agenda = {row["id"]: row for row in owner.get("/api/bookings", params=window()).json()}
    detail = owner.get(f"/api/bookings/{booked['pending']}").json()
    live = owner.get(f"/api/bookings/{booked['live']}").json()

    for row in (queue[booked["pending"]], agenda[booked["confirmed"]], detail):
        assert (row["client_name"], row["client_erased"]) == ("", True)
    for row in (queue[booked["live"]], agenda[booked["live"]], live):
        assert (row["client_name"], row["client_erased"]) == ("Live Booker", False)
    assert {
        key: detail[key]
        for key in ("client_email", "client_phone", "client_message", "client_note")
    } == dict.fromkeys(("client_email", "client_phone", "client_message", "client_note"))


def schema(spec: dict[str, Any], name: str) -> dict[str, Any]:
    found: dict[str, Any] = spec["components"]["schemas"][name]
    return found


# ZIF-58 F11 FENCE, the contract (review finding 5): client_name stays a required, non-nullable
# string, client_erased a required boolean, and the field description says that "" goes with
# client_erased true. Wrong impl: client_name: str | None, or client_erased optional or absent.
@pytest.mark.parametrize("name", ["PendingOut", "AgendaOut", "BookingDetailOut", "InTheWay"])
def test_the_erased_client_pair_is_a_pinned_contract(name: str) -> None:
    # The app's own document: tests/test_openapi.py holds backend/openapi.json to it.
    spec = json.loads(openapi_document())
    found = schema(spec, name)
    client_name = found["properties"]["client_name"]
    assert client_name["type"] == "string"
    assert "anyOf" not in client_name
    assert found["properties"]["client_erased"]["type"] == "boolean"
    assert {"client_name", "client_erased"} <= set(found["required"])
    assert '""' in client_name["description"]
    assert "client_erased" in client_name["description"]


# ZIF-58 F12 FENCE. Wrong impl: today's InTheWay, with no client_erased (the console cannot tell
# the refused block's erased client from a blank name).
def test_a_refused_block_marks_an_erased_client(
    people: People, owner: TestClient, migrate_engine: Engine, booked: dict[str, str]
) -> None:
    erase_by_hand(migrate_engine, people.a, client_of(people.a, booked["pending"]))

    refused = owner.post(
        f"/api/members/{member_id(people.a, people.both)}/time-off",
        json={"starts_at": at("08:00"), "ends_at": at("12:00")},
    )

    assert (refused.status_code, refused.json()["code"]) == (409, "overlaps_bookings")
    named = {
        row["id"]: (row["client_name"], row["client_erased"]) for row in refused.json()["bookings"]
    }
    assert named == {
        booked["pending"]: ("", True),
        booked["confirmed"]: ("", True),
        booked["live"]: ("Live Booker", False),
    }


# ZIF-58 F13 FENCE. Wrong impl: no gate, so the team's mail about an erased client's booking is
# rendered with a blank $client and recorded in the outbox. The same mail for a live client is
# written, so the gate is the erasure's alone.
@pytest.mark.parametrize("template", ["booking_request", "booking_client_cancelled"])
def test_the_team_gets_no_booking_mail_for_an_erased_client(
    people: People,
    migrate_engine: Engine,
    booked: dict[str, str],
    clean_outbox: None,
    template: str,
) -> None:
    erase_by_hand(migrate_engine, people.a, client_of(people.a, booked["pending"]))
    status = "pending" if template == "booking_request" else "cancelled_by_client"
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        conn.execute(
            text("UPDATE bookings SET status = :s WHERE id = ANY(:ids)"),
            {"s": status, "ids": [booked["pending"], booked["live"]]},
        )

    erased = run_send_booking(people.a, booked["pending"], template, user_id=str(people.both))
    live = run_send_booking(people.a, booked["live"], template, user_id=str(people.both))

    assert outbox_for(people.a, erased.id) is None
    assert outbox_for(people.a, live.id) is not None


def started_booking(
    people: People, service_id: str, client_name: str, minutes_ago: int = 10
) -> tuple[str, uuid.UUID]:
    """A confirmed booking that started minutes ago, for a new client: what an erasure keeps."""
    client_id = seed_client(people.a, name=client_name, email=fresh_email())
    starts = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    with tenant_context(people.a) as session:
        booking_id = insert_booking(
            session,
            client_id=client_id,
            worker_id=member_id(people.a, people.both),
            service_id=uuid.UUID(service_id),
            starts_at=starts,
            ends_at=starts + timedelta(minutes=30),
        )
    return str(booking_id), client_id


def starts_of(tenant_id: uuid.UUID, booking_id: str) -> datetime:
    with tenant_context(tenant_id) as session:
        found: datetime = session.scalar(
            text("SELECT starts_at FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    return found


# ZIF-58 FENCE. Wrong impl: today's reschedule, which checks only the status. An erasure keeps a
# confirmed booking that has already started, so moving it to a future time would make a live
# appointment of a client who can no longer be told, and that no erasure can end again. The same
# move for a live client goes through.
def test_an_erased_clients_booking_cannot_be_moved(
    people: People, owner: TestClient, migrate_engine: Engine, ready: str
) -> None:
    gone, gone_client = started_booking(people, ready, "Gone Mover")
    kept, _ = started_booking(people, ready, "Kept Mover", minutes_ago=120)
    erase_by_hand(migrate_engine, people.a, gone_client)
    before = starts_of(people.a, gone)

    moved = owner.post(
        f"/api/bookings/{gone}/reschedule", json={"starts_at": at("12:00"), "override": True}
    )

    assert (moved.status_code, moved.json()["code"]) == (409, "invalid_transition")
    assert starts_of(people.a, gone) == before
    assert (
        owner.post(
            f"/api/bookings/{kept}/reschedule", json={"starts_at": at("13:00"), "override": True}
        ).status_code
        == 200
    )


# ZIF-58 FENCE. Wrong impl: any of the three moving statements (the console's MOVE, the client's
# RESCHEDULE and PENDING_MOVE) without the erased-client qualifier. The client's routes need a
# manage link, which an erasure deletes; the statements refuse on their own all the same.
@pytest.mark.parametrize("statement", ["MOVE", "RESCHEDULE", "PENDING_MOVE"])
def test_no_moving_statement_moves_an_erased_clients_booking(
    people: People, migrate_engine: Engine, ready: str, statement: str
) -> None:
    client_id = seed_client(people.a, name="Gone Statement", email=fresh_email())
    starts = datetime.now(UTC) + timedelta(days=5)
    pending = statement == "PENDING_MOVE"
    with tenant_context(people.a) as session:
        booking_id = insert_booking(
            session,
            client_id=client_id,
            worker_id=member_id(people.a, people.both),
            service_id=uuid.UUID(ready),
            starts_at=starts,
            ends_at=starts + timedelta(minutes=30),
            status="pending" if pending else "confirmed",
            expires_at=starts if pending else None,
            original_starts_at=starts,
            earliest_starts_at=starts,
        )
    erase_by_hand(migrate_engine, people.a, client_id)
    params = {
        "id": booking_id,
        "new": starts + timedelta(days=1),
        "seen": 0,
        "worker": member_id(people.a, people.both),
        "name": "Worker",
    }

    with tenant_context(people.a) as session:
        assert session.execute(getattr(bookings, statement), params).first() is None
    assert starts_of(people.a, str(booking_id)) == starts
