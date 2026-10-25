"""ZIF-146: a client creates a ziftbook account with no business: email first, then name, optional
phone and password from the emailed link. The answer never tells whether an email has an account."""

import secrets
import threading
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import auth, setup
from app.main import create_app
from tests.conftest import (
    People,
    add_user,
    email_of,
    events,
    fresh_email,
    issue_link,
    live,
    new_client,
    signed_in,
    wait_until_blocked,
)
from tests.test_account_sessions import (
    account_sessions_of,
    holding_account_of,
    live_account_session,
    set_cookies,
)

NEW_PASSWORD = "quiet-copper-kettle-7"


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def complete(client: TestClient, token: str, **overrides: Any) -> Any:
    body = {"token": token, "password": NEW_PASSWORD, "name": "Cora Client", **overrides}
    return client.post("/api/account/sign-up/complete", json=body)


def account_jobs(migrate_engine: Engine, email: str) -> list[str]:
    with migrate_engine.connect() as conn:
        return list(
            conn.scalars(
                text("""
                SELECT j.kind FROM jobs j JOIN email_tokens t ON j.payload->>'token_id' = t.id::text
                WHERE t.email = :e
                """),
                {"e": email},
            )
        )


# FENCE (no directory). Wrong impl: an account_by_email check in the route ("you already have an
# account"): the answer would tell anyone which emails have one.
def test_asking_for_an_account_answers_the_same_for_every_email(
    app: FastAPI, people: People, migrate_engine: Engine
) -> None:
    client_account = add_user(migrate_engine, name="Client Only")
    admin = add_user(migrate_engine, name="Platform Admin")
    with migrate_engine.begin() as conn:
        conn.execute(text("INSERT INTO platform_admins (user_id) VALUES (:u)"), {"u": admin})
    seen = []
    try:
        for email in (
            fresh_email(),
            email_of(people.both),
            email_of(client_account),
            email_of(admin),
        ):
            response = new_client(app).post(
                "/api/account/sign-up", json={"email": email, "locale": "en"}
            )
            seen.append(
                (response.status_code, response.content, account_jobs(migrate_engine, email))
            )
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(text("DELETE FROM platform_admins WHERE user_id = :u"), {"u": admin})
    assert seen[0] == seen[1] == seen[2] == seen[3]
    assert seen[0] == (202, b"null", ["email.token"])  # as business sign-up answers


# FENCE. Wrong impl: account sign-up with limits of its own: each kind would give one inbox three
# emails an hour.
def test_one_inbox_gets_three_sign_up_emails_an_hour_across_both_kinds(app: FastAPI) -> None:
    email = fresh_email()
    client = new_client(app)
    statuses = [
        client.post(route, json={"email": email, "locale": "en"}).status_code
        for route in (
            "/api/sign-up",
            "/api/account/sign-up",
            "/api/sign-up",
            "/api/account/sign-up",
        )
    ]
    assert statuses == [202, 202, 202, 429]


# FENCE. Wrong impl: reusing complete_sign_up (a business and an owner membership), or starting a
# business session.
def test_a_link_creates_an_account_with_no_business_and_signs_in_to_it(
    app: FastAPI, app_engine: Engine, migrate_engine: Engine
) -> None:
    email = fresh_email()
    token = issue_link(app_engine, "account_sign_up", email)
    assert token is not None
    client = new_client(app)

    response = complete(client, token, phone="+31 6 1234 5678")

    assert response.status_code == 201, response.json()
    assert response.json() == {"email": email, "name": "Cora Client", "phone": "+31612345678"}
    assert set(set_cookies(response)) == {auth.ACCOUNT_COOKIE}
    assert client.get("/api/account").json() == response.json()
    assert client.get("/api/session").status_code == 401  # no business
    with migrate_engine.connect() as conn:
        user = conn.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": email})
    assert [
        r["tenant_id"] for r in events(migrate_engine, action="account_created", actor_user_id=user)
    ] == [None]
    # And it signs in again with that password, on the booking page.
    again = new_client(app).post(
        "/api/account/session", json={"email": email, "password": NEW_PASSWORD}
    )
    assert again.status_code == 200
    assert not live(app_engine, token, "account_sign_up")  # used up


