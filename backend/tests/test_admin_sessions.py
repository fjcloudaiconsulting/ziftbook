"""ZIF-70: platform admin sessions (migration 0037, app/admin_auth.py) and the business() helper."""

import hashlib
import secrets
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from http.cookies import SimpleCookie
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, event, text

from app import admin, admin_auth, auth, passwords, turnstile
from app.admin_auth import ADMIN_COOKIE, AdminSession, business
from app.errors import ApiError
from app.main import create_app
from tests.conftest import (
    PASSWORD,
    People,
    add_password,
    add_user,
    email_of,
    fresh_address,
    fresh_email,
    issue_link,
    new_client,
    signed_in,
)
from tests.test_admin_cli import sqlstate
from tests.test_bookings_db import seed_service

NEW_PASSWORD = "quiet-copper-kettle-7"
SESSIONS_OF = text("SELECT count(*) FROM admin_sessions WHERE user_id = :u")


@pytest.fixture
def an_admin(migrate_engine: Engine, bound: None) -> Iterator[uuid.UUID]:
    """A platform admin with a password and no business."""
    user_id = add_user(migrate_engine)
    add_password(migrate_engine, user_id, PASSWORD)
    with migrate_engine.begin() as conn:
        conn.execute(text("INSERT INTO platform_admins (user_id) VALUES (:u)"), {"u": user_id})
    yield user_id
    with migrate_engine.begin() as conn:
        # Its admin sessions go with it (ON DELETE CASCADE).
        conn.execute(text("DELETE FROM platform_admins WHERE user_id = :u"), {"u": user_id})
        conn.execute(text("DELETE FROM password_credentials WHERE user_id = :u"), {"u": user_id})
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


@pytest.fixture
def app(people: People, migrate_engine: Engine) -> FastAPI:
    add_password(migrate_engine, people.both, PASSWORD)  # an owner in a, a worker in b
    return create_app()


def admin_sign_in(client: TestClient, email: str, password: str = PASSWORD) -> Response:
    return client.post("/api/admin/session", json={"email": email, "password": password})


def admin_client(app: FastAPI, user_id: uuid.UUID) -> TestClient:
    client = new_client(app)
    assert admin_sign_in(client, email_of(user_id)).status_code == 200
    return client


def whoami(client: TestClient) -> int:
    return client.get("/api/admin/session").status_code


def stored_hash(migrate_engine: Engine, user_id: uuid.UUID) -> str:
    with migrate_engine.connect() as conn:
        found: str = conn.scalar(
            text("SELECT hash FROM password_credentials WHERE user_id = :u"), {"u": user_id}
        )
    return found


def start(app_engine: Engine, user_id: uuid.UUID, password_hash: str) -> bool:
    """start_admin_session as the app calls it, with a fresh session id."""
    with app_engine.begin() as conn:
        started: bool = conn.scalar(
            text("SELECT start_admin_session(:u, :h, :id, NULL, NULL)"),
            {"u": user_id, "h": password_hash, "id": hashlib.sha256(uuid.uuid4().bytes).digest()},
        )
    return started


def sessions_of(migrate_engine: Engine, user_id: uuid.UUID) -> int:
    with migrate_engine.connect() as conn:
        count: int = conn.scalar(SESSIONS_OF, {"u": user_id})
    return count


def platform_events(
    migrate_engine: Engine, action: str, **match: object
) -> list[dict[str, object]]:
    where = "".join(f" AND {column} = :{column}" for column in match)
    with migrate_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT actor_user_id, kind, tenant_id, target FROM platform_audit_events "
                f"WHERE action = :action{where} ORDER BY id"
            ),
            {"action": action, **match},
        ).mappings()
        return [dict(row) for row in rows]


def a_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [(b"user-agent", b"pytest")],
            "client": ("192.0.2.1", 1),
        }
    )


# fence: the app role can't forge, read, extend or end an admin session itself. Kills 0001's default
# grants left in place (an injection inserting a session row for any admin).
def test_app_role_has_no_dml_on_admin_sessions(app_engine: Engine, an_admin: uuid.UUID) -> None:
    params = {"h": hashlib.sha256(b"x").digest(), "u": an_admin}
    for sql in (
        "INSERT INTO admin_sessions (id_hash, user_id, expires_at) "
        "VALUES (:h, :u, now() + interval '1 hour')",
        "SELECT * FROM admin_sessions",
        "UPDATE admin_sessions SET expires_at = now() + interval '1 year'",
        "DELETE FROM admin_sessions",
    ):
        assert sqlstate(app_engine, sql, params) == "42501", sql


