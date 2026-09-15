import asyncio
import json
import os
import secrets
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from http.cookies import SimpleCookie
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text

from app import auth, passwords
from app.db import tenant_context
from app.jobs import run_once
from app.main import create_app
from app.worker import KINDS
from tests.conftest import EXPIRE, PASSWORD, People, email_of, issue_link, new_client

MAILPIT = f"http://{os.environ['ZIF_SMTP_HOST']}:8025"


def fresh_email() -> str:
    return f"{uuid.uuid4()}@example.com"


@pytest.fixture
def app(bound: None) -> FastAPI:
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return new_client(app)


@pytest.fixture
def businesses(migrate_engine: Engine, bound: None) -> Iterator[list[dict[str, Any]]]:
    """Sessions returned by completed sign-ups; their accounts and businesses go afterwards."""
    created: list[dict[str, Any]] = []
    yield created
    for business in created:
        with tenant_context(business["tenant_id"]) as session:
            session.execute(text("DELETE FROM memberships"))
    with migrate_engine.begin() as conn:
        ids = {"u": [b["user_id"] for b in created], "t": [b["tenant_id"] for b in created]}
        conn.execute(text("DELETE FROM password_credentials WHERE user_id::text = ANY(:u)"), ids)
        conn.execute(text("DELETE FROM users WHERE id::text = ANY(:u)"), ids)
        conn.execute(text("DELETE FROM tenants WHERE id::text = ANY(:t)"), ids)


def complete(
    client: TestClient, token: str, password: str = PASSWORD, business: str = "Studio Ana"
) -> Response:
    # Encoded here: json.dumps escapes a lone surrogate the way a browser can send it.
    body = json.dumps({"token": token, "password": password, "business_name": business})
    return client.post(
        "/api/sign-up/complete", content=body, headers={"content-type": "application/json"}
    )


def created(response: Response, businesses: list[dict[str, Any]]) -> dict[str, Any]:
    assert response.status_code == 201, response.content
    session: dict[str, Any] = response.json()
    businesses.append(session)
    return session


def live(app_engine: Engine, token: str) -> bool:
    with app_engine.connect() as conn:
        result: bool = conn.scalar(
            text("SELECT email_token_live(:h, 'sign_up')"), {"h": auth.hash_token(token)}
        )
    return result


def jobs_for(migrate_engine: Engine, email: str) -> int:
    with migrate_engine.connect() as conn:
        count: int = conn.scalar(
            text("""
            SELECT count(*) FROM jobs j JOIN email_tokens t ON j.payload->>'token_id' = t.id::text
            WHERE t.email = :e AND j.kind = 'email.token'
            """),
            {"e": email},
        )
    return count


