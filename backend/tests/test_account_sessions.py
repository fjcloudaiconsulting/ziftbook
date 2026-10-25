"""ZIF-146: a person's own account session, beside the business one.

One sign-in issues both sessions; any sign-out ends both on that browser; no console route ever
authenticates with an account session, and no account route with a business session. Docs:
CONTRIBUTING.md "Tenant-owned tables" (account_sessions) and "Client records".
"""

import hashlib
import re
import secrets
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from http.cookies import SimpleCookie
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from app import auth, passwords
from app.main import create_app
from tests.conftest import (
    PASSWORD,
    People,
    add_password,
    add_user,
    email_of,
    events,
    issue_link,
    member_id,
    new_client,
    signed_in,
)
from tests.test_names import routes_behind_signed_in

NEW_PASSWORD = "quiet-copper-kettle-7"


@pytest.fixture
def app(people: People, migrate_engine: Engine) -> FastAPI:
    add_password(migrate_engine, people.both, PASSWORD)
    add_password(migrate_engine, people.only_a, PASSWORD)
    return create_app()


@pytest.fixture
def client_only(migrate_engine: Engine) -> Iterator[uuid.UUID]:
    """A person with a password and no business: a client account."""
    user_id = add_user(migrate_engine, name="Cora Client")
    add_password(migrate_engine, user_id, PASSWORD)
    yield user_id
    with migrate_engine.begin() as conn:
        conn.execute(text("DELETE FROM password_credentials WHERE user_id = :u"), {"u": user_id})
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


def account_sign_in(client: TestClient, email: str, password: str = PASSWORD) -> Response:
    return client.post("/api/account/session", json={"email": email, "password": password})


def console_sign_in(client: TestClient, email: str, password: str = PASSWORD) -> Response:
    return client.post("/api/session", json={"email": email, "password": password})


def set_cookies(response: Response) -> dict[str, SimpleCookie]:
    found: dict[str, SimpleCookie] = {}
    for header in response.headers.get_list("set-cookie"):
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(header)
        for name in cookie:
            found[name] = cookie
    return found


def account_sessions_of(migrate_engine: Engine, user_id: uuid.UUID) -> int:
    with migrate_engine.connect() as conn:
        count: int = conn.scalar(
            text("SELECT count(*) FROM account_sessions WHERE user_id = :u"), {"u": user_id}
        )
    return count


def sessions_of(migrate_engine: Engine, user_id: uuid.UUID) -> int:
    with migrate_engine.connect() as conn:
        count: int = conn.scalar(
            text("SELECT count(*) FROM sessions WHERE user_id = :u"), {"u": user_id}
        )
    return count