def test_a_business_sign_up_link_creates_no_account_here(app: FastAPI, app_engine: Engine) -> None:
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None

    response = complete(new_client(app), token)

    assert (response.status_code, response.json()) == (400, {"code": "invalid_token"})
    assert live(app_engine, token, "sign_up")


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"phone": "0612345678"}, "phone_invalid"),  # no country to read a local number in
        ({"password": "short"}, "password_too_short"),
        ({"name": "ㅤ"}, "invalid_request"),
    ],
)
def test_a_refused_detail_leaves_the_link_usable(
    app: FastAPI, app_engine: Engine, overrides: dict[str, str], code: str
) -> None:
    token = issue_link(app_engine, "account_sign_up", fresh_email())
    assert token is not None

    response = complete(new_client(app), token, **overrides)

    assert (response.status_code, response.json()) == (422, {"code": code})
    assert live(app_engine, token, "account_sign_up")


# FENCE (as business sign-up, ZIF-70). Wrong impl: no setup check: a stranger's account on a fresh
# install would close first-run setup for good.
def test_no_account_is_created_while_setup_is_needed(
    app: FastAPI, app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = issue_link(app_engine, "account_sign_up", fresh_email())
    assert token is not None
    monkeypatch.setattr(setup, "needed", lambda: True)

    response = complete(new_client(app), token)

    assert (response.status_code, response.json()) == (409, {"code": "setup_required"})
    assert live(app_engine, token, "account_sign_up")


def test_an_email_that_gained_an_account_meanwhile_is_told_so_only_with_its_link(
    app: FastAPI, app_engine: Engine, migrate_engine: Engine
) -> None:
    email = fresh_email()
    token = issue_link(app_engine, "account_sign_up", email)
    assert token is not None
    add_user(migrate_engine, email)

    response = complete(new_client(app), token)

    assert (response.status_code, response.json()) == (409, {"code": "account_exists"})


# FENCE (security N11, one browser, one person). Wrong impl: completing a sign-up without ending
# the browser's previous account session, or another person's business session.
def test_completing_ends_the_previous_persons_sessions_on_this_browser(
    app: FastAPI, people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    previous = add_user(migrate_engine, name="Previous Person")
    client, old = holding_account_of(app, migrate_engine, previous)
    business = signed_in(app, people.a, people.both).cookies.get(auth.COOKIE)
    assert business is not None
    client.cookies.set(auth.COOKIE, business)
    token = issue_link(app_engine, "account_sign_up", fresh_email())
    assert token is not None

    response = complete(client, token)

    assert response.status_code == 201
    assert not live_account_session(migrate_engine, old)
    assert account_sessions_of(migrate_engine, previous) == 0
    assert client.get("/api/session").status_code == 401  # the other person's ended
    assert auth.COOKIE in set_cookies(response)


# FENCE (fold re-verify NB-1). Wrong impl: completing a sign-up that writes account_sessions (its
# new row, and the purge's locks on expired ones) before deleting the browser's business session of
# another person, against the one lock order: that person signing out everywhere at the same
# moment deadlocks with it.
def test_completing_and_the_previous_persons_sign_out_everywhere_never_deadlock(
    app: FastAPI,
    people: People,
    app_engine: Engine,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = people.both
    browser = signed_in(app, people.a, previous)
    elsewhere = signed_in(app, people.b, previous).cookies.get(auth.COOKIE)
    assert elsewhere is not None
    with migrate_engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO account_sessions (id_hash, user_id, expires_at)
            VALUES (:h, :u, now() - interval '1 hour')
            """),
            {"h": secrets.token_bytes(32), "u": previous},
        )
    token = issue_link(app_engine, "account_sign_up", fresh_email())
    assert token is not None
    paused, release = threading.Event(), threading.Event()
    start = auth.start_account

    def hold_after_the_account_session(*args: Any, **kwargs: Any) -> str | None:
        started = start(*args, **kwargs)
        paused.set()
        assert release.wait(timeout=20)
        return started

    monkeypatch.setattr(auth, "start_account", hold_after_the_account_session)
    answers: list[Any] = []
    completing = threading.Thread(target=lambda: answers.append(complete(browser, token)))
    completing.start()
    assert paused.wait(timeout=20)
    failures: list[BaseException] = []

    def sign_out_everywhere() -> None:
        try:
            params = {"u": previous, "h": auth.hash_token(elsewhere)}
            with app_engine.connect() as conn, conn.begin():
                for step in auth.EVERY_SESSION:
                    conn.execute(step, params)
        except BaseException as error:  # a deadlock victim raises here
            failures.append(error)

    everywhere = threading.Thread(target=sign_out_everywhere)
    everywhere.start()
    try:
        wait_until_blocked(migrate_engine, 1)
    finally:
        release.set()
        completing.join(timeout=20)
        everywhere.join(timeout=20)

    assert failures == []
    assert answers[0].status_code == 201