@pytest.fixture
def no_hashing(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    hashed: list[str] = []

    def spy(password: str) -> str:
        hashed.append(password)
        return ""

    monkeypatch.setattr(passwords, "hash_password", spy)
    return hashed


def test_signing_up_answers_the_same_for_a_new_and_a_registered_email(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    new, registered = fresh_email(), email_of(people.only_a)

    answers = [
        new_client(app).post("/api/sign-up", json={"email": email, "locale": "en"})
        for email in (new, registered)
    ]

    assert [a.status_code for a in answers] == [202, 202]
    assert answers[0].content == answers[1].content
    assert {k: v for k, v in answers[0].headers.items() if k != "date"} == {
        k: v for k, v in answers[1].headers.items() if k != "date"
    }
    assert (jobs_for(migrate_engine, new), jobs_for(migrate_engine, registered)) == (1, 1)


def test_the_emailed_link_sets_up_the_business_and_the_owner_can_sign_in_again(
    app: FastAPI, businesses: list[dict[str, Any]]
) -> None:
    email = fresh_email()
    client = new_client(app)
    assert client.post("/api/sign-up", json={"email": email, "locale": "nl"}).status_code == 202

    async def run_until_idle() -> None:
        while await run_once(KINDS):
            pass

    asyncio.run(run_until_idle())
    query = urllib.parse.urlencode({"query": f"to:{email}"})
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/search?{query}", timeout=5) as found:
        (sent,) = json.load(found)["messages"]
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/message/{sent['ID']}", timeout=5) as body:
        token = json.load(body)["Text"].split("#")[1].split()[0]

    response = complete(client, token, business="  Studio Ana Nails  ")

    session = created(response, businesses)
    assert response.headers["cache-control"] == "no-store"
    assert (session["email"], session["role"], session["business_name"]) == (
        email,
        "owner",
        "Studio Ana Nails",
    )
    client.cookies.set(auth.COOKIE, SimpleCookie(response.headers["set-cookie"])[auth.COOKIE].value)
    assert client.get("/api/session").json()["tenant_id"] == session["tenant_id"]
    with tenant_context(uuid.UUID(session["tenant_id"])) as db:
        # The language of the page the owner signed up on.
        locale = db.scalar(
            text("SELECT locale FROM users WHERE id = :u"), {"u": session["user_id"]}
        )
    assert locale == "nl"
    # The password they chose is the one stored.
    again = new_client(app).post("/api/session", json={"email": email, "password": PASSWORD})
    assert again.status_code == 200


def test_completing_sign_up_replaces_the_session_the_browser_already_had(
    people: People, app: FastAPI, app_engine: Engine, businesses: list[dict[str, Any]]
) -> None:
    with tenant_context(people.b) as session:
        planted = auth.create(session, people.only_b, ip=None, user_agent=None)
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None
    client = new_client(app)
    client.cookies.set(auth.COOKIE, planted)

    created(complete(client, token), businesses)

    with app_engine.connect() as conn:
        assert (
            conn.scalar(
                text("SELECT count(*) FROM sessions WHERE id_hash = :h"),
                {"h": auth.hash_token(planted)},
            )
            == 0
        )


def test_a_link_works_once(
    app: FastAPI, app_engine: Engine, businesses: list[dict[str, Any]]
) -> None:
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None

    created(complete(new_client(app), token), businesses)
    again = complete(new_client(app), token)

    assert (again.status_code, again.json()) == (400, {"code": "invalid_token"})


@pytest.mark.parametrize("wrong", ["expired", "reset_link", "made_up"])
def test_only_a_live_sign_up_link_is_accepted(
    people: People, client: TestClient, app_engine: Engine, migrate_engine: Engine, wrong: str
) -> None:
    if wrong == "expired":
        token = issue_link(app_engine, "sign_up", fresh_email())
        assert token is not None
        with migrate_engine.begin() as conn:
            conn.execute(text(EXPIRE), {"h": auth.hash_token(token)})
    elif wrong == "reset_link":
        token = issue_link(app_engine, "password_reset", email_of(people.only_a))
        assert token is not None
    else:
        token = secrets.token_urlsafe(32)

    response = complete(client, token)

    assert (response.status_code, response.json()) == (400, {"code": "invalid_token"})


def test_a_dead_link_costs_no_password_hash(client: TestClient, no_hashing: list[str]) -> None:
    response = complete(client, secrets.token_urlsafe(32))

    assert (response.status_code, no_hashing) == (400, [])


@pytest.mark.parametrize(
    ("password", "code"),
    [
        ("short-pw-11", "password_too_short"),
        ("x" * 257, "password_too_long"),
        ("Q1W2E3R4T5Y6", "password_too_common"),  # on the list, in any case
        ("lavender-harbour-19\ud800", "invalid_request"),  # a lone surrogate argon2 can't hash
    ],
)
def test_a_weak_password_is_refused_without_hashing_and_the_link_still_works(
    app: FastAPI, app_engine: Engine, no_hashing: list[str], password: str, code: str
) -> None:
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None

    response = complete(new_client(app), token, password)

    assert (response.status_code, response.json(), no_hashing) == (422, {"code": code}, [])
    assert live(app_engine, token)


@pytest.mark.parametrize(
    "business", ["", "   ", "x" * 101, "\u200b", "Studio\x00Ana", "Studio\x07"]
)
def test_a_business_needs_a_printable_name(app: FastAPI, app_engine: Engine, business: str) -> None:
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None

    response = complete(new_client(app), token, business=business)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert live(app_engine, token)


def test_a_business_name_of_a_hundred_characters_is_kept_without_its_spaces(
    app: FastAPI, app_engine: Engine, businesses: list[dict[str, Any]]
) -> None:
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None

    session = created(complete(new_client(app), token, business=f"  {'x' * 100}  "), businesses)

    assert session["business_name"] == "x" * 100


def test_an_email_registered_after_the_link_went_out_creates_nothing(
    app: FastAPI, app_engine: Engine, migrate_engine: Engine
) -> None:
    email = fresh_email()
    token = issue_link(app_engine, "sign_up", email)
    assert token is not None
    with app_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :e)"), {"id": uuid.uuid7(), "e": email}
        )
    with migrate_engine.connect() as conn:
        tenants = conn.scalar(text("SELECT count(*) FROM tenants"))

    try:
        response = complete(new_client(app), token)
        assert (response.status_code, response.json()) == (409, {"code": "account_exists"})
        with migrate_engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM tenants")) == tenants
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE email = :e"), {"e": email})


def test_the_complete_step_is_never_a_get(client: TestClient, app_engine: Engine) -> None:
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None

    assert client.get(f"/api/sign-up/complete?token={token}").status_code == 405
    assert live(app_engine, token)


def test_sign_up_requests_for_one_email_are_limited_whatever_its_case(app: FastAPI) -> None:
    email = fresh_email()

    statuses = [
        new_client(app).post("/api/sign-up", json={"email": typed, "locale": "en"}).status_code
        for typed in (email, email.upper(), f" {email} ", email)
    ]

    assert statuses == [202, 202, 202, 429]


def test_sign_up_requests_from_one_address_are_limited(client: TestClient) -> None:
    statuses = [
        client.post("/api/sign-up", json={"email": fresh_email(), "locale": "en"}).status_code
        for _ in range(11)
    ]

    assert statuses == [202] * 10 + [429]


@pytest.mark.parametrize(
    "body",
    [{"email": "not-an-email", "locale": "en"}, {"email": "a@example.com", "locale": "de"}],
)
def test_a_malformed_sign_up_gets_a_code(client: TestClient, body: dict[str, str]) -> None:
    response = client.post("/api/sign-up", json=body)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