def account_client(app: FastAPI, migrate_engine: Engine, user_id: uuid.UUID) -> TestClient:
    """A browser holding only a live account session for user_id, stored as migrate."""
    token = secrets.token_urlsafe(32)
    with migrate_engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO account_sessions (id_hash, user_id, expires_at)
            VALUES (:h, :u, now() + interval '30 days')
            """),
            {"h": auth.hash_token(token), "u": user_id},
        )
    client = new_client(app)
    client.cookies.set(auth.ACCOUNT_COOKIE, token)
    return client


# FENCE. Wrong impl: reuse auth.create for a member-less person (403 no_tenant, no session at all).
def test_a_person_without_a_business_signs_in_and_reads_their_own_details(
    app: FastAPI, client_only: uuid.UUID
) -> None:
    client = new_client(app)

    response = account_sign_in(client, email_of(client_only))

    assert response.status_code == 200, response.json()
    assert response.json() == {"email": email_of(client_only), "name": "Cora Client", "phone": None}
    assert response.headers["cache-control"] == "no-store"
    cookies = set_cookies(response)
    assert set(cookies) == {auth.ACCOUNT_COOKIE}
    morsel = cookies[auth.ACCOUNT_COOKIE][auth.ACCOUNT_COOKIE]
    assert (morsel["secure"], morsel["httponly"], morsel["samesite"], morsel["path"]) == (
        True,
        True,
        "strict",
        "/",
    )
    assert client.get("/api/account").json() == response.json()
    # Still no business: the console answers as for anyone signed out.
    assert client.get("/api/session").status_code == 401


# FENCE (ruling 6). Wrong impl: today's auth.start, which starts the business session only.
def test_a_console_sign_in_also_fills_in_the_booking_page(
    app: FastAPI, people: People, migrate_engine: Engine
) -> None:
    client = new_client(app)

    response = console_sign_in(client, email_of(people.both))

    assert response.status_code == 200
    assert set(set_cookies(response)) == {auth.COOKIE, auth.ACCOUNT_COOKIE}
    assert client.get("/api/account").json() == {
        "email": email_of(people.both),
        "name": "Test Person",
        "phone": None,
    }
    assert account_sessions_of(migrate_engine, people.both) == 1


# FENCE. Wrong impl: a sign-out that ends only its own kind of session.
@pytest.mark.parametrize("route", ["/api/session", "/api/account/session"])
def test_any_sign_out_ends_both_sessions_on_this_browser(
    app: FastAPI, people: People, migrate_engine: Engine, route: str
) -> None:
    client = new_client(app)
    assert console_sign_in(client, email_of(people.both)).status_code == 200

    response = client.request("DELETE", route, json={})

    assert response.status_code == 204
    cleared = set_cookies(response)
    assert {auth.COOKIE, auth.ACCOUNT_COOKIE} <= set(cleared)
    assert (
        sessions_of(migrate_engine, people.both),
        account_sessions_of(migrate_engine, people.both),
    ) == (0, 0)
    assert client.get("/api/account").status_code == 401
    assert client.get("/api/session").status_code == 401
    rows = events(migrate_engine, action="account_signed_out", actor_user_id=people.both)
    assert [row["tenant_id"] for row in rows] == [None]


# FENCE (security N1). Wrong impl: auth.resolve falling back to the account session when the
# business cookie is missing. Every route outside /api/account and /api/public, not only those the
# signed_in walk finds (photos.owner_tenant calls auth.resolve in its body).
NO_SESSION_NEEDED = {
    ("GET", "/api/healthz"),
    ("GET", "/api/health/dependencies"),
    ("GET", "/api/setup"),
}
# Signing out needs no session and ends both kinds on this browser (ruling 6): tested on its own.
SIGN_OUT = ("DELETE", "/api/session")


def test_no_console_route_answers_an_account_session(
    app: FastAPI, people: People, migrate_engine: Engine
) -> None:
    # An owner's own account session: a fallback from it to their membership would open everything.
    client = account_client(app, migrate_engine, people.both)
    routes = sorted(
        (method, route.path)
        for top in app.routes
        if hasattr(top, "original_router")
        for route in top.original_router.routes
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
        if not route.path.startswith(("/api/account", "/api/public"))
        and (method, route.path) != SIGN_OUT
    )
    # The floor guards the walk: every route behind a session is in it.
    assert set(routes_behind_signed_in(app)) <= set(routes) and len(routes) >= 60

    answered = []
    for method, path in routes:
        url = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        response = client.request(method, url, json=None if method == "GET" else {})
        if 200 <= response.status_code < 300:
            answered.append((method, path))
    assert set(answered) == NO_SESSION_NEEDED
    # The account session is untouched by all of it.
    assert client.get("/api/account").status_code == 200


# FENCE. Wrong impl: auth.RESOLVE also looking the hash up in account_sessions.
def test_an_account_token_in_the_business_cookie_is_refused(
    app: FastAPI, migrate_engine: Engine, client_only: uuid.UUID
) -> None:
    client = account_client(app, migrate_engine, client_only)
    token = client.cookies.get(auth.ACCOUNT_COOKIE)
    assert token is not None
    stolen = new_client(app)
    stolen.cookies.set(auth.COOKIE, token)

    assert stolen.get("/api/session").status_code == 401


# FENCE. Wrong impl: the account routes accepting the business session.
def test_the_account_routes_refuse_a_business_session(app: FastAPI, people: People) -> None:
    client = signed_in(app, people.a, people.both)

    assert client.get("/api/session").status_code == 200
    assert client.get("/api/account").status_code == 401


# FENCE. Wrong impl: omitting 0057's REVOKE ALL (0001's default privileges grant DML).
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM account_sessions",
        "INSERT INTO account_sessions (id_hash, user_id, expires_at) "
        "VALUES (decode(repeat('ab', 32), 'hex'), gen_random_uuid(), now())",
        "UPDATE account_sessions SET expires_at = now()",
        "DELETE FROM account_sessions",
    ],
)
def test_the_app_role_has_no_privilege_on_account_sessions(
    app_engine: Engine, statement: str
) -> None:
    with pytest.raises(ProgrammingError) as error, app_engine.begin() as conn:
        conn.execute(text(statement))
    assert isinstance(error.value.orig, InsufficientPrivilege)


# FENCE. Wrong impl: lifetimes the caller chooses, or a resolver that ignores the idle limit.
@pytest.mark.parametrize(
    ("started", "expires", "last_seen", "live", "fresh"),
    [
        ("0 hours", "30 days", "0 hours", True, True),
        ("25 hours", "29 days", "1 hour", True, False),
        ("10 days", "20 days", "8 days", False, False),  # idle
        ("31 days", "-1 hour", "1 hour", False, False),  # past its absolute expiry
    ],
)
def test_resolve_account_session_fixes_the_lifetimes(
    migrate_engine: Engine,
    app_engine: Engine,
    client_only: uuid.UUID,
    started: str,
    expires: str,
    last_seen: str,
    live: bool,
    fresh: bool,
) -> None:
    id_hash = secrets.token_bytes(32)
    with migrate_engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO account_sessions (id_hash, user_id, started_at, expires_at, last_seen_at)
            VALUES (:h, :u, now() - CAST(:s AS interval), now() + CAST(:e AS interval),
                    now() - CAST(:l AS interval))
            """),
            {"h": id_hash, "u": client_only, "s": started, "e": expires, "l": last_seen},
        )
    with app_engine.begin() as conn:
        row = conn.execute(
            text("SELECT user_id, fresh FROM resolve_account_session(:h)"), {"h": id_hash}
        ).first()
    assert (row is not None) == live
    if row is not None:
        assert (row.user_id, row.fresh) == (client_only, fresh)


