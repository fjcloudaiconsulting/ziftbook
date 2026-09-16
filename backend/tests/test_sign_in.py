import threading
import time
import unicodedata
import uuid
from collections.abc import Iterator
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text

from app import auth, passwords
from app.db import SessionLocal, tenant_context
from app.errors import ApiError
from app.main import create_app
from tests.conftest import (
    PASSWORD,
    People,
    add_password,
    add_user,
    email_of,
    member_id,
    new_client,
)


@pytest.fixture
def app(people: People, migrate_engine: Engine) -> FastAPI:
    add_password(migrate_engine, people.only_a, PASSWORD)
    add_password(migrate_engine, people.both, PASSWORD)
    app = create_app()

    @app.get("/api/probe/boom", tags=["probe"])
    def boom() -> None:
        raise RuntimeError("unexpected")

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return new_client(app)


@pytest.fixture
def stranger(app_engine: Engine, migrate_engine: Engine) -> Iterator[uuid.UUID]:
    """A user with a password and no business."""
    user_id = add_user(app_engine)
    add_password(migrate_engine, user_id, PASSWORD)
    yield user_id
    with migrate_engine.begin() as conn:
        conn.execute(text("DELETE FROM password_credentials WHERE user_id = :u"), {"u": user_id})
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


def sign_in(client: TestClient, email: str, password: str = PASSWORD) -> Response:
    return client.post("/api/session", json={"email": email, "password": password})


def test_signing_in_starts_a_session_in_the_business(people: People, client: TestClient) -> None:
    response = sign_in(client, email_of(people.only_a))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "user_id": str(people.only_a),
        "tenant_id": str(people.a),
        "member_id": str(member_id(people.a, people.only_a)),
        "role": "worker",
        "email": email_of(people.only_a),
        "business_name": "a",
        "currency": "EUR",
    }
    client.cookies.set(auth.COOKIE, SimpleCookie(response.headers["set-cookie"])[auth.COOKIE].value)
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
def test_an_account_that_can_not_sign_in_is_still_checked_against_the_decoy(
    people: People, client: TestClient, monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    checked: list[str] = []
    real = passwords._verify_hash

    def spy(stored: str, password: str) -> bool:
        checked.append(stored)
        return real(stored, password)

    monkeypatch.setattr(passwords, "_verify_hash", spy)
    email = f"{uuid.uuid4()}@example.com" if who == "unknown" else email_of(people.only_b)

    response = sign_in(client, email)

    assert (response.status_code, checked) == (401, [passwords.DUMMY])


def test_the_email_is_matched_whatever_its_case_and_spacing(
    people: People, client: TestClient
) -> None:
    assert sign_in(client, f"  {email_of(people.only_a).upper()} ").status_code == 200


@pytest.mark.parametrize(
    "password",
    [
        "  lavender harbour 19  ",  # spaces are part of a password: never stripped
        unicodedata.normalize("NFC", "crème brûlée à la maison"),
    ],
)
def test_a_password_is_matched_exactly_up_to_unicode_composition(
    people: People, app: FastAPI, migrate_engine: Engine, password: str
) -> None:
    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE password_credentials SET hash = :h WHERE user_id = :u"),
            {"h": passwords.hash_password(password), "u": people.only_a},
        )

    # A password manager may send the same accents decomposed (NFD).
    typed = unicodedata.normalize("NFD", password)
    assert sign_in(new_client(app), email_of(people.only_a), typed).status_code == 200
    assert sign_in(new_client(app), email_of(people.only_a), password.strip()).status_code == (
        401 if password != password.strip() else 200
    )


def test_a_member_of_several_businesses_lands_in_the_oldest_membership(
    people: People, stranger: uuid.UUID, client: TestClient
) -> None:
    older, newer = uuid.uuid7(), uuid.uuid7()
    for membership_id, tenant_id, role in ((newer, people.a, "owner"), (older, people.b, "worker")):
        with tenant_context(tenant_id) as session:
            session.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, role) VALUES (:i, :t, :u, :r)"
                ),
                {"i": membership_id, "t": tenant_id, "u": stranger, "r": role},
            )
    try:
        assert sign_in(client, email_of(stranger)).json()["tenant_id"] == str(people.b)
    finally:
        for tenant_id in (people.a, people.b):
            with tenant_context(tenant_id) as session:
                session.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": stranger})


def test_without_a_business_a_correct_password_is_the_only_way_to_learn_it(
    stranger: uuid.UUID, client: TestClient
) -> None:
    assert sign_in(client, email_of(stranger), "not-the-password-1").status_code == 401
    response = sign_in(client, email_of(stranger))
    assert (response.status_code, response.json()) == (403, {"code": "no_tenant"})


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
        results = [sign_in(new_client(app), email).status_code for _ in range(11)]
        assert results == [first_ten] * 10 + [429]


