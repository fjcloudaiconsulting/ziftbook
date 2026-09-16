import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text

from app import auth
from app.db import tenant_context
from app.main import create_app
from tests.conftest import People, email_of, member_id


@pytest.fixture
def app(people: People) -> FastAPI:
    # No lifespan (TestClient isn't entered): SessionLocal stays bound by the people fixture.
    app = create_app()

    @app.get("/api/probe/tenant", tags=["probe"])
    def tenant(current: auth.CurrentSession) -> str:
        return str(current.db.scalar(text("SELECT current_setting('app.tenant_id')")))

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="https://testserver")


def sign_in(client: TestClient, tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    with tenant_context(tenant_id) as session:
        token = auth.create(session, user_id, ip=None, user_agent=None)
    client.cookies.set(auth.COOKIE, token)
    return token


def age(app_engine: Engine, token: str, column: str, by: timedelta) -> None:
    with app_engine.begin() as conn:
        conn.execute(
            text(f"UPDATE sessions SET {column} = now() - :by WHERE id_hash = :h"),
            {"by": by, "h": auth.hash_token(token)},
        )


def last_seen(app_engine: Engine, token: str) -> datetime | None:
    with app_engine.connect() as conn:
        seen: datetime | None = conn.scalar(
            text("SELECT last_seen_at FROM sessions WHERE id_hash = :h"),
            {"h": auth.hash_token(token)},
        )
    return seen


def cleared(response: Response) -> bool:
    (header,) = response.headers.get_list("set-cookie")
    cookie = SimpleCookie(header)[auth.COOKIE]
    return cookie["max-age"] == "0" and bool(cookie["secure"])


def unauthenticated(response: Response) -> bool:
    return response.status_code == 401 and response.json() == {"code": "unauthenticated"}


@pytest.mark.parametrize("cookie", [None, "garbage"])
def test_without_a_valid_cookie_the_request_is_unauthenticated(
    client: TestClient, cookie: str | None
) -> None:
    if cookie:
        client.cookies.set(auth.COOKIE, cookie)

    assert unauthenticated(client.get("/api/session"))


def test_a_signed_in_user_sees_their_session(people: People, client: TestClient) -> None:
    sign_in(client, people.a, people.both)

    response = client.get("/api/session")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "user_id": str(people.both),
        "tenant_id": str(people.a),
        "member_id": str(member_id(people.a, people.both)),
        "role": "owner",
        "email": email_of(people.both),
        "business_name": "a",
        "currency": "EUR",
    }


def test_a_session_idle_too_long_is_rejected(
    people: People, client: TestClient, app_engine: Engine
) -> None:
    token = sign_in(client, people.a, people.only_a)
    age(app_engine, token, "last_seen_at", auth.IDLE + timedelta(seconds=1))

    assert unauthenticated(client.get("/api/session"))


def test_a_session_past_its_absolute_expiry_is_rejected_even_when_active(
    people: People, client: TestClient, app_engine: Engine
) -> None:
    token = sign_in(client, people.a, people.only_a)
    age(app_engine, token, "expires_at", timedelta(seconds=1))

    assert unauthenticated(client.get("/api/session"))


def test_last_seen_is_written_at_most_every_few_minutes(
    people: People, client: TestClient, app_engine: Engine
) -> None:
    token = sign_in(client, people.a, people.only_a)

    age(app_engine, token, "last_seen_at", timedelta(minutes=6))
    stale = last_seen(app_engine, token)
    client.get("/api/session")
    touched = last_seen(app_engine, token)
    client.get("/api/session")
    assert stale is not None and touched is not None
    assert touched > stale + timedelta(minutes=5)
    assert last_seen(app_engine, token) == touched

    age(app_engine, token, "last_seen_at", timedelta(minutes=4))
    recent = last_seen(app_engine, token)
    client.get("/api/session")
    assert last_seen(app_engine, token) == recent


def test_removing_the_membership_signs_the_user_out_on_the_next_request(
    people: People, client: TestClient
) -> None:
    sign_in(client, people.a, people.only_a)
    assert client.get("/api/session").status_code == 200

    with tenant_context(people.a) as session:
        session.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": people.only_a})

    assert unauthenticated(client.get("/api/session"))


def test_the_request_runs_in_the_sessions_tenant(people: People, client: TestClient) -> None:
    # A member of both tenants, signed in to the second: not their first membership.
    sign_in(client, people.b, people.both)

    assert client.get("/api/probe/tenant").json() == str(people.b)


def test_signing_out_ends_only_this_browsers_session(
    people: People, client: TestClient, app: FastAPI, app_engine: Engine
) -> None:
    other_device = sign_in(TestClient(app), people.a, people.only_a)
    token = sign_in(client, people.a, people.only_a)

    response = client.request("DELETE", "/api/session", json={})

    assert response.status_code == 204
    assert cleared(response)
    client.cookies.set(auth.COOKIE, token)  # a copy of the cookie kept somewhere else
    assert unauthenticated(client.get("/api/session"))
    assert last_seen(app_engine, other_device) is not None


def test_signing_out_works_without_a_live_session(client: TestClient) -> None:
    client.cookies.set(auth.COOKIE, "expired-or-unknown")

    response = client.request("DELETE", "/api/session", json={})

    assert response.status_code == 204
    assert cleared(response)


def test_signing_out_everywhere_ends_every_session_of_that_user_only(
    people: People, client: TestClient, app: FastAPI, app_engine: Engine
) -> None:
    elsewhere = sign_in(TestClient(app), people.b, people.both)
    other_user = sign_in(TestClient(app), people.a, people.only_a)
    sign_in(client, people.a, people.both)

    response = client.request("DELETE", "/api/sessions", json={})

    assert response.status_code == 204
    assert cleared(response)
    assert last_seen(app_engine, elsewhere) is None
    assert last_seen(app_engine, other_user) is not None
    assert unauthenticated(client.get("/api/session"))


def test_signing_out_everywhere_needs_a_live_session(client: TestClient) -> None:
    assert unauthenticated(client.request("DELETE", "/api/sessions", json={}))


def test_a_cross_site_form_cannot_sign_anyone_out(
    people: People, client: TestClient, app_engine: Engine
) -> None:
    token = sign_in(client, people.a, people.only_a)

    response = client.request(
        "DELETE", "/api/session", content="x", headers={"content-type": "text/plain"}
    )

    assert response.status_code == 415
    assert last_seen(app_engine, token) is not None


@pytest.fixture
def deferred_table(migrate_engine: Engine) -> Iterator[None]:
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE commit_probe (id int, "
                "CONSTRAINT unique_id UNIQUE (id) DEFERRABLE INITIALLY DEFERRED)"
            )
        )
    yield
    with migrate_engine.begin() as conn:
        conn.execute(text("DROP TABLE commit_probe"))


def test_a_failed_commit_is_an_error_response_not_a_success(
    people: People, app: FastAPI, deferred_table: None
) -> None:
    # The unique check waits for COMMIT, so it fails only once the endpoint has returned.
    @app.post("/api/probe/commit", tags=["probe"])
    def commit(current: auth.CurrentSession) -> None:
        current.db.execute(text("INSERT INTO commit_probe VALUES (1), (1)"))

    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    sign_in(client, people.a, people.only_a)

    response = client.post("/api/probe/commit", json={})
    assert (response.status_code, response.json()) == (500, {"code": "internal"})
