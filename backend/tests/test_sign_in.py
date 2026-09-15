import secrets
import threading
import time
import uuid
from collections.abc import Iterator
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text

from app import auth, passwords
from app.db import tenant_context
from app.main import create_app
from tests.conftest import People, add_user

PASSWORD = "lavender-harbour-19"


def email_of(user_id: uuid.UUID) -> str:
    return f"{user_id}@example.com"


def client_address() -> str:
    # A fresh IPv6 /64 per client, so per-IP counts never carry over between tests or runs.
    return f"2001:db8:{secrets.randbelow(65536):x}:{secrets.randbelow(65536):x}::1"


@pytest.fixture
def app(people: People, migrate_engine: Engine) -> Iterator[FastAPI]:
    with migrate_engine.begin() as conn:
        for user_id in (people.only_a, people.both):
            conn.execute(
                text("INSERT INTO password_credentials VALUES (:u, :h)"),
                {"u": user_id, "h": passwords.hash_password(PASSWORD)},
            )
    app = create_app()

    @app.get("/api/probe/boom", tags=["probe"])
    def boom() -> None:
        raise RuntimeError("unexpected")

    yield app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(
        app,
        base_url="https://testserver",
        client=(client_address(), 50000),
        raise_server_exceptions=False,
    )


def sign_in(client: TestClient, email: str, password: str = PASSWORD) -> Response:
    return client.post("/api/session", json={"email": email, "password": password})


def session_cookie(response: Response) -> str | None:
    header = response.headers.get("set-cookie")
    return SimpleCookie(header)[auth.COOKIE].value if header else None


def test_signing_in_starts_a_session_in_the_business(people: People, client: TestClient) -> None:
    response = sign_in(client, email_of(people.only_a))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "user_id": str(people.only_a),
        "tenant_id": str(people.a),
        "role": "worker",
        "email": email_of(people.only_a),
        "business_name": "a",
    }
    client.cookies.set(auth.COOKIE, session_cookie(response) or "")
    assert client.get("/api/session").json()["business_name"] == "a"


def test_an_unknown_email_and_a_wrong_password_get_the_same_answer(
    people: People, client: TestClient
) -> None:
    unknown = sign_in(client, f"{uuid.uuid4()}@example.com")
    wrong = sign_in(client, email_of(people.only_a), "not-the-password-1")

    for response in (unknown, wrong):
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
    assert unknown.content == wrong.content == b'{"code":"invalid_credentials"}'
    assert {k: v for k, v in unknown.headers.items() if k != "date"} == {
        k: v for k, v in wrong.headers.items() if k != "date"
    }


@pytest.mark.parametrize("who", ["unknown", "no_password"])
def test_an_account_that_can_not_sign_in_still_costs_a_password_check(
    people: People, client: TestClient, monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    calls: list[str] = []
    real_verify = passwords.verify

    def spy(stored: str | None, password: str) -> bool:
        calls.append(password)
        return real_verify(stored, password)

    monkeypatch.setattr(passwords, "verify", spy)
    email = f"{uuid.uuid4()}@example.com" if who == "unknown" else email_of(people.only_b)

    response = sign_in(client, email)

    assert (response.status_code, len(calls)) == (401, 1)


def test_the_email_is_matched_whatever_its_case_and_spacing(
    people: People, client: TestClient
) -> None:
    assert sign_in(client, f"  {email_of(people.only_a).upper()} ").status_code == 200


def test_a_member_of_several_businesses_lands_in_the_oldest_membership(
    people: People, app_engine: Engine, migrate_engine: Engine, client: TestClient
) -> None:
    user_id = add_user(app_engine)
    older, newer = uuid.uuid7(), uuid.uuid7()
    for membership_id, tenant_id, role in ((newer, people.a, "owner"), (older, people.b, "worker")):
        with tenant_context(tenant_id) as session:
            session.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, role) VALUES (:i, :t, :u, :r)"
                ),
                {"i": membership_id, "t": tenant_id, "u": user_id, "r": role},
            )
    with migrate_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO password_credentials VALUES (:u, :h)"),
            {"u": user_id, "h": passwords.hash_password(PASSWORD)},
        )
    try:
        assert sign_in(client, email_of(user_id)).json()["tenant_id"] == str(people.b)
    finally:
        for tenant_id in (people.a, people.b):
            with tenant_context(tenant_id) as session:
                session.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": user_id})
        with migrate_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM password_credentials WHERE user_id = :u"), {"u": user_id}
            )
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


