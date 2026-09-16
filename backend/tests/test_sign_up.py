import json
import secrets
import uuid
from collections.abc import Iterator
from http.cookies import SimpleCookie
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text

from app import auth, business_settings
from app.countries import COUNTRIES, Country
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    EXPIRE,
    PASSWORD,
    People,
    delete_services,
    email_of,
    failing,
    fresh_email,
    issue_link,
    jobs_for,
    live,
    mailed,
    new_client,
    saved_settings,
    signed_in,
    token_in,
)

MISSING = object()


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
            session.execute(text("DELETE FROM settings"))  # they reference the business
            session.execute(text("DELETE FROM memberships"))
    with migrate_engine.begin() as conn:
        ids = {"u": [b["user_id"] for b in created], "t": [b["tenant_id"] for b in created]}
        conn.execute(text("DELETE FROM password_credentials WHERE user_id::text = ANY(:u)"), ids)
        conn.execute(text("DELETE FROM users WHERE id::text = ANY(:u)"), ids)
        delete_services(conn, ids["t"])
        conn.execute(text("DELETE FROM tenants WHERE id::text = ANY(:t)"), ids)


def complete(
    client: TestClient,
    token: str,
    password: str = PASSWORD,
    business: str = "Studio Ana",
    country: str | None | object = MISSING,
) -> Response:
    body: dict[str, Any] = {"token": token, "password": password, "business_name": business}
    if country is not MISSING:
        body["country"] = country
    # Encoded here: json.dumps escapes a lone surrogate the way a browser can send it.
    return client.post(
        "/api/sign-up/complete",
        content=json.dumps(body),
        headers={"content-type": "application/json"},
    )


def created(response: Response, businesses: list[dict[str, Any]]) -> dict[str, Any]:
    assert response.status_code == 201, response.content
    session: dict[str, Any] = response.json()
    businesses.append(session)
    return session


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

    token = token_in(mailed(email))

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
    assert live(app_engine, token, "sign_up")


@pytest.mark.parametrize(
    "business", ["", "   ", "x" * 101, "\u200b", "Studio\x00Ana", "Studio\x07"]
)
def test_a_business_needs_a_printable_name(app: FastAPI, app_engine: Engine, business: str) -> None:
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None

    response = complete(new_client(app), token, business=business)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert live(app_engine, token, "sign_up")


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
    assert live(app_engine, token, "sign_up")


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


def test_every_country_literal_has_defaults() -> None:
    # The registry backs every value of the API's enum, and nothing else.
    assert COUNTRIES.keys() == set(get_args(Country))


@pytest.mark.parametrize("country", [MISSING, None], ids=["absent", "null"])
def test_completing_sign_up_with_no_country_creates_a_dutch_business(
    app: FastAPI,
    app_engine: Engine,
    migrate_engine: Engine,
    businesses: list[dict[str, Any]],
    country: str | None | object,
) -> None:
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None

    session = created(complete(new_client(app), token, country=country), businesses)

    with migrate_engine.connect() as conn:
        business = conn.execute(
            text("SELECT country, currency FROM tenants WHERE id = :t"),
            {"t": session["tenant_id"]},
        ).one()
    assert (business.country, business.currency) == ("NL", "EUR")
    assert saved_settings(uuid.UUID(session["tenant_id"])) == {
        "timezone": "Europe/Amsterdam",
        "language": "nl",
    }
    owner = signed_in(app, uuid.UUID(session["tenant_id"]), uuid.UUID(session["user_id"]))
    assert owner.get("/api/settings").json()["language"] == "nl"


@pytest.mark.parametrize(
    ("country", "currency", "timezone", "language"),
    [
        ("NL", "EUR", "Europe/Amsterdam", "nl"),
        ("BR", "BRL", "America/Sao_Paulo", "pt"),
        ("PT", "EUR", "Europe/Lisbon", "pt"),
        ("GB", "GBP", "Europe/London", "en"),
        ("US", "USD", "America/New_York", "en"),
    ],
)
def test_completing_sign_up_uses_the_countrys_defaults(
    app: FastAPI,
    app_engine: Engine,
    migrate_engine: Engine,
    businesses: list[dict[str, Any]],
    country: str,
    currency: str,
    timezone: str,
    language: str,
) -> None:
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None

    session = created(complete(new_client(app), token, country=country), businesses)

    with migrate_engine.connect() as conn:
        business = conn.execute(
            text("SELECT country, currency FROM tenants WHERE id = :t"),
            {"t": session["tenant_id"]},
        ).one()
    assert (business.country, business.currency) == (country, currency)
    # Written explicitly, even where it equals the default (GB, US): a later default change must
    # not silently reach a business that already chose it.
    assert saved_settings(uuid.UUID(session["tenant_id"])) == {
        "timezone": timezone,
        "language": language,
    }


@pytest.mark.parametrize(
    ("link_locale", "country", "language"), [("pt", "GB", "en"), ("nl", "BR", "pt")]
)
def test_the_starting_language_comes_from_the_country_not_the_sign_up_link(
    app: FastAPI,
    app_engine: Engine,
    businesses: list[dict[str, Any]],
    link_locale: str,
    country: str,
    language: str,
) -> None:
    # The link's locale is the page the person signed up on; the business's language is the
    # country's, not that page.
    token = issue_link(app_engine, "sign_up", fresh_email(), link_locale)
    assert token is not None

    session = created(complete(new_client(app), token, country=country), businesses)

    assert saved_settings(uuid.UUID(session["tenant_id"]))["language"] == language


@pytest.mark.parametrize("bad", ["br", "DE", "", "NLD", 5, ["NL"]])
def test_an_invalid_country_is_refused_without_hashing_and_the_link_still_works(
    app: FastAPI, app_engine: Engine, no_hashing: list[str], bad: Any
) -> None:
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None

    response = complete(new_client(app), token, country=bad)

    assert (response.status_code, response.json(), no_hashing) == (
        422,
        {"code": "invalid_request"},
        [],
    )
    assert live(app_engine, token, "sign_up")


def test_a_business_created_with_a_country_reports_its_currency(
    app: FastAPI, app_engine: Engine, businesses: list[dict[str, Any]]
) -> None:
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None
    client = new_client(app)

    response = complete(client, token, country="BR")
    session = created(response, businesses)

    assert session["currency"] == "BRL"
    client.cookies.set(auth.COOKIE, SimpleCookie(response.headers["set-cookie"])[auth.COOKIE].value)
    assert client.get("/api/session").json()["currency"] == "BRL"


def test_a_failure_saving_starting_settings_leaves_no_business(
    app: FastAPI, app_engine: Engine, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = fresh_email()
    token = issue_link(app_engine, "sign_up", email)
    assert token is not None
    failing(monkeypatch, business_settings, "save")

    response = complete(new_client(app), token)

    assert response.status_code == 500
    with migrate_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users WHERE email = :e"), {"e": email}) == 0
    assert live(app_engine, token, "sign_up")
