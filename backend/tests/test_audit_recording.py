"""Sign-ins, sign-outs, new businesses and password resets leave audit events, in the same
transaction as what they record, and never with a secret in them."""

import hashlib
import secrets
import uuid
from collections.abc import Iterator
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text

from app import auth, limits
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    PASSWORD,
    People,
    add_password,
    delete_services,
    email_of,
    events,
    failing,
    fresh_address,
    fresh_email,
    issue_link,
    live,
    new_client,
    put_settings,
    signed_in,
)

NEW_PASSWORD = "quiet-copper-kettle-7"


def client_at(app: FastAPI) -> tuple[TestClient, str]:
    """A client with its own IPv6 address, so its events can be found by address alone."""
    address = fresh_address()
    return new_client(app, address), address


def sessions_of(app_engine: Engine, user_id: uuid.UUID) -> int:
    with app_engine.connect() as conn:
        count: int = conn.scalar(
            text("SELECT count(*) FROM sessions WHERE user_id = :u"), {"u": user_id}
        )
    return count


def sign_in(client: TestClient, email: str, password: str = PASSWORD) -> Response:
    return client.post("/api/session", json={"email": email, "password": password})


@pytest.fixture
def app(people: People, migrate_engine: Engine) -> FastAPI:
    add_password(migrate_engine, people.only_a, PASSWORD)
    add_password(migrate_engine, people.both, PASSWORD)
    return create_app()


@pytest.fixture
def businesses(migrate_engine: Engine, bound: None) -> Iterator[list[uuid.UUID]]:
    """Businesses signed up in a test, removed afterwards with their owners."""
    tenants: list[uuid.UUID] = []
    yield tenants
    for tenant_id in tenants:
        with tenant_context(tenant_id) as session:
            owners = list(session.scalars(text("SELECT user_id FROM memberships")))
            session.execute(text("DELETE FROM settings"))  # they reference the business
            session.execute(text("DELETE FROM memberships"))
        with migrate_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM password_credentials WHERE user_id = ANY(:u)"), {"u": owners}
            )
            conn.execute(text("DELETE FROM users WHERE id = ANY(:u)"), {"u": owners})
            delete_services(conn, [tenant_id])
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})


