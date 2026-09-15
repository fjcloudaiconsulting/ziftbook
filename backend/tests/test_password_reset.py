import asyncio
import json
import os
import secrets
import urllib.parse
import urllib.request
import uuid
from http.cookies import SimpleCookie

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
from tests.conftest import EXPIRE, PASSWORD, People, add_password, email_of, issue_link, new_client

MAILPIT = f"http://{os.environ['ZIF_SMTP_HOST']}:8025"
NEW_PASSWORD = "quiet-copper-kettle-7"


@pytest.fixture
def app(people: People, migrate_engine: Engine) -> FastAPI:
    add_password(migrate_engine, people.both, PASSWORD)
    add_password(migrate_engine, people.only_a, PASSWORD)
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return new_client(app)


def request_reset(client: TestClient, email: str, locale: str = "en") -> Response:
    return client.post("/api/password-reset", json={"email": email, "locale": locale})


def complete(client: TestClient, token: str, password: str = NEW_PASSWORD) -> Response:
    # Encoded here: json.dumps escapes a lone surrogate the way a browser can send it.
    return client.post(
        "/api/password-reset/complete",
        content=json.dumps({"token": token, "password": password}),
        headers={"content-type": "application/json"},
    )


def sign_in(app: FastAPI, email: str, password: str) -> int:
    response = new_client(app).post("/api/session", json={"email": email, "password": password})
    return response.status_code