def test_a_start_with_a_hash_that_is_not_the_stored_one_is_stale(
    app_engine: Engine, client_only: uuid.UUID
) -> None:
    with app_engine.begin() as conn:
        outcome = conn.scalar(
            text("SELECT start_account_session(:u, :p, :h, NULL, NULL)"),
            {
                "u": client_only,
                "p": passwords.hash_password("not-it"),
                "h": secrets.token_bytes(32),
            },
        )
    assert outcome == "stale"


def reset_racing(
    app_engine: Engine, migrate_engine: Engine, email: str
) -> tuple[Callable[[], None], list[bool]]:
    new_hash = passwords.hash_password(NEW_PASSWORD)
    resets: list[bool] = []

    def reset() -> None:
        token = issue_link(app_engine, "password_reset", email)
        assert token is not None
        with app_engine.begin() as conn:
            resets.append(
                conn.scalar(
                    text("SELECT complete_password_reset(:h, :p)"),
                    {"h": auth.hash_token(token), "p": new_hash},
                )
            )

    return reset, resets


# FENCE (tests/test_password_reset.py:268-311, over both tables). Wrong impl: a
# start_account_session without FOR SHARE on the stored hash: a reset that commits after the check,
# before the sign-in commits, leaves an account session behind.
@pytest.mark.parametrize("route", ["/api/session", "/api/account/session"])
@pytest.mark.parametrize("when", ["while the password is checked", "before the sign-in commits"])
def test_a_reset_during_sign_in_leaves_no_session_of_either_kind(
    people: People,
    app: FastAPI,
    app_engine: Engine,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
    route: str,
) -> None:
    email = email_of(people.both)
    reset, resets = reset_racing(app_engine, migrate_engine, email)
    resetting = threading.Thread(target=reset)
    if when == "while the password is checked":
        verify = passwords.verify

        def verify_then_reset(stored: str | None, password: str) -> bool:
            matched = verify(stored, password)
            reset()
            return matched

        monkeypatch.setattr(passwords, "verify", verify_then_reset)
    else:
        start: Callable[..., str | None] = auth.start_account

        def start_while_resetting(*args: Any) -> str | None:
            # The session row is written, its transaction still open: a reset that isn't held back
            # by the FOR SHARE on the stored hash finishes here, before the session is visible to
            # its DELETE, and the session survives it.
            token = start(*args)
            resetting.start()
            resetting.join(timeout=1)
            return token

        monkeypatch.setattr(auth, "start_account", start_while_resetting)

    response = new_client(app).post(route, json={"email": email, "password": PASSWORD})
    if resetting.ident is not None:
        resetting.join(timeout=10)
        assert not resetting.is_alive()

    assert response.status_code in (200, 401)
    if when == "while the password is checked":
        assert response.status_code == 401
    assert resets == [True]
    assert sessions_of(migrate_engine, people.both) == 0
    assert account_sessions_of(migrate_engine, people.both) == 0


