"""GET /api/bookings?from&to and GET /api/bookings/{id} (ZIF-57 PR1): the agenda range and the
booking detail with its history. See docs/specs/2026-09-30-zif-57-pr1-spec.md; F# and G# are that
spec's fences and guards."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    People,
    fresh_email,
    member_id,
    save_setting,
    signed_in,
)
from tests.test_availability_api import assign, new_service, weekdays
from tests.test_booking_links_api import linked, post, reschedule_body
from tests.test_bookings_api import at
from tests.test_bookings_approval_api import make_pending, patch
from tests.test_bookings_db import book, seed_service
from tests.test_clients_db import seed_client
from tests.test_working_hours import seed

DAY = datetime(2031, 3, 10, tzinfo=UTC)
FROM = DAY
TO = DAY + timedelta(days=1)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def window(start: datetime = FROM, end: datetime = TO) -> dict[str, str]:
    return {"from": iso(start), "to": iso(end)}


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture
def worker(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.only_a)


@pytest.fixture
def shop(people: People) -> dict[str, Any]:
    """A service, a client and the two members (owner `both`, worker `only_a`) of business a."""
    return {
        "service": seed_service(people.a),
        "client": seed_client(people.a, name="Ada", email=fresh_email()),
        "owner": member_id(people.a, people.both),
        "worker": member_id(people.a, people.only_a),
    }


def put(
    people: People,
    shop: dict[str, Any],
    who: str,
    starts_at: datetime,
    ends_at: datetime,
    **over: Any,
) -> str:
    return str(
        book(
            people.a,
            client_id=shop["client"],
            worker_id=shop[who],
            service_id=shop["service"],
            starts_at=starts_at,
            ends_at=ends_at,
            **over,
        )
    )


def hour(h: float, days: int = 0) -> datetime:
    return DAY + timedelta(days=days, hours=h)


def ids(response: Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()]


def range_(client: TestClient, **params: str) -> Response:
    return client.get("/api/bookings", params=params or window())


# F1: worker A's range omits worker B's booking; owner's includes both. Kills dropping :everyone.
def test_a_worker_sees_only_their_own_range_and_an_owner_everyones(
    people: People, shop: dict[str, Any], owner: TestClient, worker: TestClient
) -> None:
    mine = put(people, shop, "worker", hour(10), hour(11))
    theirs = put(people, shop, "owner", hour(10), hour(11))

    assert ids(range_(worker)) == [mine]
    assert sorted(ids(range_(owner))) == sorted([mine, theirs])


# F2: a worker's detail of someone else's booking is 403 owner_only; another business's id is 404.
# Kills a detail without may_manage, and a 403 that leaks across businesses.
def test_detail_is_403_for_a_colleagues_booking_and_404_across_businesses(
    app: FastAPI, people: People, shop: dict[str, Any], worker: TestClient
) -> None:
    theirs = put(people, shop, "owner", hour(10), hour(11))

    denied = worker.get(f"/api/bookings/{theirs}")
    assert (denied.status_code, denied.json()) == (403, {"code": "owner_only"})

    elsewhere = signed_in(app, people.b, people.both)
    assert elsewhere.get(f"/api/bookings/{theirs}").status_code == 404
    assert worker.get(f"/api/bookings/{uuid.uuid7()}").status_code == 404


# F3: a booking 23:30-00:30 that overlaps `from` is returned; one ending exactly at `from` is not.
# Kills a starts_at-only filter and a closed interval.
def test_the_window_is_half_open_at_from(
    people: People, shop: dict[str, Any], owner: TestClient
) -> None:
    across = put(people, shop, "owner", hour(23.5, -1), hour(0.5))
    touching = put(people, shop, "worker", hour(23, -1), hour(0))

    found = ids(range_(owner))

    assert across in found
    assert touching not in found


# F4: a booking starting exactly at `to` is not returned. Kills an inclusive `<=`.
def test_a_booking_starting_at_to_is_outside(
    people: People, shop: dict[str, Any], owner: TestClient
) -> None:
    starting_at_to = put(people, shop, "owner", hour(24), hour(25))
    last_minute = put(people, shop, "worker", hour(23.5), hour(24))

    found = ids(range_(owner))

    assert starting_at_to not in found
    assert last_minute in found


# F5: a pending whose hold has lapsed (unswept) is absent; a live one is present, with expires_at.
# Kills a status-only filter.
def test_a_lapsed_pending_is_hidden_and_a_live_one_carries_its_expiry(
    people: People, shop: dict[str, Any], owner: TestClient
) -> None:
    now = datetime.now(UTC)
    lapsed = put(
        people,
        shop,
        "owner",
        hour(10),
        hour(11),
        status="pending",
        expires_at=now - timedelta(hours=1),
    )
    live = put(
        people,
        shop,
        "worker",
        hour(10),
        hour(11),
        status="pending",
        expires_at=now + timedelta(hours=1),
    )

    response = range_(owner)

    assert ids(response) == [live]
    assert lapsed not in ids(response)
    assert response.json()[0]["expires_at"] is not None
    assert response.json()[0]["status"] == "pending"


# F6: only the statuses that still matter on an agenda. Kills filtering on OCCUPYING alone (drops
# no_show) and no filter at all.
def test_only_live_and_settled_statuses_are_listed(
    people: People, shop: dict[str, Any], owner: TestClient
) -> None:
    shown = {
        status: put(people, shop, "owner", hour(8 + i), hour(8.5 + i), status=status)
        for i, status in enumerate(("confirmed", "completed", "no_show"))
    }
    hidden = [
        put(
            people,
            shop,
            "owner",
            hour(14 + i),
            hour(14.5 + i),
            status=status,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        for i, status in enumerate(
            ("declined", "expired", "cancelled_by_client", "cancelled_by_merchant")
        )
    ]

    found = ids(range_(owner))

    assert sorted(found) == sorted(shown.values())
    assert not set(hidden) & set(found)
    by_status = {row["status"] for row in range_(owner).json()}
    assert by_status == {"confirmed", "completed", "no_show"}


# F7: the window is validated. Kills a missing check, and a bare datetime.
@pytest.mark.parametrize(
    "params",
    [
        window(FROM, FROM + timedelta(days=8, seconds=1)),
        window(FROM, FROM),
        window(TO, FROM),
    ],
)
def test_an_invalid_window_is_422(owner: TestClient, params: dict[str, str]) -> None:
    response = range_(owner, **params)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_window"})


def test_a_naive_datetime_is_422(owner: TestClient) -> None:
    response = range_(owner, **{"from": "2031-03-10T00:00:00", "to": "2031-03-11T00:00:00"})

    assert response.status_code == 422


# F8: a local week across DST is 7d+-1h and is fine; the cap is 8 days, inclusive. Kills a
# `<= 7 days` cap.
def test_a_week_across_dst_and_exactly_eight_days_are_accepted(owner: TestClient) -> None:
    assert range_(owner, **window(FROM, FROM + timedelta(days=7, hours=1))).status_code == 200
    assert range_(owner, **window(FROM, FROM + timedelta(days=8))).status_code == 200


# F9: a booking that started 11h59 before `from` and is still running is returned. Kills a
# look-back that is too tight.
def test_a_twelve_hour_booking_started_almost_twelve_hours_early_is_returned(
    people: People, shop: dict[str, Any], owner: TestClient
) -> None:
    start = FROM - timedelta(hours=11, minutes=59)
    long_one = put(people, shop, "owner", start, start + timedelta(hours=12))

    assert ids(range_(owner)) == [long_one]


@pytest.fixture
def ready(people: People, owner: TestClient) -> str:
    """A 30-minute service performed by the owner, who works 09:00-17:00 every day."""
    save_setting(people.a, "published", True)
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    return service_id


def history(response: Response) -> list[tuple[str, str, str | None]]:
    assert response.status_code == 200, response.text
    return [(e["event"], e["actor"], e["actor_name"]) for e in response.json()["history"]]


# F10 (+ F11's first half): the public POST still writes `created` through the app role, and a
# PATCH's event carries the signed-in person. Kills a write site that does not pass the actor, and
# coalesce(display_name, name) read the wrong way round.
def test_history_names_who_confirmed(
    app: FastAPI, people: People, ready: str, owner: TestClient
) -> None:
    booking_id = make_pending(app, people.a, ready, starts_at=at("10:00"))
    with tenant_context(people.a) as session:
        session.execute(
            text("UPDATE memberships SET display_name = 'Joana' WHERE user_id = :u"),
            {"u": people.both},
        )
    assert patch(owner, booking_id, "confirmed").status_code == 200

    assert history(owner.get(f"/api/bookings/{booking_id}")) == [
        ("created", "client", None),
        ("confirmed", "team", "Joana"),
    ]


# F12: the app role still cannot UPDATE or DELETE a booking event, whatever the new grant says.
# Kills an over-wide grant.
def test_the_app_role_still_cannot_update_or_delete_a_booking_event(
    people: People, shop: dict[str, Any], app_engine: Engine
) -> None:
    booking_id = put(people, shop, "owner", hour(10), hour(11))
    with tenant_context(people.a) as session:
        session.execute(
            text("""
            INSERT INTO booking_events (tenant_id, booking_id, event, actor_user_id)
            VALUES (current_setting('app.tenant_id')::uuid, :b, 'confirmed', :u)
            """),
            {"b": booking_id, "u": people.both},
        )
    for statement in (
        "UPDATE booking_events SET actor_user_id = NULL",
        "UPDATE booking_events SET details = NULL",
        "DELETE FROM booking_events",
    ):
        with pytest.raises(DBAPIError) as error, app_engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
            conn.execute(text(statement))
        assert isinstance(error.value.orig, InsufficientPrivilege)


# F13: a client's reschedule through their link lands in the history with from and to, in UTC.
# Kills a reschedule write site left untouched.
def test_a_clients_reschedule_is_in_the_history_with_both_times(
    app: FastAPI, people: People, ready: str, owner: TestClient
) -> None:
    booking_id = make_pending(app, people.a, ready, starts_at=at("10:00"))
    assert patch(owner, booking_id, "confirmed").status_code == 200
    moved = post(
        linked(app, people.a, booking_id),
        "/reschedule",
        reschedule_body(booking_id, at("10:30"), 0),
    )
    assert moved.status_code == 200

    events = owner.get(f"/api/bookings/{booking_id}").json()["history"]

    rescheduled = events[-1]
    assert (rescheduled["event"], rescheduled["actor"]) == ("rescheduled", "client")
    assert set(rescheduled["details"]) == {"from", "to"}
    assert rescheduled["details"]["from"].endswith("+00:00")
    assert datetime.fromisoformat(rescheduled["details"]["from"]) == datetime.fromisoformat(
        at("10:00")
    )
    assert datetime.fromisoformat(rescheduled["details"]["to"]) == datetime.fromisoformat(
        at("10:30")
    )


# F14: actor_user_id has no foreign key, so removing a member never meets their events. Kills
# `REFERENCES users (id)` or `REFERENCES memberships`.
def test_actor_user_id_has_no_foreign_key(migrate_engine: Engine) -> None:
    query = text("""
        SELECT count(*) FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
        WHERE c.conrelid = 'booking_events'::regclass AND c.contype = 'f' AND a.attname = :column
    """)
    with migrate_engine.connect() as conn:
        assert conn.scalar(query, {"column": "booking_id"}) == 1  # the query can see a foreign key
        assert conn.scalar(query, {"column": "actor_user_id"}) == 0


DETAIL_KEYS = {
    "id",
    "starts_at",
    "ends_at",
    "expires_at",
    "service_id",
    "service_name",
    "price",
    "worker_id",
    "worker_display_name",
    "client_id",
    "client_name",
    "created_at",
    "status",
    "source",
    "client_email",
    "client_phone",
    "client_note",
    "decline_message",
    "cancellation_policy_text",
    "reschedule_count",
    "max_reschedules",
    "expired",
    "history",
}


# F15: the detail never carries the private notes or the event's personal data, in the response or
# in the published schema. Kills SELECT * and reusing ClientOut.
def test_the_detail_carries_no_internal_note_or_event_personal_data(
    app: FastAPI, people: People, ready: str, owner: TestClient
) -> None:
    booking_id = make_pending(app, people.a, ready, starts_at=at("10:00"))
    with tenant_context(people.a) as session:
        session.execute(text("UPDATE clients SET internal_note = 'secret', client_note = 'hi'"))

    body = owner.get(f"/api/bookings/{booking_id}").json()

    assert set(body) == DETAIL_KEYS
    assert body["client_note"] == "hi"
    assert set(body["history"][0]) == {"event", "at", "actor", "actor_name", "details"}
    assert "secret" not in json.dumps(body)
    schemas = app.openapi()["components"]["schemas"]
    exposed = set(schemas["BookingDetailOut"]["properties"]) | set(
        schemas["EventOut"]["properties"]
    )
    assert not exposed & {"internal_note", "ip", "user_agent", "policy_version", "consent_purposes"}


# F16: the new `{booking_id}` route does not swallow /bookings/pending.
def test_pending_still_routes_to_the_queue(owner: TestClient) -> None:
    response = owner.get("/api/bookings/pending")

    assert (response.status_code, response.json()) == (200, [])


def insert_event(
    people: People, booking_id: str, event: str, actor: uuid.UUID | None = None
) -> None:
    with tenant_context(people.a) as session:
        session.execute(
            text("""
            INSERT INTO booking_events (tenant_id, booking_id, event, actor_user_id)
            VALUES (current_setting('app.tenant_id')::uuid, :b, :e, :a)
            """),
            {"b": booking_id, "e": event, "a": actor},
        )


# F20: a pre-0033 row (actor NULL) still reads right: a transition is the team's, a marketplace
# `created` is the client's. Kills "NULL means client". G1: a merchant `created` is the team's.
def test_a_null_actor_is_read_from_the_event_and_the_source(
    people: People, shop: dict[str, Any], owner: TestClient
) -> None:
    marketplace = put(people, shop, "owner", hour(8), hour(9), source="marketplace")
    merchant = put(people, shop, "owner", hour(10), hour(11), source="merchant")
    for booking_id, events in (
        (marketplace, ("created", "confirmed", "cancelled_by_client", "rescheduled")),
        (merchant, ("created", "expired")),
    ):
        for event in events:
            insert_event(people, booking_id, event)

    assert history(owner.get(f"/api/bookings/{marketplace}")) == [
        ("created", "client", None),
        ("confirmed", "team", None),
        ("cancelled_by_client", "client", None),
        ("rescheduled", "client", None),
    ]
    assert history(owner.get(f"/api/bookings/{merchant}")) == [
        ("created", "team", None),
        ("expired", "system", None),
    ]


# G4: a lapsed pending nobody has swept reads `pending` with a past expires_at and expired=true, so
# no panel offers Accept on it; a live one is expired=false.
def test_a_lapsed_unswept_pending_says_expired(
    people: People, shop: dict[str, Any], owner: TestClient
) -> None:
    now = datetime.now(UTC)
    lapsed = put(
        people,
        shop,
        "owner",
        hour(10),
        hour(11),
        status="pending",
        expires_at=now - timedelta(hours=1),
    )
    live = put(
        people,
        shop,
        "worker",
        hour(10),
        hour(11),
        status="pending",
        expires_at=now + timedelta(hours=1),
    )
    settled = put(people, shop, "owner", hour(12), hour(13))

    got = {i: owner.get(f"/api/bookings/{i}").json() for i in (lapsed, live, settled)}

    assert (got[lapsed]["status"], got[lapsed]["expired"]) == ("pending", True)
    assert datetime.fromisoformat(got[lapsed]["expires_at"]) < now
    assert got[live]["expired"] is False
    assert got[settled]["expired"] is False


# G2: both routes send no-store, and the operation ids stay unique.
def test_both_routes_are_no_store_and_operation_ids_are_unique(
    app: FastAPI, people: People, shop: dict[str, Any], owner: TestClient
) -> None:
    booking_id = put(people, shop, "owner", hour(10), hour(11))

    assert range_(owner).headers["cache-control"] == "no-store"
    assert owner.get(f"/api/bookings/{booking_id}").headers["cache-control"] == "no-store"
    operations = [
        operation["operationId"]
        for path in app.openapi()["paths"].values()
        for operation in path.values()
    ]
    assert len(operations) == len(set(operations))
    assert "booking-approvals-range" in operations
    assert "booking-approvals-read" in operations