def live(app_engine: Engine, token: str) -> bool:
    with app_engine.connect() as conn:
        result: bool = conn.scalar(
            text("SELECT email_token_live(:h, 'password_reset')"), {"h": auth.hash_token(token)}
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


def test_asking_for_a_reset_answers_the_same_whether_or_not_the_account_exists(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    known, unknown = email_of(people.both), f"{uuid.uuid4()}@example.com"

    answers = [request_reset(new_client(app), email) for email in (known, unknown)]

    assert [a.status_code for a in answers] == [202, 202]
    assert answers[0].content == answers[1].content
    assert {k: v for k, v in answers[0].headers.items() if k != "date"} == {
        k: v for k, v in answers[1].headers.items() if k != "date"
    }
    # Both do the same work; the email job drops the unknown one.
    assert (jobs_for(migrate_engine, known), jobs_for(migrate_engine, unknown)) == (1, 1)


def test_the_emailed_link_sets_a_new_password(people: People, app: FastAPI) -> None:
    email = email_of(people.both)
    assert request_reset(new_client(app), email, "pt").status_code == 202

    async def run_until_idle() -> None:
        while await run_once(KINDS):
            pass

    asyncio.run(run_until_idle())
    query = urllib.parse.urlencode({"query": f"to:{email}"})
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/search?{query}", timeout=5) as found:
        (sent,) = json.load(found)["messages"]
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/message/{sent['ID']}", timeout=5) as body:
        token = json.load(body)["Text"].split("#")[1].split()[0]

    response = complete(new_client(app), token)

    assert response.status_code == 204
    assert sign_in(app, email, PASSWORD) == 401
    assert sign_in(app, email, NEW_PASSWORD) == 200


def test_a_reset_signs_the_person_out_everywhere_and_ends_their_other_links(
    people: People, app: FastAPI, app_engine: Engine
) -> None:
    sessions = {}
    for tenant_id, user_id in (
        (people.a, people.both),
        (people.b, people.both),
        (people.a, people.only_a),
    ):
        with tenant_context(tenant_id) as session:
            sessions[(tenant_id, user_id)] = auth.create(session, user_id, ip=None, user_agent=None)
    used = issue_link(app_engine, "password_reset", email_of(people.both))
    other = issue_link(app_engine, "password_reset", email_of(people.both))
    assert used is not None and other is not None
    client = new_client(app)
    client.cookies.set(auth.COOKIE, sessions[(people.a, people.both)])

    response = complete(client, used)

    assert response.status_code == 204
    cleared = SimpleCookie(response.headers["set-cookie"])[auth.COOKIE]
    assert cleared["max-age"] == "0"
    with app_engine.connect() as conn:
        remaining = set(
            conn.scalars(
                text("SELECT user_id FROM sessions WHERE id_hash = ANY(:h)"),
                {"h": [auth.hash_token(t) for t in sessions.values()]},
            )
        )
    assert remaining == {people.only_a}
    assert not live(app_engine, other)


def test_a_link_works_once(people: People, app: FastAPI, app_engine: Engine) -> None:
    token = issue_link(app_engine, "password_reset", email_of(people.both))
    assert token is not None

    first = complete(new_client(app), token)
    again = complete(new_client(app), token, "another-new-password-8")

    assert (first.status_code, again.status_code, again.json()) == (
        204,
        400,
        {"code": "invalid_token"},
    )
    assert sign_in(app, email_of(people.both), NEW_PASSWORD) == 200


@pytest.mark.parametrize("wrong", ["expired", "sign_up_link", "made_up"])
def test_only_a_live_reset_link_is_accepted(
    people: People, client: TestClient, app_engine: Engine, migrate_engine: Engine, wrong: str
) -> None:
    if wrong == "expired":
        token = issue_link(app_engine, "password_reset", email_of(people.both))
        assert token is not None
        with migrate_engine.begin() as conn:
            conn.execute(text(EXPIRE), {"h": auth.hash_token(token)})
    elif wrong == "sign_up_link":
        token = issue_link(app_engine, "sign_up", f"{uuid.uuid4()}@example.com")
        assert token is not None
    else:
        token = secrets.token_urlsafe(32)

    response = complete(client, token)

    assert (response.status_code, response.json()) == (400, {"code": "invalid_token"})


def test_a_link_for_an_account_that_is_gone_says_so(
    app: FastAPI, app_engine: Engine, migrate_engine: Engine
) -> None:
    # The link is live, but the account went away before it was used: nothing was reset.
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

    response = complete(new_client(app), token)

    assert (response.status_code, response.json()) == (400, {"code": "invalid_token"})


def test_a_dead_link_costs_no_password_hash(client: TestClient, no_hashing: list[str]) -> None:
    response = complete(client, secrets.token_urlsafe(32))

    assert (response.status_code, no_hashing) == (400, [])


@pytest.mark.parametrize(
    ("password", "code"),
    [
        ("short-pw-11", "password_too_short"),
        ("x" * 257, "password_too_long"),
        ("Q1W2E3R4T5Y6", "password_too_common"),
        ("lavender-harbour-19\ud800", "invalid_request"),
    ],
)
def test_a_weak_password_is_refused_without_hashing_and_the_link_still_works(
    people: People,
    app: FastAPI,
    app_engine: Engine,
    no_hashing: list[str],
    password: str,
    code: str,
) -> None:
    token = issue_link(app_engine, "password_reset", email_of(people.both))
    assert token is not None

    response = complete(new_client(app), token, password)

    assert (response.status_code, response.json(), no_hashing) == (422, {"code": code}, [])
    assert live(app_engine, token)


def test_the_complete_step_is_never_a_get(
    people: People, client: TestClient, app_engine: Engine
) -> None:
    token = issue_link(app_engine, "password_reset", email_of(people.both))
    assert token is not None

    assert client.get(f"/api/password-reset/complete?token={token}").status_code == 405
    assert live(app_engine, token)


@pytest.mark.parametrize("who", ["known", "unknown"])
def test_reset_requests_for_one_email_are_limited(people: People, app: FastAPI, who: str) -> None:
    email = email_of(people.both) if who == "known" else f"{uuid.uuid4()}@example.com"

    statuses = [
        request_reset(new_client(app), typed).status_code
        for typed in (email, email.upper(), email, email)
    ]

    assert statuses == [202, 202, 202, 429]


def test_reset_requests_from_one_address_are_limited(client: TestClient) -> None:
    statuses = [request_reset(client, f"{uuid.uuid4()}@example.com").status_code for _ in range(11)]

    assert statuses == [202] * 10 + [429]