# FENCE. Wrong impl: separate limit keys for the account sign-in ("account_sign_in").
def test_both_sign_ins_spend_one_budget_of_guesses(app: FastAPI, people: People) -> None:
    client = new_client(app)
    email = email_of(people.both)
    statuses = [
        (console_sign_in if n % 2 else account_sign_in)(client, email, "wrong").status_code
        for n in range(auth.PER_ADDRESS_LIMIT + 1)
    ]
    assert statuses == [401] * auth.PER_ADDRESS_LIMIT + [429]


def test_a_wrong_password_and_an_unknown_email_answer_the_same(
    app: FastAPI, people: People
) -> None:
    client = new_client(app)
    wrong = account_sign_in(client, email_of(people.both), "wrong")
    unknown = account_sign_in(client, f"{uuid.uuid4()}@example.com")

    assert (wrong.status_code, wrong.json()) == (unknown.status_code, unknown.json())
    assert (wrong.status_code, wrong.json()) == (401, {"code": "invalid_credentials"})


# FENCE (one browser, one person). Wrong impl: ending only the old account cookie.
def test_an_account_sign_in_ends_another_persons_business_session_on_this_browser(
    app: FastAPI, people: People, migrate_engine: Engine, client_only: uuid.UUID
) -> None:
    client = signed_in(app, people.a, people.both)

    response = account_sign_in(client, email_of(client_only))

    assert response.status_code == 200
    assert auth.COOKIE in set_cookies(response)  # cleared
    assert sessions_of(migrate_engine, people.both) == 0
    assert client.get("/api/account").json()["email"] == email_of(client_only)


def test_an_account_sign_in_keeps_the_same_persons_business_session(
    app: FastAPI, people: People, migrate_engine: Engine
) -> None:
    client = signed_in(app, people.a, people.both)

    assert account_sign_in(client, email_of(people.both)).status_code == 200

    assert sessions_of(migrate_engine, people.both) == 1
    assert client.get("/api/session").status_code == 200


# FENCE (fixation). Wrong impl: starting a new account session without ending the old cookie's.
@pytest.mark.parametrize("second", ["account", "console"])
def test_a_sign_in_ends_this_browsers_previous_account_session(
    app: FastAPI, people: People, migrate_engine: Engine, client_only: uuid.UUID, second: str
) -> None:
    client = new_client(app)
    assert account_sign_in(client, email_of(client_only)).status_code == 200

    sign_in = account_sign_in if second == "account" else console_sign_in
    assert sign_in(client, email_of(people.both)).status_code == 200

    assert account_sessions_of(migrate_engine, client_only) == 0
    assert client.get("/api/account").json()["email"] == email_of(people.both)


@pytest.fixture
def admin(people: People, migrate_engine: Engine) -> Iterator[uuid.UUID]:
    with migrate_engine.begin() as conn:
        conn.execute(text("INSERT INTO platform_admins (user_id) VALUES (:u)"), {"u": people.both})
    yield people.both
    with migrate_engine.begin() as conn:
        conn.execute(text("DELETE FROM platform_admins WHERE user_id = :u"), {"u": people.both})


# FENCE (security N5). Wrong impl: an account session for a platform admin's identity.
def test_a_platform_admin_gets_no_account_session(
    app: FastAPI, admin: uuid.UUID, migrate_engine: Engine
) -> None:
    console = new_client(app)
    signed = console_sign_in(console, email_of(admin))
    assert signed.status_code == 200
    assert set(set_cookies(signed)) == {auth.COOKIE}

    response = account_sign_in(new_client(app), email_of(admin))

    assert (response.status_code, response.json()) == (403, {"code": "account_unavailable"})
    assert account_sessions_of(migrate_engine, admin) == 0