def test_without_a_business_a_correct_password_is_the_only_way_to_learn_it(
    app_engine: Engine, migrate_engine: Engine, client: TestClient
) -> None:
    user_id = add_user(app_engine)
    with migrate_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO password_credentials VALUES (:u, :h)"),
            {"u": user_id, "h": passwords.hash_password(PASSWORD)},
        )
    try:
        assert sign_in(client, email_of(user_id), "not-the-password-1").status_code == 401
        response = sign_in(client, email_of(user_id))
        assert (response.status_code, response.json()) == (403, {"code": "no_tenant"})
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM password_credentials WHERE user_id = :u"), {"u": user_id}
            )
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


def test_signing_in_replaces_the_session_the_browser_already_had(
    people: People, client: TestClient, app_engine: Engine
) -> None:
    with tenant_context(people.b) as session:
        planted = auth.create(session, people.only_b, ip=None, user_agent=None)
    with tenant_context(people.b) as session:
        other_device = auth.create(session, people.both, ip=None, user_agent=None)
    client.cookies.set(auth.COOKIE, planted)

    assert sign_in(client, email_of(people.both)).status_code == 200

    with app_engine.connect() as conn:
        remaining = set(
            conn.scalars(
                text("SELECT id_hash FROM sessions WHERE id_hash = ANY(:h)"),
                {"h": [auth.hash_token(planted), auth.hash_token(other_device)]},
            )
        )
    assert remaining == {auth.hash_token(other_device)}


def test_attempts_on_one_email_are_limited_even_with_the_right_password(
    people: People, app: FastAPI
) -> None:
    for email, first_ten in ((email_of(people.only_a), 200), (f"{uuid.uuid4()}@example.com", 401)):
        # A new address for every attempt: only the email limit can refuse the eleventh.
        results = [
            sign_in(
                TestClient(app, base_url="https://testserver", client=(client_address(), 1)), email
            ).status_code
            for _ in range(11)
        ]
        assert results == [first_ten] * 10 + [429]


def test_attempts_from_one_address_are_limited_across_emails(client: TestClient) -> None:
    results = [sign_in(client, f"{uuid.uuid4()}@example.com").status_code for _ in range(51)]

    assert results == [401] * 50 + [429]


def test_a_malformed_request_gets_a_code_not_a_description(client: TestClient) -> None:
    for body in ({"email": "ana@studioana.nl"}, {"email": "not-an-email", "password": PASSWORD}):
        response = client.post("/api/session", json=body)
        assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})


def test_an_overlong_password_is_refused_before_any_hashing(
    people: People, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def spy(stored: str | None, password: str) -> bool:
        calls.append(password)
        return False

    monkeypatch.setattr(passwords, "verify", spy)

    response = sign_in(client, email_of(people.only_a), "x" * 257)

    assert (response.status_code, response.json(), calls) == (422, {"code": "invalid_request"}, [])


@pytest.mark.parametrize(
    "email", ["a\x00@example.com", "x<a@evil.example>", "a@localhost", "a" * 250 + "@x.nl"]
)
def test_unusable_emails_are_refused_as_invalid(client: TestClient, email: str) -> None:
    response = sign_in(client, email)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})


def test_every_answer_keeps_the_page_address_to_itself(client: TestClient) -> None:
    responses = [
        client.post(
            "/api/session", json={"email": "ana@studioana.nl", "password": "wrong-password-1"}
        ),
        client.post("/api/session", content="x", headers={"content-type": "text/plain"}),
        client.get("/api/probe/boom"),
    ]

    assert [r.status_code for r in responses] == [401, 415, 500]
    assert all(r.headers.get("referrer-policy") == "no-referrer" for r in responses)


def test_a_rush_of_sign_ins_is_turned_away_without_stalling_the_api(
    people: People, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_verify_hash = passwords._verify_hash

    def slow(stored: str, password: str) -> bool:
        time.sleep(0.5)
        return real_verify_hash(stored, password)

    monkeypatch.setattr(passwords, "_verify_hash", slow)
    with tenant_context(people.a) as session:
        token = auth.create(session, people.only_a, ip=None, user_agent=None)
    statuses: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(48)

    def attempt() -> None:
        client = TestClient(app, base_url="https://testserver", client=(client_address(), 1))
        start.wait()
        status = sign_in(client, f"{uuid.uuid4()}@example.com").status_code
        with lock:
            statuses.append(status)

    threads = [threading.Thread(target=attempt) for _ in range(48)]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    reader = TestClient(app, base_url="https://testserver", client=(client_address(), 1))
    reader.cookies.set(auth.COOKIE, token)
    started = time.monotonic()
    assert reader.get("/api/session").status_code == 200
    assert time.monotonic() - started < 1
    for thread in threads:
        thread.join()

    assert statuses.count(503) >= 40 and set(statuses) <= {401, 503}


def test_the_decoy_hash_costs_what_a_real_one_does() -> None:
    assert passwords.DUMMY.split("$")[3] == passwords.hash_password("anything").split("$")[3]