def test_signing_in_is_recorded_in_the_business(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    client, address = client_at(app)

    assert sign_in(client, email_of(people.only_a)).status_code == 200

    assert events(migrate_engine, ip=address) == [
        {
            "action": "sign_in_succeeded",
            "tenant_id": people.a,
            "actor_user_id": people.only_a,
            "target": None,
            "details": None,
            "ip": address,
            "user_agent": "testclient",
        }
    ]


@pytest.mark.parametrize("fails", ["record", "describe"])
def test_a_sign_in_that_fails_part_way_leaves_neither_session_nor_event(
    people: People,
    app: FastAPI,
    app_engine: Engine,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    fails: str,
) -> None:
    # describe runs after the event is written: the event must go with the rolled-back session.
    failing(monkeypatch, auth, fails)
    client, address = client_at(app)

    assert sign_in(client, email_of(people.only_a)).status_code == 500

    assert sessions_of(app_engine, people.only_a) == 0
    assert events(migrate_engine, ip=address) == []


def test_failed_sign_ins_look_the_same_whether_or_not_the_account_exists(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    unknown_client, unknown_address = client_at(app)
    known_client, known_address = client_at(app)

    unknown = sign_in(unknown_client, fresh_email(), "not-the-password-1")
    wrong = sign_in(known_client, email_of(people.only_a), "not-the-password-1")

    assert (unknown.status_code, wrong.status_code) == (401, 401)
    assert unknown.content == wrong.content
    assert {k: v for k, v in unknown.headers.items() if k != "date"} == {
        k: v for k, v in wrong.headers.items() if k != "date"
    }
    no_account = {
        "action": "sign_in_failed",
        "tenant_id": None,
        "actor_user_id": None,
        "details": None,
    }
    assert events(migrate_engine, ip=unknown_address) == [
        {**no_account, "target": None, "ip": unknown_address, "user_agent": "testclient"}
    ]
    assert events(migrate_engine, ip=known_address) == [
        {
            **no_account,
            "target": f"user:{people.only_a}",
            "ip": known_address,
            "user_agent": "testclient",
        }
    ]


def test_a_rate_limited_sign_in_records_nothing(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "hit", lambda keys, window: True)
    client, address = client_at(app)

    assert sign_in(client, email_of(people.only_a), "not-the-password-1").status_code == 429

    assert events(migrate_engine, ip=address) == []


def test_signing_out_is_recorded_in_the_business_of_the_session(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    with tenant_context(people.b) as session:
        token = auth.create(session, people.both, ip=None, user_agent=None)
    client, address = client_at(app)
    client.cookies.set(auth.COOKIE, token)

    assert client.request("DELETE", "/api/session", json={}).status_code == 204

    assert [
        (e["action"], e["tenant_id"], e["actor_user_id"])
        for e in events(migrate_engine, ip=address)
    ] == [("signed_out", people.b, people.both)]


@pytest.mark.parametrize("cookie", [None, "unknown"])
def test_signing_out_without_a_session_records_nothing(
    app: FastAPI, migrate_engine: Engine, cookie: str | None
) -> None:
    client, address = client_at(app)
    if cookie:
        client.cookies.set(auth.COOKIE, secrets.token_urlsafe(32))

    assert client.request("DELETE", "/api/session", json={}).status_code == 204

    assert events(migrate_engine, ip=address) == []


def test_a_sign_out_whose_event_fails_keeps_the_session(
    people: People,
    app: FastAPI,
    app_engine: Engine,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tenant_context(people.a) as session:
        token = auth.create(session, people.only_a, ip=None, user_agent=None)
    failing(monkeypatch, auth, "record")
    client, _ = client_at(app)
    client.cookies.set(auth.COOKIE, token)

    assert client.request("DELETE", "/api/session", json={}).status_code == 500

    assert sessions_of(app_engine, people.only_a) == 1


def test_signing_out_everywhere_is_recorded(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    with tenant_context(people.a) as session:
        token = auth.create(session, people.both, ip=None, user_agent=None)
    client, address = client_at(app)
    client.cookies.set(auth.COOKIE, token)

    assert client.request("DELETE", "/api/sessions", json={}).status_code == 204

    assert [
        (e["action"], e["tenant_id"], e["actor_user_id"])
        for e in events(migrate_engine, ip=address)
    ] == [("signed_out_everywhere", people.a, people.both)]


def complete_sign_up(client: TestClient, token: str, country: str | None = None) -> Response:
    body = {"token": token, "password": PASSWORD, "business_name": "Studio Audit"}
    if country is not None:
        body["country"] = country
    return client.post("/api/sign-up/complete", json=body)


@pytest.mark.parametrize("country", [None, "BR"])
def test_a_new_business_is_recorded_with_its_first_sign_in(
    app: FastAPI,
    app_engine: Engine,
    migrate_engine: Engine,
    businesses: list[uuid.UUID],
    country: str | None,
) -> None:
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None
    client, address = client_at(app)

    response = complete_sign_up(client, token, country)

    assert response.status_code == 201
    session = response.json()
    tenant_id, user_id = uuid.UUID(session["tenant_id"]), uuid.UUID(session["user_id"])
    businesses.append(tenant_id)
    recorded = events(migrate_engine, ip=address)
    assert [(e["action"], e["tenant_id"], e["actor_user_id"]) for e in recorded] == [
        ("business_created", tenant_id, user_id),
        ("sign_in_succeeded", tenant_id, user_id),
    ]
    assert [e["details"] for e in recorded] == [None, None]


def test_a_business_whose_event_fails_is_not_created(
    app: FastAPI, app_engine: Engine, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = fresh_email()
    token = issue_link(app_engine, "sign_up", email)
    assert token is not None
    failing(monkeypatch, auth, "record")
    client, _ = client_at(app)

    assert complete_sign_up(client, token).status_code == 500

    with migrate_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users WHERE email = :e"), {"e": email}) == 0
    assert live(app_engine, token, "sign_up")


def complete_reset(client: TestClient, token: str) -> Response:
    return client.post(
        "/api/password-reset/complete", json={"token": token, "password": NEW_PASSWORD}
    )


def test_a_password_reset_is_recorded_against_the_account(
    people: People, app: FastAPI, app_engine: Engine, migrate_engine: Engine
) -> None:
    token = issue_link(app_engine, "password_reset", email_of(people.both))
    assert token is not None
    client, address = client_at(app)

    assert complete_reset(client, token).status_code == 204

    assert events(migrate_engine, ip=address) == [
        {
            "action": "password_reset_completed",
            "tenant_id": None,
            "actor_user_id": None,
            "target": f"user:{people.both}",
            "details": None,
            "ip": address,
            "user_agent": "testclient",
        }
    ]


def test_a_reset_that_changes_no_password_records_nothing(
    app: FastAPI, app_engine: Engine, migrate_engine: Engine
) -> None:
    # A live link whose account was removed before it was used: the reset itself finds no one.
    user_id = uuid.uuid7()
    with app_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :e)"),
            {"id": user_id, "e": email_of(user_id)},
        )
    token = issue_link(app_engine, "password_reset", email_of(user_id))
    assert token is not None
    with migrate_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
    client, address = client_at(app)

    assert complete_reset(client, token).status_code == 400

    assert events(migrate_engine, ip=address) == []
    assert not live(app_engine, token, "password_reset")  # used up all the same


def test_a_reset_whose_event_fails_changes_nothing(
    people: People,
    app: FastAPI,
    app_engine: Engine,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = issue_link(app_engine, "password_reset", email_of(people.both))
    assert token is not None
    client, _ = client_at(app)

    with monkeypatch.context() as patch:
        failing(patch, auth, "record")
        assert complete_reset(client, token).status_code == 500

    assert live(app_engine, token, "password_reset")
    assert sign_in(client_at(app)[0], email_of(people.both)).status_code == 200


def test_no_event_holds_a_password_email_token_or_cookie(
    app: FastAPI, app_engine: Engine, migrate_engine: Engine, businesses: list[uuid.UUID]
) -> None:
    email = fresh_email()
    sign_up_token = issue_link(app_engine, "sign_up", email)
    assert sign_up_token is not None
    client, _ = client_at(app)
    signed_up = complete_sign_up(client, sign_up_token)
    assert signed_up.status_code == 201
    tenant_id, user_id = signed_up.json()["tenant_id"], signed_up.json()["user_id"]
    businesses.append(uuid.UUID(tenant_id))
    cookies = [SimpleCookie(signed_up.headers["set-cookie"])[auth.COOKIE].value]
    again = sign_in(client_at(app)[0], email)
    cookies.append(SimpleCookie(again.headers["set-cookie"])[auth.COOKIE].value)
    assert sign_in(client_at(app)[0], email, "not-the-password-1").status_code == 401
    for path, cookie in (("/api/session", cookies[1]), ("/api/sessions", cookies[0])):
        signing_out = client_at(app)[0]
        signing_out.cookies.set(auth.COOKIE, cookie)
        assert signing_out.request("DELETE", path, json={}).status_code == 204
    reset_token = issue_link(app_engine, "password_reset", email)
    assert reset_token is not None
    assert complete_reset(client_at(app)[0], reset_token).status_code == 204
    # A settings change is the one event that carries values of its own, in details.
    owner = signed_in(app, uuid.UUID(tenant_id), uuid.UUID(user_id))
    assert put_settings(owner, {"timezone": "Asia/Tokyo"}).status_code == 200

    with migrate_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.audit_review = 'on'"))
        rows = list(
            conn.scalars(
                text("""
                SELECT audit_events::text FROM audit_events
                WHERE tenant_id = :t OR actor_user_id = :u OR target = 'user:' || :u
                """),
                {"t": tenant_id, "u": user_id},
            )
        )
    # Created, signed in twice, a failed sign-in, signed out, signed out everywhere, a reset, and
    # a settings change.
    assert len(rows) == 8
    secrets_ = [PASSWORD, NEW_PASSWORD, email, sign_up_token, reset_token, *cookies]
    secrets_ += [
        hashlib.sha256(s.encode()).hexdigest() for s in (sign_up_token, reset_token, *cookies)
    ]
    assert [s for s in secrets_ if any(s in row for row in rows)] == []