# fence: a session starts only with the hash stored for that admin. Kills a function that skips the
# password_credentials join (any admin id would then mint a session).
def test_start_admin_session_needs_the_stored_hash(
    app_engine: Engine, migrate_engine: Engine, an_admin: uuid.UUID
) -> None:
    assert start(app_engine, an_admin, "not-the-hash") is False
    assert sessions_of(migrate_engine, an_admin) == 0
    # The same call with the stored hash works, so the refusal above is the hash's doing.
    assert start(app_engine, an_admin, stored_hash(migrate_engine, an_admin)) is True
    assert sessions_of(migrate_engine, an_admin) == 1


# fence: a business owner's real password hash starts no admin session. Kills a function that skips
# platform_admins.
def test_start_admin_session_needs_an_admin(
    app: FastAPI, people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    owner = people.both
    assert start(app_engine, owner, stored_hash(migrate_engine, owner)) is False


# fence: wrong password, unknown email, and an owner with their right password: one answer, no
# cookie. Kills a 403 not_admin (an oracle telling a phished password's holder who is an admin).
def test_sign_in_gives_one_answer(app: FastAPI, people: People, an_admin: uuid.UUID) -> None:
    answers = [
        admin_sign_in(new_client(app), email_of(an_admin), "wrong-password-123"),
        admin_sign_in(new_client(app), fresh_email()),
        admin_sign_in(new_client(app), email_of(people.both)),
    ]

    assert [a.status_code for a in answers] == [401, 401, 401]
    assert {a.content for a in answers} == {b'{"code":"invalid_credentials"}'}
    headers = [
        {k: v for k, v in a.headers.items() if k not in ("date", "x-request-id")} for a in answers
    ]
    assert headers[0] == headers[1] == headers[2]
    assert "set-cookie" not in headers[0]


# guard: the cookie's flags, who-am-I, and the audit record.
def test_admin_sign_in(app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID) -> None:
    client = new_client(app)

    response = admin_sign_in(client, f"  {email_of(an_admin).upper()} ")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    expected = {
        "user_id": str(an_admin),
        "email": email_of(an_admin),
        "name": "Test Person",
        "second_factor": "enrolment_required",  # the password only buys a half session
    }
    assert response.json() == expected
    cookie = SimpleCookie(response.headers["set-cookie"])[ADMIN_COOKIE]
    assert (cookie["secure"], cookie["httponly"]) == (True, True)
    assert (cookie["path"], cookie["samesite"], cookie["max-age"]) == ("/", "strict", "28800")
    who = client.get("/api/admin/session")
    assert who.status_code == 200
    assert who.json() == expected
    assert who.headers["cache-control"] == "no-store"
    assert platform_events(migrate_engine, "admin_signed_in", actor_user_id=an_admin) == [
        {"actor_user_id": an_admin, "kind": "auth", "tenant_id": None, "target": None}
    ]


# fence: the sixth attempt for one email in the window is refused even with the right password,
# before any password is checked. Kills counting only failures, a limit checked after the hash (an
# attacker's guesses past the limit still cost a hash each), or no limit at all.
def test_admin_sign_in_rate_limited(
    app: FastAPI, an_admin: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = new_client(app)
    email = email_of(an_admin)
    verify = passwords.verify
    checked: list[str] = []

    def counting(stored: str | None, password: str) -> bool:
        checked.append(password)
        return verify(stored, password)

    monkeypatch.setattr(passwords, "verify", counting)

    wrong = [admin_sign_in(client, email, "wrong-password-123").status_code for _ in range(5)]

    assert wrong == [401] * 5
    response = admin_sign_in(client, email)
    assert response.status_code == 429
    assert response.json() == {"code": "rate_limited"}
    assert "set-cookie" not in response.headers
    assert len(checked) == 5  # the sixth was never hashed


def test_admin_per_account_limit_is_per_64_within_a_48(app: FastAPI, an_admin: uuid.UUID) -> None:
    # Someone sharing the admin's /48 must not be able to spend the admin's sign-in attempts.
    email = email_of(an_admin)
    network = f"2001:db8:{secrets.randbelow(65535) + 1:x}"
    first = new_client(app, f"{network}:1::1")
    statuses = [admin_sign_in(first, email, "wrong-password-123").status_code for _ in range(6)]
    assert statuses[-1] == 429
    assert admin_sign_in(new_client(app, f"{network}:2::1"), email).status_code == 200


def test_admin_address_limit_is_shared_across_a_48(app: FastAPI) -> None:
    network = f"2001:db8:{secrets.randbelow(65535) + 1:x}"
    for subnet in (1, 2):
        client = new_client(app, f"{network}:{subnet}::1")
        for _ in range(10):
            admin_sign_in(client, fresh_email(), "wrong-password-123")
    third = new_client(app, f"{network}:3::1")
    assert admin_sign_in(third, fresh_email(), "wrong-password-123").status_code == 429


def test_distributed_admin_failures_ask_for_a_challenge(
    app: FastAPI, an_admin: uuid.UUID, log_lines: Callable[[], list[dict[str, Any]]]
) -> None:
    email = email_of(an_admin)
    for _ in range(admin_auth.ADMIN_FAILURES_BEFORE_CHALLENGE):
        assert admin_sign_in(new_client(app), email, "wrong-password-123").status_code == 401

    blocked = admin_sign_in(new_client(app), email)
    again = admin_sign_in(new_client(app), email)

    assert (blocked.status_code, blocked.json()) == (429, {"code": "challenge_required"})
    assert again.status_code == 429
    assert "set-cookie" not in blocked.headers
    warned = [line for line in log_lines() if line["msg"] == "admin sign-in failures"]
    assert [(line["level"], line["target"]) for line in warned] == [("WARNING", f"user:{an_admin}")]
    solved = new_client(app).post(
        "/api/admin/session",
        json={"email": email, "password": PASSWORD, "turnstile_token": "t"},
    )
    assert solved.status_code == 200  # ZIF_TURNSTILE_DISABLED in tests: verify() passes
    assert solved.json()["second_factor"] == "enrolment_required"  # the second factor still follows
    past = new_client(app).post(
        "/api/admin/session",
        json={"email": email, "password": "wrong-password-123", "turnstile_token": "t"},
    )
    assert past.status_code == 401  # a solved challenge, still a wrong password
    warned = [line for line in log_lines() if line["msg"] == "admin sign-in failures"]
    assert len(warned) == 1


def test_only_the_failure_reaching_the_threshold_reports_it(app: FastAPI) -> None:
    email = fresh_email()

    reached = [auth.count_failure(email, "admin_sign_in", 10) for _ in range(11)]

    assert reached == [False] * 9 + [True, False]


def test_unknown_admin_email_asks_for_a_challenge_the_same_way(
    app: FastAPI, log_lines: Callable[[], list[dict[str, Any]]]
) -> None:
    email = fresh_email()
    for _ in range(admin_auth.ADMIN_FAILURES_BEFORE_CHALLENGE):
        assert admin_sign_in(new_client(app), email).status_code == 401

    blocked = admin_sign_in(new_client(app), email)

    assert (blocked.status_code, blocked.json()) == (429, {"code": "challenge_required"})
    warned = [line for line in log_lines() if line["msg"] == "admin sign-in failures"]
    assert [line.get("target") for line in warned] == [None]


def test_a_refused_admin_challenge_keeps_out_even_the_right_password(
    app: FastAPI, an_admin: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = email_of(an_admin)
    for _ in range(admin_auth.ADMIN_FAILURES_BEFORE_CHALLENGE):
        admin_sign_in(new_client(app), email, "wrong-password-123")
    calls: list[tuple[str | None, str | None, str]] = []

    def refuse(token: str | None, ip: str | None, *, action: str) -> bool:
        calls.append((token, ip, action))
        return False

    monkeypatch.setattr(turnstile, "verify", refuse)
    address = fresh_address()

    refused = new_client(app, address).post(
        "/api/admin/session", json={"email": email, "password": PASSWORD, "turnstile_token": "t"}
    )

    assert (refused.status_code, refused.json()) == (429, {"code": "challenge_required"})
    assert calls == [("t", address, "admin_sign_in")]


def business_sign_in(client: TestClient, email: str, password: str = PASSWORD) -> Response:
    return client.post("/api/session", json={"email": email, "password": password})


def answer(response: Response) -> tuple[int, str | None]:
    return response.status_code, response.json().get("code")


WRONG = (401, "invalid_credentials")
CHALLENGE = (429, "challenge_required")


@pytest.fixture
def every_kind(
    app: FastAPI, an_admin: uuid.UUID, people: People, migrate_engine: Engine
) -> Iterator[dict[str, str]]:
    """An email of each kind business sign-in must not tell apart: a platform admin with no
    business, one with a business, a member who is no admin, an account with no business, and no
    account at all."""
    add_password(migrate_engine, people.only_a, PASSWORD)
    nobody = add_user(migrate_engine)
    add_password(migrate_engine, nobody, PASSWORD)
    with migrate_engine.begin() as conn:
        # Goes with people.both's account (ON DELETE CASCADE).
        conn.execute(text("INSERT INTO platform_admins (user_id) VALUES (:u)"), {"u": people.both})
    yield {
        "admin": email_of(an_admin),
        "admin_in_a_business": email_of(people.both),
        "member": email_of(people.only_a),
        "no_business": email_of(nobody),
        "unknown": fresh_email(),
    }
    with migrate_engine.begin() as conn:
        conn.execute(text("DELETE FROM password_credentials WHERE user_id = :u"), {"u": nobody})
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": nobody})


# ZB-18: wrong passwords tried on business sign-in count toward the admin sign-in challenge.
def test_business_sign_in_failures_ask_admin_sign_in_for_a_challenge(
    app: FastAPI, an_admin: uuid.UUID
) -> None:
    email = email_of(an_admin)
    for _ in range(admin_auth.ADMIN_FAILURES_BEFORE_CHALLENGE):
        assert business_sign_in(new_client(app), email, "wrong-password-123").status_code == 401

    blocked = admin_sign_in(new_client(app), email)

    assert answer(blocked) == CHALLENGE
    solved = new_client(app).post(
        "/api/admin/session", json={"email": email, "password": PASSWORD, "turnstile_token": "t"}
    )
    assert solved.status_code == 200  # ZIF_TURNSTILE_DISABLED in tests: verify() passes


# fence (ZB-18): guesses at an admin's password spread over both routes reach one challenge, after
# 11 in all, the threshold both routes share. Kills each route counting only its own failures.
def test_guesses_spread_over_both_routes_reach_one_challenge(
    app: FastAPI, an_admin: uuid.UUID
) -> None:
    email = email_of(an_admin)
    answers = [
        answer(
            new_client(app).post(
                "/api/session" if n % 2 == 0 else "/api/admin/session",
                json={"email": email, "password": "wrong-password-123"},
            )
        )
        for n in range(24)
    ]

    assert answers == [WRONG] * 11 + [CHALLENGE] * 13
    assert answer(business_sign_in(new_client(app), email)) == CHALLENGE
    assert answer(admin_sign_in(new_client(app), email)) == CHALLENGE


# fence (ZB-01, ZB-18): one address alone never challenges an account, even through both routes:
# admin sign-in spends business sign-in's attempts from that address too, so it gets 10 password
# attempts at an email in all, fewer than the challenge's 11. Kills the routes' per-address limits
# adding up (15 failures from one address) and an admin threshold one address can reach.
@pytest.mark.parametrize("route", ["/api/session", "/api/admin/session"])
def test_one_address_using_both_routes_never_challenges_an_account(
    app: FastAPI, an_admin: uuid.UUID, people: People, migrate_engine: Engine, route: str
) -> None:
    if route == "/api/session":
        add_password(migrate_engine, people.only_a, PASSWORD)
        email = email_of(people.only_a)  # a member
    else:
        email = email_of(an_admin)
    attacker = new_client(app)
    on_admin = [answer(admin_sign_in(attacker, email, "wrong-password-123")) for _ in range(5)]
    on_business = [
        answer(business_sign_in(attacker, email, "wrong-password-123")) for _ in range(11)
    ]

    right = new_client(app).post(route, json={"email": email, "password": PASSWORD})

    rate_limited = (429, "rate_limited")
    assert (right.status_code, on_admin, on_business) == (
        200,
        [WRONG] * 5,
        [WRONG] * 5 + [rate_limited] * 6,
    )


# ZB-18: business sign-in tells no one who is a platform admin. Every email gets the same answers
# and reaches the challenge after as many wrong passwords, whatever its account.
def test_business_sign_in_challenges_every_kind_of_email_alike(
    app: FastAPI, every_kind: dict[str, str]
) -> None:
    answers = {
        kind: [
            answer(business_sign_in(new_client(app), email, "wrong-password-123"))
            for _ in range(auth.FAILURES_BEFORE_CHALLENGE + 1)
        ]
        for kind, email in every_kind.items()
    }

    expected = [WRONG] * auth.FAILURES_BEFORE_CHALLENGE + [CHALLENGE]
    assert answers == dict.fromkeys(every_kind, expected)


# ZB-18: admin sign-in's failures count on business sign-in for every email, not only an admin's,
# so where the challenge comes tells no one who is an admin.
def test_admin_sign_in_failures_count_on_business_sign_in_for_every_kind_of_email(
    app: FastAPI, every_kind: dict[str, str]
) -> None:
    for email in every_kind.values():
        for _ in range(admin_auth.ADMIN_FAILURES_BEFORE_CHALLENGE):
            assert admin_sign_in(new_client(app), email, "wrong-password-123").status_code == 401
    left = auth.FAILURES_BEFORE_CHALLENGE - admin_auth.ADMIN_FAILURES_BEFORE_CHALLENGE

    answers = {
        kind: [
            answer(business_sign_in(new_client(app), email, "wrong-password-123"))
            for _ in range(left + 1)
        ]
        for kind, email in every_kind.items()
    }

    assert answers == dict.fromkeys(every_kind, [WRONG] * left + [CHALLENGE])


# fence (ZB-18): a wrong password on business sign-in runs the same statements and one hash for
# every kind of email, so its timing tells no one who is an admin. Kills an admin lookup added to
# one path only.
def test_a_wrong_business_password_does_the_same_work_for_every_kind_of_email(
    app: FastAPI,
    every_kind: dict[str, str],
    app_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []
    verify_hash = passwords._verify_hash

    def hashed(stored: str, password: str) -> bool:
        statements.append("hash")
        return verify_hash(stored, password)

    def executed(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    monkeypatch.setattr(passwords, "_verify_hash", hashed)
    work: dict[str, list[str]] = {}
    event.listen(app_engine, "before_cursor_execute", executed)
    try:
        for kind, email in every_kind.items():
            statements.clear()
            assert business_sign_in(new_client(app), email, "wrong-password-123").status_code == 401
            work[kind] = list(statements)
    finally:
        event.remove(app_engine, "before_cursor_execute", executed)

    assert work["member"].count("hash") == 1
    assert work == dict.fromkeys(every_kind, work["member"])


# fence: 30 minutes idle ends a session, and use keeps it alive. Kills reusing auth.IDLE (7 days),
# and a resolve that never touches last_seen_at (an active admin dropped at 30 minutes).
def test_idle_timeout_is_30_minutes(
    app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID
) -> None:
    client = admin_client(app, an_admin)
    idle = (
        "UPDATE admin_sessions SET last_seen_at = now() - CAST(:i AS interval) WHERE user_id = :u"
    )

    with migrate_engine.begin() as conn:
        conn.execute(text(idle), {"i": "29 minutes", "u": an_admin})
    assert whoami(client) == 200
    with migrate_engine.connect() as conn:
        since: timedelta = conn.scalar(
            text("SELECT now() - last_seen_at FROM admin_sessions WHERE user_id = :u"),
            {"u": an_admin},
        )
    assert since < timedelta(seconds=10)

    with migrate_engine.begin() as conn:
        conn.execute(text(idle), {"i": "31 minutes", "u": an_admin})
    assert whoami(client) == 401


# fence: a session lasts 8 hours from sign-in, whatever its use. Kills a 30-day or caller-chosen
# lifetime.
def test_absolute_lifetime_is_8_hours(
    app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID
) -> None:
    client = admin_client(app, an_admin)
    with migrate_engine.connect() as conn:
        left: timedelta = conn.scalar(
            text("SELECT expires_at - now() FROM admin_sessions WHERE user_id = :u"),
            {"u": an_admin},
        )
    assert timedelta(hours=7, minutes=59) < left <= timedelta(hours=8)

    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE admin_sessions SET expires_at = now() - interval '1 second' "
                "WHERE user_id = :u"
            ),
            {"u": an_admin},
        )
    assert whoami(client) == 401


# fence: a business session's token is no admin session. Kills a resolve that also looks in
# sessions.
def test_business_cookie_is_not_an_admin_cookie(app: FastAPI, people: People) -> None:
    owner = signed_in(app, people.a, people.both)
    token = owner.cookies[auth.COOKIE]
    assert owner.get("/api/session").status_code == 200  # a live business session

    owner.cookies.set(ADMIN_COOKIE, token)

    assert whoami(owner) == 401


# fence: revoking an admin ends their live sessions. Kills a foreign key without ON DELETE CASCADE.
def test_revoke_ends_admin_sessions(app: FastAPI, an_admin: uuid.UUID) -> None:
    client = admin_client(app, an_admin)
    assert whoami(client) == 200

    assert admin.main(["revoke", email_of(an_admin), "--reason", "left the team"]) == 0

    assert whoami(client) == 401


# fence: a password reset ends the person's admin sessions, as it does their business ones. Kills
# reset_password left as 0010 wrote it.
def test_password_reset_ends_admin_sessions(
    app: FastAPI, app_engine: Engine, an_admin: uuid.UUID
) -> None:
    client = admin_client(app, an_admin)
    assert whoami(client) == 200
    token = issue_link(app_engine, "password_reset", email_of(an_admin))
    assert token is not None

    response = new_client(app).post(
        "/api/password-reset/complete", json={"token": token, "password": NEW_PASSWORD}
    )

    assert response.status_code == 204
    assert whoami(client) == 401


# guard: sign-out ends the session, clears the cookie and is recorded.
def test_admin_sign_out(app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID) -> None:
    client = admin_client(app, an_admin)

    response = client.request("DELETE", "/api/admin/session", json={})

    assert response.status_code == 204
    assert SimpleCookie(response.headers["set-cookie"])[ADMIN_COOKIE]["max-age"] == "0"
    assert sessions_of(migrate_engine, an_admin) == 0
    assert platform_events(migrate_engine, "admin_signed_out", actor_user_id=an_admin) == [
        {"actor_user_id": an_admin, "kind": "auth", "tenant_id": None, "target": None}
    ]
    assert whoami(client) == 401


# fence: business() records the read first, in the read's own transaction, and scopes it to that
# one business. Kills (1)/(3) a record in its own committed transaction, (5) a record after the
# yield, (2) a helper that never sets the tenant (the read would raise, not see a's rows).
def test_business_helper_records_and_isolates(
    people: People, migrate_engine: Engine, an_admin: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = AdminSession(
        user_id=an_admin,
        email=email_of(an_admin),
        name=None,
        factor_passed=True,
        factor_fresh=True,
        enrolled=True,
    )
    in_a, in_b = seed_service(people.a), seed_service(people.b)
    tag = uuid.uuid4().hex

    # (1) and (2)
    with business(current, a_request(), people.a, target=f"services:{tag}") as session:
        seen = set(session.scalars(text("SELECT id FROM services")))
    assert in_a in seen and in_b not in seen
    assert platform_events(migrate_engine, "business_read", target=f"services:{tag}") == [
        {
            "actor_user_id": an_admin,
            "kind": "read",
            "tenant_id": people.a,
            "target": f"services:{tag}",
        }
    ]

    # (3) a read that fails leaves no record
    with pytest.raises(RuntimeError), business(current, a_request(), people.a, target=tag):
        raise RuntimeError("the read failed")
    assert platform_events(migrate_engine, "business_read", target=tag) == []

    # (4) an unknown business
    with pytest.raises(ApiError) as error, business(current, a_request(), uuid.uuid4(), target=tag):
        pass
    assert (error.value.status_code, error.value.code) == (404, "not_found")

    # (5) a record that fails stops the read before it runs
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("record failed")

    monkeypatch.setattr(admin, "record", broken)
    ran = False
    with pytest.raises(RuntimeError), business(current, a_request(), people.a, target=tag):
        ran = True
    assert ran is False
