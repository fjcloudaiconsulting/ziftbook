"""ZIF-70: first-run setup (migration 0038, app/setup.py) and the sign-up guard while it is due.

Everything here runs on its own empty-install database (conftest.empty_install), except where a
test says it uses the shared, installed one.
"""

import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app import passwords, setup
from app.admin_auth import ADMIN_COOKIE
from app.main import create_app
from tests.conftest import (
    PASSWORD,
    EmptyInstall,
    fresh_address,
    issue_link,
    new_client,
    wait_until_blocked,
)

TOKEN = "a-setup-token-of-at-least-32-characters"
ADMIN_EMAIL = "admin@example.com"
COMPLETE_SETUP = text("SELECT complete_setup(:email, :hash, 'Admin', 'en')")


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("ZIF_SETUP_TOKEN", TOKEN)
    return TOKEN


@pytest.fixture
def app(empty_install: EmptyInstall) -> FastAPI:
    return create_app()


def body(token: str = TOKEN, email: str = ADMIN_EMAIL, password: str = PASSWORD) -> dict[str, str]:
    return {"token": token, "email": email, "password": password, "name": "Ada", "locale": "en"}


def post_setup(client: TestClient, payload: dict[str, Any]) -> Response:
    # Encoded here: json.dumps escapes a lone surrogate the way a browser can send it.
    return client.post(
        "/api/setup", content=json.dumps(payload), headers={"content-type": "application/json"}
    )


def count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        found: int = conn.scalar(text(f"SELECT count(*) FROM {table}"))
    return found


def needs_setup(engine: Engine) -> bool:
    with engine.connect() as conn:
        found: bool = conn.scalar(text("SELECT needs_setup()"))
    return found


@pytest.fixture(scope="module")
def password_hash() -> str:
    return passwords.hash_password(PASSWORD)


# guard
def test_needs_setup_flips(
    empty_install: EmptyInstall, app: FastAPI, configured: str, app_engine: Engine
) -> None:
    assert needs_setup(empty_install.app) is True
    assert new_client(app).get("/api/setup").json() == {"needs_setup": True}
    assert post_setup(new_client(app), body()).status_code == 201
    assert needs_setup(empty_install.app) is False
    answer = new_client(app).get("/api/setup")
    assert answer.json() == {"needs_setup": False}
    assert answer.headers["cache-control"] == "no-store"
    assert needs_setup(app_engine) is False  # the shared test database is installed


def setup_in_thread(
    engine: Engine, email: str, password_hash: str
) -> tuple[threading.Thread, list[Any]]:
    """complete_setup on its own connection in a thread; the list gets what it returned."""
    returned: list[Any] = []

    def run() -> None:
        with engine.begin() as conn:
            returned.append(conn.scalar(COMPLETE_SETUP, {"email": email, "hash": password_hash}))

    thread = threading.Thread(target=run)
    thread.start()
    return thread, returned


# FENCE. Wrong impls: no LOCK TABLE (the second never waits: wait_until_blocked raises), no second
# needs_setup() after the lock (the second returns a uuid: two users, two admins).
def test_concurrent_setups_make_one_admin(empty_install: EmptyInstall, password_hash: str) -> None:
    with empty_install.app.connect() as first:
        first.begin()
        assert first.scalar(COMPLETE_SETUP, {"email": "a@example.com", "hash": password_hash})
        thread, returned = setup_in_thread(empty_install.app, "b@example.com", password_hash)
        try:
            wait_until_blocked(empty_install.migrate, 1)
        finally:
            first.commit()
            thread.join(10)
    assert returned == [None]
    assert count(empty_install.migrate, "users") == 1
    assert count(empty_install.migrate, "platform_admins") == 1


# guard: end-to-end wiring; the fence above carries the race.
def test_concurrent_setup_requests(app: FastAPI, configured: str) -> None:
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def run(email: str) -> None:
        client = new_client(app)
        barrier.wait()
        statuses.append(post_setup(client, body(email=email)).status_code)

    threads = [threading.Thread(target=run, args=(e,)) for e in ("a@example.com", "b@example.com")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(statuses) == [201, 409]


# FENCE. Wrong impl: an advisory lock in place of the table lock (a sign-up's INSERT would not wait,
# and could make the first user while setup is making it too). A different email, so the insert
# can't be waiting on the unique index instead.
def test_setup_lock_blocks_a_concurrent_user_insert(
    empty_install: EmptyInstall, password_hash: str
) -> None:
    inserted = threading.Event()

    # As the migrate role, as complete_sign_up and accept_invite insert: the app role has no INSERT
    # on users (ZB-10).
    def insert() -> None:
        with empty_install.migrate.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:id, 'b@example.com')"),
                {"id": uuid.uuid7()},
            )
        inserted.set()

    with empty_install.app.connect() as first:
        first.begin()
        assert first.scalar(COMPLETE_SETUP, {"email": "a@example.com", "hash": password_hash})
        thread = threading.Thread(target=insert)
        thread.start()
        try:
            wait_until_blocked(empty_install.migrate, 1)
            assert not inserted.is_set()
        finally:
            first.commit()
            thread.join(10)
    assert inserted.is_set()
    assert count(empty_install.migrate, "users") == 2