def test_attempts_from_one_address_are_limited_across_emails(
    people: People, client: TestClient
) -> None:
    # Successful sign-ins count as attempts too.
    successes = [sign_in(client, email_of(people.only_a)).status_code for _ in range(5)]
    failures = [sign_in(client, f"{uuid.uuid4()}@example.com").status_code for _ in range(46)]

    assert successes + failures == [200] * 5 + [401] * 45 + [429]


def test_a_malformed_request_gets_a_code_not_a_description(client: TestClient) -> None:
    for body in ({"email": "ana@studioana.nl"}, {"email": "not-an-email", "password": PASSWORD}):
        response = client.post("/api/session", json=body)
        assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})


def test_an_overlong_password_is_refused_before_any_hashing(
    people: People, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked: list[str] = []

    def spy(stored: str, password: str) -> bool:
        checked.append(stored)
        return False

    monkeypatch.setattr(passwords, "_verify_hash", spy)

    response = sign_in(client, email_of(people.only_a), "x" * 257)

    assert (response.status_code, response.json(), checked) == (
        422,
        {"code": "invalid_request"},
        [],
    )


@pytest.mark.parametrize(
    ("email", "usable"),
    [
        ("a\x00@example.com", False),
        ("x<a@evil.example>", False),
        ("a@localhost", False),
        ("a" * 250 + "@x.nl", False),  # 255 characters
        ("{unique}" + "a" * 217 + "@x.nl", True),  # 254 characters
        ("{unique}@café.example", True),  # an internationalised domain
    ],
)
def test_emails_are_accepted_or_refused_by_their_shape(
    client: TestClient, email: str, usable: bool
) -> None:
    # Accepted addresses are looked up, so each run needs its own: the per-email limit remembers.
    response = sign_in(client, email.replace("{unique}", uuid.uuid4().hex))

    # A usable email is looked up (and not found); an unusable one never gets that far.
    assert response.status_code == (401 if usable else 422)


def test_every_answer_keeps_the_page_address_to_itself(client: TestClient) -> None:
    responses = [
        client.get("/api/healthz"),
        sign_in(client, f"{uuid.uuid4()}@example.com"),
        client.post("/api/session", content="x", headers={"content-type": "text/plain"}),
        client.get("/api/probe/boom"),
    ]

    assert [r.status_code for r in responses] == [200, 401, 415, 500]
    assert all(r.headers.get("referrer-policy") == "no-referrer" for r in responses)


def test_a_session_whose_membership_just_went_is_refused_not_an_error(
    people: People, app_engine: Engine
) -> None:
    # The membership is removed after the cookie was checked but before the answer is built.
    with tenant_context(people.a) as session:
        session.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": people.only_a})
        with pytest.raises(ApiError) as error:
            auth.describe(session, people.only_a)
    assert (error.value.status_code, error.value.code) == (401, "unauthenticated")


def test_a_rush_of_sign_ins_is_turned_away_without_stalling_the_api(
    people: People, app: FastAPI, app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = passwords._verify_hash

    def slow(stored: str, password: str) -> bool:
        time.sleep(2)
        return real(stored, password)

    monkeypatch.setattr(passwords, "_verify_hash", slow)
    with tenant_context(people.a) as session:
        token = auth.create(session, people.only_a, ip=None, user_agent=None)
    statuses: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(48)

    # One client for everything, so every request shares the same event loop and thread pool,
    # as they do in one server process.
    try:
        with new_client(app) as shared:
            SessionLocal.configure(bind=app_engine)  # the lifespan bound its own engine

            def attempt() -> None:
                start.wait()
                response = shared.post(
                    "/api/session",
                    json={"email": f"{uuid.uuid4()}@example.com", "password": PASSWORD},
                )
                with lock:
                    statuses.append(response.status_code)

            threads = [threading.Thread(target=attempt) for _ in range(48)]
            for thread in threads:
                thread.start()
            time.sleep(0.5)
            started = time.monotonic()
            # A header, not the client's cookie jar, which the sign-ins would rotate away.
            reader = shared.get("/api/session", headers={"cookie": f"{auth.COOKIE}={token}"})
            waited = time.monotonic() - started
            for thread in threads:
                thread.join()
    finally:
        SessionLocal.configure(bind=app_engine)  # the lifespan unbound it on the way out

    assert reader.status_code == 200 and waited < 1
    assert (statuses.count(503), statuses.count(401)) == (44, 4)


def test_the_decoy_hash_costs_what_a_real_one_does() -> None:
    assert passwords.DUMMY.split("$")[3] == passwords.hash_password("anything").split("$")[3]