def test_removing_a_member_leaves_their_account_session(
    app: FastAPI, people: People, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    worker = new_client(app)
    assert console_sign_in(worker, email_of(people.only_a)).status_code == 200

    assert (
        owner.request(
            "DELETE", f"/api/members/{member_id(people.a, people.only_a)}", json={}
        ).status_code
        == 204
    )

    assert worker.get("/api/session").status_code == 401
    assert worker.get("/api/account").status_code == 200


# FENCE (security B3). Wrong impl: no Fetch Metadata check, or a cacheable answer.
@pytest.mark.parametrize("site", ["cross-site", "same-site", "none"])
def test_another_site_cannot_read_the_details(
    app: FastAPI, migrate_engine: Engine, client_only: uuid.UUID, site: str
) -> None:
    client = account_client(app, migrate_engine, client_only)

    response = client.get("/api/account", headers={"Sec-Fetch-Site": site})

    assert (response.status_code, response.json()) == (403, {"code": "cross_site"})


def test_the_details_are_private_to_this_browser(
    app: FastAPI, migrate_engine: Engine, client_only: uuid.UUID
) -> None:
    client = account_client(app, migrate_engine, client_only)

    response = client.get("/api/account", headers={"Sec-Fetch-Site": "same-origin"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["vary"] == "Cookie"
    assert "etag" not in response.headers


def test_a_password_reset_ends_account_sessions(
    app: FastAPI, people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    client = new_client(app)
    assert console_sign_in(client, email_of(people.both)).status_code == 200
    token = issue_link(app_engine, "password_reset", email_of(people.both))
    assert token is not None

    response = client.post(
        "/api/password-reset/complete", json={"token": token, "password": NEW_PASSWORD}
    )

    assert response.status_code == 204
    assert account_sessions_of(migrate_engine, people.both) == 0
    assert auth.ACCOUNT_COOKIE in set_cookies(response)  # this browser's is cleared too


def test_an_account_sign_in_is_audited_outside_any_business(
    app: FastAPI, migrate_engine: Engine, client_only: uuid.UUID
) -> None:
    assert account_sign_in(new_client(app), email_of(client_only)).status_code == 200

    rows = events(migrate_engine, action="account_signed_in", actor_user_id=client_only)
    assert [(r["tenant_id"], r["target"], r["details"]) for r in rows] == [
        (None, f"user:{client_only}", None)
    ]


def test_the_stored_session_is_only_a_hash(
    app: FastAPI, migrate_engine: Engine, client_only: uuid.UUID
) -> None:
    response = account_sign_in(new_client(app), email_of(client_only))
    token = set_cookies(response)[auth.ACCOUNT_COOKIE][auth.ACCOUNT_COOKIE].value
    with migrate_engine.connect() as conn:
        row = conn.execute(
            text("SELECT expires_at - now() AS lifetime FROM account_sessions WHERE id_hash = :h"),
            {"h": hashlib.sha256(token.encode()).digest()},
        ).one()
    assert timedelta(days=29, hours=23) < row.lifetime <= timedelta(days=30)


# FENCE. Wrong impl: today's DELETE /api/sessions, which ends business sessions only.
def test_signing_out_everywhere_from_the_console_ends_every_account_session(
    app: FastAPI, people: People, migrate_engine: Engine
) -> None:
    elsewhere = new_client(app)
    assert account_sign_in(elsewhere, email_of(people.both)).status_code == 200
    here = new_client(app)
    assert console_sign_in(here, email_of(people.both)).status_code == 200
    other = new_client(app)
    assert console_sign_in(other, email_of(people.only_a)).status_code == 200

    response = here.request("DELETE", "/api/sessions", json={})

    assert response.status_code == 204
    assert {auth.COOKIE, auth.ACCOUNT_COOKIE} <= set(set_cookies(response))
    assert account_sessions_of(migrate_engine, people.both) == 0
    assert elsewhere.get("/api/account").status_code == 401
    assert other.get("/api/account").status_code == 200  # someone else's stays


# FENCE. Wrong impl: ending only account sessions, or only this browser's.
def test_signing_out_everywhere_from_the_account_ends_every_session_of_both_kinds(
    app: FastAPI, people: People, migrate_engine: Engine
) -> None:
    console = new_client(app)
    assert console_sign_in(console, email_of(people.both)).status_code == 200
    here = new_client(app)
    assert account_sign_in(here, email_of(people.both)).status_code == 200
    other = new_client(app)
    assert console_sign_in(other, email_of(people.only_a)).status_code == 200

    response = here.request("DELETE", "/api/account/sessions", json={})

    assert response.status_code == 204
    assert {auth.COOKIE, auth.ACCOUNT_COOKIE} <= set(set_cookies(response))
    assert (
        sessions_of(migrate_engine, people.both),
        account_sessions_of(migrate_engine, people.both),
    ) == (0, 0)
    assert console.get("/api/session").status_code == 401
    assert other.get("/api/session").status_code == 200
    assert other.get("/api/account").status_code == 200
    rows = events(migrate_engine, action="signed_out_everywhere", actor_user_id=people.both)
    assert [r["tenant_id"] for r in rows] == [None]


def test_signing_out_everywhere_needs_an_account_session(app: FastAPI, people: People) -> None:
    business = signed_in(app, people.a, people.both)

    assert business.request("DELETE", "/api/account/sessions", json={}).status_code == 401
    assert business.get("/api/session").status_code == 200
