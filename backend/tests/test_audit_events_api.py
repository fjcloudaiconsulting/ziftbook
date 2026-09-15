"""An owner reads their business's audit log: newest first, in pages, without their staff's
addresses and browsers."""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import auth
from app.db import tenant_context
from app.main import create_app
from tests.conftest import People, new_client


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def signed_in(app: FastAPI, tenant_id: uuid.UUID, user_id: uuid.UUID) -> TestClient:
    with tenant_context(tenant_id) as session:
        token = auth.create(session, user_id, ip=None, user_agent=None)
    client = new_client(app)
    client.cookies.set(auth.COOKIE, token)
    return client


def add_event(
    app_engine: Engine,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    ip: str = "192.0.2.10",
    user_agent: str = "Firefox",
) -> uuid.UUID:
    with app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text("""
            INSERT INTO audit_events (actor_user_id, action, target, ip, user_agent)
            VALUES (:actor, :action, :target, :ip, :user_agent)
            """),
            {
                "actor": actor_user_id,
                "action": action,
                "target": f"user:{actor_user_id}",
                "ip": ip,
                "user_agent": user_agent,
            },
        )
        event_id: uuid.UUID = conn.scalar(
            text("SELECT id FROM audit_events WHERE action = :a"), {"a": action}
        )
    return event_id


def test_an_owner_reads_only_their_business_newest_first(
    people: People, app: FastAPI, app_engine: Engine
) -> None:
    first = add_event(app_engine, people.a, people.both, "first")
    second = add_event(app_engine, people.a, people.only_a, "second")
    add_event(app_engine, people.b, people.only_b, "elsewhere")

    response = signed_in(app, people.a, people.both).get("/api/audit-events")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert [(e["id"], e["action"]) for e in response.json()] == [
        (str(second), "second"),
        (str(first), "first"),
    ]


def test_an_owner_sees_the_address_and_browser_only_of_their_own_events(
    people: People, app: FastAPI, app_engine: Engine
) -> None:
    add_event(app_engine, people.a, people.both, "mine", ip="2001:db8::1", user_agent="Safari")
    add_event(app_engine, people.a, people.only_a, "staff", ip="192.0.2.20", user_agent="Edge")
    add_event(app_engine, people.a, None, "nobody", ip="192.0.2.30", user_agent="Chrome")

    events = signed_in(app, people.a, people.both).get("/api/audit-events").json()

    assert {e["action"]: (e["actor_user_id"], e["ip"], e["user_agent"]) for e in events} == {
        "mine": (str(people.both), "2001:db8::1", "Safari"),
        "staff": (str(people.only_a), None, None),
        "nobody": (None, None, None),
    }


@pytest.mark.parametrize("who", ["worker", "owner_signed_in_as_worker"])
def test_only_an_owner_reads_the_log(people: People, app: FastAPI, who: str) -> None:
    # people.both owns a but works in b: the role comes from the session's business.
    tenant_id, user_id = (people.a, people.only_a) if who == "worker" else (people.b, people.both)

    response = signed_in(app, tenant_id, user_id).get("/api/audit-events")

    assert (response.status_code, response.json()) == (403, {"code": "owner_only"})


def test_the_log_needs_a_session(app: FastAPI) -> None:
    response = new_client(app).get("/api/audit-events")

    assert (response.status_code, response.json()) == (401, {"code": "unauthenticated"})


def test_the_log_comes_in_pages_before_an_event(
    people: People, app: FastAPI, app_engine: Engine
) -> None:
    ids = [add_event(app_engine, people.a, people.both, f"event-{n}") for n in range(3)]
    client = signed_in(app, people.a, people.both)

    page = client.get("/api/audit-events", params={"limit": 2}).json()
    rest = client.get("/api/audit-events", params={"limit": 2, "before": page[-1]["id"]}).json()

    assert [e["id"] for e in page] == [str(ids[2]), str(ids[1])]
    assert [e["id"] for e in rest] == [str(ids[0])]


@pytest.mark.parametrize("limit", [0, 101])
def test_a_page_holds_one_to_a_hundred_events(people: People, app: FastAPI, limit: int) -> None:
    response = signed_in(app, people.a, people.both).get(
        "/api/audit-events", params={"limit": limit}
    )

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