# FENCE. Wrong impl: no isolation assert, which lets two REPEATABLE READ callers both see no users.
def test_complete_setup_refuses_repeatable_read(
    empty_install: EmptyInstall, password_hash: str
) -> None:
    with empty_install.app.connect() as conn:
        conn.begin()
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        with pytest.raises(DBAPIError, match="complete_setup needs READ COMMITTED"):
            conn.scalar(COMPLETE_SETUP, {"email": "a@example.com", "hash": password_hash})
    assert count(empty_install.migrate, "users") == 0


# FENCE. Wrong impls: comparing the token, or checking it is configured, before the 409: a guess
# stays an oracle after setup, and removing the token afterwards turns the 409 into a 503.
def test_installed_answers_409_before_the_token(
    app: FastAPI, configured: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert post_setup(new_client(app), body()).status_code == 201
    again = post_setup(new_client(app), body(token="x" * 40, email="b@example.com"))
    assert (again.status_code, again.json()) == (409, {"code": "already_set_up"})
    monkeypatch.delenv("ZIF_SETUP_TOKEN")  # operators may remove it once setup is done
    removed = post_setup(new_client(app), body(token="", email="b@example.com"))
    assert (removed.status_code, removed.json()) == (409, {"code": "already_set_up"})


DEV_TOKEN = "dev-only-setup-token-not-a-secret-0000"  # docker-compose.yaml's development value


# fence: the development token is refused on a public URL and accepted on localhost.
def test_dev_token_refused_on_a_public_url(
    app: FastAPI, empty_install: EmptyInstall, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZIF_SETUP_TOKEN", DEV_TOKEN)
    monkeypatch.setenv("ZIF_APP_URL", "https://app.example.com")
    assert setup.token_state() == "dev"
    refused = post_setup(new_client(app), body(token=DEV_TOKEN))
    assert (refused.status_code, refused.json()) == (503, {"code": "setup_not_configured"})
    assert count(empty_install.migrate, "users") == 0
    for local in ["http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000"]:
        monkeypatch.setenv("ZIF_APP_URL", local)
        assert setup.token_state() == "set", local


# FENCE. Wrong impls: compare_digest(b"", b"") accepting an empty token, or no minimum length.
def test_no_or_weak_token_refuses(
    app: FastAPI, empty_install: EmptyInstall, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ZIF_SETUP_TOKEN", raising=False)
    unset = post_setup(new_client(app), body(token=""))
    assert (unset.status_code, unset.json()) == (503, {"code": "setup_not_configured"})
    weak = "w" * 31
    monkeypatch.setenv("ZIF_SETUP_TOKEN", weak)
    refused = post_setup(new_client(app), body(token=weak))
    assert (refused.status_code, refused.json()) == (503, {"code": "setup_not_configured"})
    assert count(empty_install.migrate, "users") == 0


# FENCE. Wrong impls: compare_digest on str (TypeError on non-ASCII), no max_length, or the guess
# in the WARNING line. A JSON lone surrogate never reaches the compare: pydantic refuses it as a str
# (422), so surrogatepass there is a backstop no request can reach.
def test_token_input_never_500s(
    app: FastAPI,
    empty_install: EmptyInstall,
    configured: str,
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    for guess in ("é" * 40, "y" * 40):
        refused = post_setup(new_client(app), body(token=guess))
        assert (refused.status_code, refused.json()) == (403, {"code": "invalid_setup_token"})
    for guess in ("\ud800", "x" * 201):
        invalid = post_setup(new_client(app), body(token=guess))
        assert (invalid.status_code, invalid.json()) == (422, {"code": "invalid_request"})
    rejected = [line for line in log_lines() if line["msg"] == "setup token rejected"]
    assert [line["level"] for line in rejected] == ["WARNING", "WARNING"]
    logged = json.dumps(log_lines(), ensure_ascii=False)
    for secret in ("é" * 40, "y" * 40, TOKEN, ADMIN_EMAIL):
        assert secret not in logged
    assert count(empty_install.migrate, "users") == 0


# FENCE. Wrong impls: a global key (a stranger locks the operator out), or limiting after the
# compare (guessing is unlimited).
def test_setup_rate_limit_is_per_ip(app: FastAPI, configured: str) -> None:
    client = new_client(app, fresh_address())
    for _ in range(10):
        assert post_setup(client, body(token="x" * 40)).status_code == 403
    limited = post_setup(client, body(token="x" * 40))
    assert (limited.status_code, limited.json()) == (429, {"code": "rate_limited"})
    assert post_setup(new_client(app), body(token="x" * 40)).status_code == 403


# guard
def test_setup_creates_and_signs_in_an_admin(
    app: FastAPI, empty_install: EmptyInstall, configured: str
) -> None:
    client = new_client(app)
    done = post_setup(client, body(email="Admin@Example.com"))
    assert done.status_code == 201, done.content
    assert done.headers["cache-control"] == "no-store"
    user_id = done.json()["user_id"]
    assert done.json() == {
        "user_id": user_id,
        "email": ADMIN_EMAIL,
        "name": "Ada",
        "second_factor": "enrolment_required",  # setup issues no enrolment ticket (ZIF-71)
    }
    cookie = done.headers["set-cookie"]
    assert cookie.startswith(f"{ADMIN_COOKIE}=") and "SameSite=strict" in cookie
    assert client.get("/api/admin/session").json()["user_id"] == user_id
    with empty_install.migrate.connect() as conn:
        # No business at all, so no membership (memberships' forced RLS needs a tenant to count).
        assert conn.scalar(text("SELECT count(*) FROM tenants")) == 0
        assert conn.scalar(text("SELECT user_id::text FROM platform_admins")) == user_id
        audit = conn.execute(
            text("SELECT action, kind, actor_user_id::text, target FROM platform_audit_events")
        ).all()
    assert [tuple(row) for row in audit] == [
        ("setup_completed", "auth", user_id, f"user:{user_id}")
    ]
    again = new_client(app).post(
        "/api/admin/session", json={"email": ADMIN_EMAIL, "password": PASSWORD}
    )
    assert again.status_code == 200


# guard
def test_setup_password_rules(app: FastAPI, empty_install: EmptyInstall, configured: str) -> None:
    short = post_setup(new_client(app), body(password="short"))
    assert (short.status_code, short.json()) == (422, {"code": "password_too_short"})
    assert count(empty_install.migrate, "users") == 0


def complete_sign_up(client: TestClient, token: str) -> Response:
    return client.post(
        "/api/sign-up/complete",
        json={
            "token": token,
            "password": PASSWORD,
            "name": "Ana Silva",
            "business_name": "Studio Ana",
            "country": "NL",
        },
    )


# FENCE. Wrong impls: no guard (a stranger's sign-up closes setup for good), or a guard placed after
# the link is used up (the link is dead once setup is done).
def test_sign_up_waits_for_setup(
    app: FastAPI, empty_install: EmptyInstall, configured: str
) -> None:
    link = issue_link(empty_install.app, "sign_up", "ana@example.com")
    assert link is not None
    waiting = complete_sign_up(new_client(app), link)
    assert (waiting.status_code, waiting.json()) == (409, {"code": "setup_required"})
    assert count(empty_install.migrate, "users") == 0
    assert post_setup(new_client(app), body()).status_code == 201
    assert complete_sign_up(new_client(app), link).status_code == 201


UNREACHABLE = "postgresql+psycopg://ziftbook_app:ziftbook_app@10.255.255.1:5432/ziftbook"


# FENCE. Wrong impls: an unconditional ERROR (fires forever once the operator removes the token), a
# startup that fails or hangs on a database error, or the token value in a log.
@pytest.mark.parametrize(
    ("token", "database", "state", "errors"),
    [
        (None, "empty", "unset", 1),
        (TOKEN, "empty", "set", 0),
        (None, "installed", "unset", 0),
        ("w" * 31, "empty", "weak", 1),
        (None, "unreachable", "unset", 0),
    ],
    ids=["unset-empty", "set-empty", "unset-installed", "weak-empty", "unreachable"],
)
def test_startup_setup_token_log(
    empty_install: EmptyInstall,
    app_engine: Engine,
    log_lines: Callable[[], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
    database: str,
    state: str,
    errors: int,
) -> None:
    if token is None:
        monkeypatch.delenv("ZIF_SETUP_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ZIF_SETUP_TOKEN", token)
    url = {
        "empty": empty_install.app_url,
        "installed": app_engine.url.render_as_string(hide_password=False),
        "unreachable": UNREACHABLE,
    }[database]
    monkeypatch.setenv("ZIF_DATABASE_URL", url)
    started = time.monotonic()
    with new_client(create_app()):
        pass
    assert time.monotonic() - started < 10
    lines = log_lines()
    assert [line["setup_token"] for line in lines if line["msg"] == "api started"] == [state]
    refused = [
        line
        for line in lines
        if line["msg"] == "setup token unset, weak or dev value: first-run setup refused"
    ]
    assert [line["level"] for line in refused] == ["ERROR"] * errors
    if token:
        assert token not in json.dumps(lines)
